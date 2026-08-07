"""Offline tests for the pipeline integration core (no API key, no network).

Each test gets its own isolated SQLite database by rebinding ``db.session``'s
engine + sessionmaker to a fresh on-disk temp DB and recreating the schema.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
from agents.llm import FakeChat
from core.schemas import Lead
from db.models import Base, LeadRecord, LeadStatus, ProposalRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Rebind the shared engine/SessionLocal to an isolated temp SQLite file."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


class FakeRetriever:
    def retrieve(self, query, k=3):
        return [
            {
                "text": "multi-cloud-k8s-terraform cut infra cost 40%.",
                "source": "multi-cloud-k8s-terraform",
                "kind": "win",
                "score": 0.9,
            }
        ]


def _lead(i: int) -> Lead:
    return Lead(
        source="upwork_rss",
        external_id=f"job-{i}",
        title=f"Kubernetes + DevSecOps hardening #{i}",
        description="Secure our EKS clusters and CI/CD with Terraform.",
        company="Acme Corp",
        budget="$90/hr",
        tags=["kubernetes", "devsecops"],
    )


class FakeSource:
    """In-memory source returning a fixed list of leads."""

    name = "upwork_rss"

    def __init__(self, leads):
        self._leads = leads

    def fetch(self, limit: int = 50):
        return list(self._leads[:limit])


def _route_structured(messages):
    system = ""
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else None
        if role == "system":
            system = m.get("content", "")
            break
    if "qualifier" in system:
        return {
            "lead": _lead(0).model_dump(),
            "fit_score": 90,
            "reasons": ["strong k8s + devsecops fit"],
            "matched_projects": ["multi-cloud-k8s-terraform"],
        }
    return {
        "summary": "Acme runs EKS and wants CI/CD hardening.",
        "tech_stack": ["EKS", "Terraform"],
        "pain_points": ["insecure pipelines"],
        "contacts": [],
    }


def _high_fit_chat() -> FakeChat:
    body = (
        "Hi Acme, I have hardened Kubernetes platforms and CI/CD pipelines for "
        "years. On multi-cloud-k8s-terraform I cut infrastructure cost 40% and "
        "lifted deploy frequency 75% with security gates kept green. I can audit "
        "your EKS clusters, add policy-as-code guardrails, and wire signed "
        "supply-chain checks into your pipeline, shipping in small reviewable "
        "increments so you stay in control. Glad to share a concrete plan on a "
        "short call: https://cal.com/surya-devsecops/15min"
    )
    return FakeChat(responses=[body, body, body, body, body], structured=_route_structured)


def test_run_pipeline_persists_queued_leads_and_drafts(temp_db):
    from pipeline import run_pipeline

    sources = [FakeSource([_lead(1), _lead(2)])]
    stats = run_pipeline(
        sources=sources, retriever=FakeRetriever(), chat=_high_fit_chat()
    )

    assert stats["fetched"] == 2
    assert stats["new"] == 2
    assert stats["queued"] == 2
    assert stats["skipped"] == 0
    assert "cost_usd" in stats
    assert stats["budget_exhausted"] is False

    with dbsession.get_session() as session:
        leads = session.query(LeadRecord).all()
        assert len(leads) == 2
        assert all(lead.status == LeadStatus.drafted for lead in leads)
        assert all(lead.fit_score == 90 for lead in leads)
        proposals = session.query(ProposalRecord).all()
        assert len(proposals) == 2
        assert all("multi-cloud-k8s-terraform" in p.cited_projects for p in proposals)


def test_dedupe_skips_already_present(temp_db):
    from pipeline import run_pipeline

    # Pre-load one of the leads.
    with dbsession.get_session() as session:
        session.add(
            LeadRecord(
                source="upwork_rss",
                external_id="job-1",
                title="already here",
                status=LeadStatus.drafted,
            )
        )

    sources = [FakeSource([_lead(1), _lead(2)])]
    stats = run_pipeline(
        sources=sources, retriever=FakeRetriever(), chat=_high_fit_chat()
    )

    assert stats["fetched"] == 2
    assert stats["skipped"] == 1
    assert stats["new"] == 1
    assert stats["queued"] == 1


def test_max_proposals_per_day_cap(temp_db, monkeypatch):
    import config
    from pipeline import run_pipeline

    real_get = config.get_settings

    def capped():
        s = real_get()
        s.max_proposals_per_day = 1
        return s

    monkeypatch.setattr("pipeline.get_settings", capped)

    sources = [FakeSource([_lead(1), _lead(2), _lead(3)])]
    stats = run_pipeline(
        sources=sources, retriever=FakeRetriever(), chat=_high_fit_chat()
    )

    # First lead queues, then the per-day cap stops further queuing.
    assert stats["queued"] == 1
    with dbsession.get_session() as session:
        assert session.query(ProposalRecord).count() == 1


def test_stats_include_cost_usd(temp_db):
    from pipeline import run_pipeline

    stats = run_pipeline(
        sources=[FakeSource([_lead(1)])],
        retriever=FakeRetriever(),
        chat=_high_fit_chat(),
    )
    assert "cost_usd" in stats
    assert isinstance(stats["cost_usd"], float)


def test_over_budget_tracker_stops_cleanly(temp_db, monkeypatch):
    """A pre-exhausted budget makes the first metered call raise -> clean stop."""
    import pipeline as pipeline_mod
    from costs import CostTracker
    from pipeline import run_pipeline

    over = CostTracker(budget_usd=2.0)
    over.record("claude-opus-4-8", 0, 1_000_000)  # $25 spent, over the $2 cap
    assert over.would_exceed()

    monkeypatch.setattr(pipeline_mod, "CostTracker", lambda budget_usd=None: over)

    stats = run_pipeline(
        sources=[FakeSource([_lead(1), _lead(2)])],
        retriever=FakeRetriever(),
        chat=_high_fit_chat(),
    )

    assert stats["budget_exhausted"] is True
    assert stats["queued"] == 0
    with dbsession.get_session() as session:
        assert session.query(LeadRecord).count() == 0


def test_pipeline_stats_and_top_queued(temp_db):
    from pipeline import pipeline_stats, run_pipeline, top_queued

    run_pipeline(
        sources=[FakeSource([_lead(1), _lead(2)])],
        retriever=FakeRetriever(),
        chat=_high_fit_chat(),
    )
    stats = pipeline_stats()
    assert stats["total_leads"] == 2
    assert stats["by_status"]["drafted"] == 2

    top = top_queued(n=5)
    assert len(top) == 2
    assert top[0]["fit_score"] == 90


# --------------------------------------------------------------------------- #
# fit-score distribution: making "dropped: N" actionable
# --------------------------------------------------------------------------- #
def test_fit_summary_blames_the_threshold_on_near_misses():
    """Scores bunched just under the bar mean the bar is wrong, not the sources."""
    from pipeline import _fit_summary

    s = _fit_summary([62, 64, 65, 67, 68, 69], threshold=70)
    assert s["passed"] == 0
    assert s["near_miss"] == 6
    assert "threshold" in s["bottleneck"]
    assert "min_fit_score" in s["bottleneck"]


def test_fit_summary_refuses_to_blame_targeting_on_a_censored_sample():
    """A confident verdict over a sample that excluded the best-targeted source.

    Three consecutive production runs printed "The lead mix is off-ICP; fix targeting,
    not the threshold." while the day-rate contract feed — the one adapter built for
    this ICP, and the only one that reports an actual day rate — contributed **zero**
    scores, because its leads carry no email and were dropped before qualification.
    That verdict sends you to fix targeting that is already correct, which is worse
    than printing nothing: missing data makes you look, wrong data makes you act.
    """
    from pipeline import _fit_summary

    # 4 scored, 40 withheld: nothing here licenses a claim about the lead mix.
    s = _fit_summary([5, 8, 12, 15], threshold=70, unscored=40)
    assert "sample" in s["bottleneck"]
    assert "off-ICP" not in s["bottleneck"]
    assert "fix targeting" not in s["bottleneck"]
    assert s["unscored"] == 40

    # Same scores, nothing withheld -> the off-ICP verdict is now earned.
    s = _fit_summary([5, 8, 12, 15], threshold=70, unscored=0)
    assert "off-ICP" in s["bottleneck"]


def test_fit_summary_blames_the_sources_when_scores_are_far_below():
    from pipeline import _fit_summary

    s = _fit_summary([5, 8, 12, 15, 20, 22], threshold=70)
    assert s["near_miss"] == 0
    assert "sources" in s["bottleneck"]
    assert "off-ICP" in s["bottleneck"]


def test_fit_summary_reports_no_bottleneck_when_leads_pass():
    from pipeline import _fit_summary

    s = _fit_summary([40, 75, 90], threshold=70)
    assert s["passed"] == 2
    assert "none" in s["bottleneck"]


# --------------------------------------------------------------------------- #
# per-source attribution: making "the lead mix is off-ICP" actionable
# --------------------------------------------------------------------------- #
def test_per_source_summary_separates_the_good_source_from_the_useless_ones():
    """`_fit_summary` says the mix is off-ICP; this says WHICH sources caused it.

    "All sources are mediocre" and "six are useless and one is good" produce the same
    totals and need opposite responses — expand the winner vs. re-target everything.
    """
    from pipeline import _per_source_summary

    out = _per_source_summary(
        {
            "hn_hiring": {
                "fetched": 10, "new": 10, "contactable": 6, "queued": 2,
                "scores": [40, 72, 88],
            },
            "jobicy": {
                "fetched": 20, "new": 20, "contactable": 18, "queued": 0,
                "scores": [8, 12, 15],
            },
        },
        threshold=70,
    )

    assert out["hn_hiring"]["passed"] == 2
    assert "productive" in out["hn_hiring"]["verdict"]
    assert out["jobicy"]["passed"] == 0
    assert "off-ICP" in out["jobicy"]["verdict"]
    # The verdict must name the evidence, so it can be acted on without re-deriving it.
    assert "15" in out["jobicy"]["verdict"] and "70" in out["jobicy"]["verdict"]


def test_a_source_that_fetched_nothing_is_reported_as_dead():
    """A dead source must be visible, not absent.

    An omitted row reads as "not a problem" — which is the failure mode this whole
    report exists to expose. run_pipeline seeds a row for every enabled source so a
    source yielding zero is still named.
    """
    from pipeline import _per_source_summary

    out = _per_source_summary(
        {"upwork_rss": {"fetched": 0, "new": 0, "contactable": 0, "queued": 0, "scores": []}},
        threshold=70,
    )
    assert "dead" in out["upwork_rss"]["verdict"]


def test_a_source_whose_leads_are_never_reachable_is_called_out_separately():
    """Yields good leads that carry no address = a channel problem, not a bad source.

    Distinct from "off-ICP" and from "dead": the leads are real, new, and they score
    well — they simply have no email, so only the human-submit channel can use them.

    This verdict used to read "unreachable: scoring them is wasted spend" and was
    checked *before* the `not scores` branch, which made it unfalsifiable: the
    pre-gate meant an uncontactable lead was never scored, so `scores` was always
    empty, so this branch always won — and then advised retiring the feed that had
    just surfaced a $208k-$249k Forward Deployed Engineer role. Adzuna publishes no
    address by design (it sells the redirect click); that makes the feed
    email-blocked, not wasteful.
    """
    from pipeline import _per_source_summary

    out = _per_source_summary(
        {
            "contract_jobs": {
                "fetched": 25,
                "new": 25,
                "contactable": 0,
                "queued": 0,
                "scores": [72, 81, 88],
            }
        },
        threshold=70,
    )
    verdict = out["contract_jobs"]["verdict"]
    assert "email-blocked" in verdict
    assert "human-submit" in verdict
    # It must NOT tell you to stop scoring a source that scores 88.
    assert "wasted spend" not in verdict


def test_an_unscored_source_is_not_reported_as_unreachable():
    """The ordering bug itself, pinned.

    With no scores at all, the honest report is "I never looked", not a confident
    claim about the leads' quality or contactability.
    """
    from pipeline import _per_source_summary

    out = _per_source_summary(
        {"contra_startup": {"fetched": 9, "new": 9, "contactable": 0, "queued": 0, "scores": []}},
        threshold=70,
    )
    assert "unscored" in out["contra_startup"]["verdict"]


def test_a_source_returning_only_already_seen_leads_is_stale_not_dead():
    """Fetching 25 leads we've all seen before is a different problem from fetching 0."""
    from pipeline import _per_source_summary

    out = _per_source_summary(
        {"hn_freelancer": {"fetched": 25, "new": 0, "contactable": 0, "queued": 0, "scores": []}},
        threshold=70,
    )
    assert "stale" in out["hn_freelancer"]["verdict"]


def test_run_pipeline_reports_every_enabled_source_even_a_silent_one(temp_db):
    """End-to-end: a source that returns [] still gets a row in the run stats.

    Attribution built only from returned leads would omit it, and an absent row reads
    as "no problem here" — so run_pipeline seeds a row per enabled source.
    """
    from pipeline import run_pipeline

    class SilentSource:
        name = "silent_board"

        def fetch(self, limit: int = 50):
            return []

    stats = run_pipeline(
        sources=[SilentSource(), FakeSource([_lead(1)])],
        retriever=FakeRetriever(),
        chat=_high_fit_chat(),
    )

    assert "silent_board" in stats["by_source"]
    assert "dead" in stats["by_source"]["silent_board"]["verdict"]
    # The productive source is attributed too, so the contrast is visible in one place.
    assert stats["by_source"]["upwork_rss"]["fetched"] == 1


# --------------------------------------------------------------------------- #
# the run cap must not be mistaken for a dead source
# --------------------------------------------------------------------------- #
def test_a_source_starved_by_the_run_cap_is_not_called_dead():
    """`dead` means the source returned nothing. This one returned 30.

    Shipped broken: `fetched` was counted AFTER the run cap, so with a per-source limit
    equal to the cap the first source in registry order filled it and every later source
    read `fetched: 0`. A live run reported six of seven sources dead. A wrong verdict is
    worse than a missing one — it sends you to fix a source that has nothing wrong.
    """
    from pipeline import _per_source_summary

    out = _per_source_summary(
        {"jobicy": {"fetched": 30, "considered": 0, "new": 0, "contactable": 0,
                    "queued": 0, "scores": []}},
        threshold=70,
    )
    assert "dead" not in out["jobicy"]["verdict"]
    assert "starved" in out["jobicy"]["verdict"]
    # Name the lever, not just the symptom.
    assert "max_leads_per_run" in out["jobicy"]["verdict"]


def test_a_source_that_errored_is_reported_broken_not_dead():
    """`dead` blames the market; `broken` blames the code. Opposite fixes.

    `uk_contract` sent Adzuna an invalid filter name and got a 400 on every request for
    the life of the source. Because adapters swallow errors and return [], the report
    said `dead: fetched nothing` — pointing at the queries instead of the one word of
    code that was wrong.
    """
    from pipeline import _per_source_summary

    out = _per_source_summary(
        {"uk_contract": {"fetched": 0, "considered": 0, "new": 0, "contactable": 0,
                         "queued": 0, "scores": [],
                         "error": "HTTPStatusError: 400 Bad Request"}},
        threshold=70,
    )
    verdict = out["uk_contract"]["verdict"]
    assert verdict.startswith("broken")
    assert "dead" not in verdict
    # The digest is the only thing read, so the reason has to travel with the verdict.
    assert "400" in verdict


def test_the_error_verdict_outranks_every_healthy_looking_count():
    """A source can fail *and* have counts, e.g. one query of five was rejected."""
    from pipeline import _per_source_summary

    out = _per_source_summary(
        {"uk_contract": {"fetched": 7, "considered": 7, "new": 7, "contactable": 3,
                         "queued": 1, "scores": [80],
                         "error": "ConnectError: timed out"}},
        threshold=70,
    )
    # Without this, a partial outage hides behind "productive" and is never fixed.
    assert out["uk_contract"]["verdict"].startswith("broken")


def test_run_pipeline_collects_the_error_a_source_reported(temp_db):
    """End-to-end: an adapter's `last_error` has to reach the funnel report.

    The adapter recording it is useless if nothing reads the attribute.
    """
    from pipeline import run_pipeline

    class BrokenSource:
        name = "uk_contract"
        last_error = "HTTPStatusError: 400 Bad Request"

        def fetch(self, limit: int = 50):
            return []

    stats = run_pipeline(
        sources=[BrokenSource(), FakeSource([_lead(1)])],
        retriever=FakeRetriever(),
        chat=_high_fit_chat(),
    )
    assert stats["by_source"]["uk_contract"]["verdict"].startswith("broken")
    assert "400" in stats["by_source"]["uk_contract"]["error"]
    # A healthy source alongside it must not be tarred with the same brush.
    assert not stats["by_source"]["upwork_rss"]["verdict"].startswith("broken")


def test_a_source_that_truly_fetched_nothing_is_still_dead():
    """The starved check must not swallow the real failure it sits next to."""
    from pipeline import _per_source_summary

    out = _per_source_summary(
        {"uk_contract": {"fetched": 0, "considered": 0, "new": 0, "contactable": 0,
                         "queued": 0, "scores": []}},
        threshold=70,
    )
    assert "dead" in out["uk_contract"]["verdict"]


def test_the_run_cap_samples_every_source_instead_of_exhausting_the_first():
    """The cap took a prefix of a registry-ordered concatenation.

    So source #1 consumed the entire budget and the rest were never looked at — the
    cause of the bogus `dead` verdicts. Interleaving makes the cap a sample.
    """
    from pipeline import _interleave_by_source

    first = [_lead(i) for i in range(10)]
    second = [_lead(100 + i) for i in range(10)]
    for lead in second:
        lead.source = "jobicy"

    ordered = _interleave_by_source(first + second)
    sources = {lead.source for lead in ordered[:4]}
    assert sources == {"upwork_rss", "jobicy"}, "cap must see both sources"
    assert len(ordered) == 20, "interleaving must not drop or duplicate leads"


def test_interleaving_preserves_each_sources_own_ordering():
    """Sources return newest-first; reordering within a source would bury fresh leads."""
    from pipeline import _interleave_by_source

    leads = [_lead(1), _lead(2), _lead(3)]
    ordered = _interleave_by_source(leads)
    assert [lead.external_id for lead in ordered] == ["job-1", "job-2", "job-3"]


def test_run_pipeline_counts_fetched_before_the_cap_truncates(temp_db, monkeypatch):
    """End-to-end: with the cap smaller than one source's yield, no source reads dead."""
    import config
    from pipeline import run_pipeline

    real_get = config.get_settings

    def capped():
        s = real_get()
        s.max_leads_per_run = 2
        return s

    monkeypatch.setattr("pipeline.get_settings", capped)

    other = _lead(99)
    other.source = "jobicy"
    stats = run_pipeline(
        sources=[FakeSource([_lead(1), _lead(2), _lead(3)]), FakeSource([other])],
        retriever=FakeRetriever(),
        chat=_high_fit_chat(),
    )

    by_source = stats["by_source"]
    # per_source_limit == the run cap, so the first source alone returns the whole
    # budget (2) — precisely the condition that produced the bogus verdicts.
    assert by_source["upwork_rss"]["fetched"] == 2
    assert by_source["jobicy"]["fetched"] == 1, "fetched is what the source returned"
    # The second source is behind the first in registry order and the cap is already
    # full, so prefix-truncation showed it fetching nothing at all.
    assert "dead" not in by_source["jobicy"]["verdict"]
    assert by_source["jobicy"]["considered"] >= 1, "the cap must sample it"


def test_fit_summary_percentiles_and_bounds():
    from pipeline import _fit_summary

    s = _fit_summary([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], threshold=70)
    assert s["n"] == 10
    assert s["min"] == 10 and s["max"] == 100
    assert 50 <= s["p50"] <= 60
    assert s["p90"] >= 90


def test_fit_summary_handles_empty_and_single():
    from pipeline import _fit_summary

    assert _fit_summary([], threshold=70)["n"] == 0
    single = _fit_summary([73], threshold=70)
    assert single["min"] == single["max"] == single["p50"] == 73
    assert single["passed"] == 1
