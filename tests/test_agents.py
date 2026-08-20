"""Offline unit tests for the individual agents (no API key, no network)."""
from __future__ import annotations

from agents.compliance import review
from agents.followup import draft_followup
from agents.llm import FakeChat
from agents.proposal_writer import QUANTIFIED_WINS, write_proposal
from agents.qualifier import qualify
from agents.researcher import research
from core.schemas import (
    CompanyResearch,
    Lead,
    ProposalDraft,
    ScoredLead,
)


class FakeRetriever:
    """Minimal retriever: ``.retrieve(q, k)`` -> list of {text, source, score}."""

    def __init__(self, chunks=None):
        self._chunks = chunks or [
            {
                "text": "Cut multi-cloud spend 40% with reusable Terraform modules.",
                "source": "multi-cloud-k8s-terraform",
                "score": 0.91,
            },
            {
                "text": "Shipped an LLM guardrails gateway blocking unsafe prompts.",
                "source": "llm-guardrails-gateway",
                "score": 0.88,
            },
        ]

    def retrieve(self, query, k=3):
        return self._chunks[:k]


def _lead(**kw):
    base = dict(
        source="upwork_rss",
        external_id="job-123",
        title="Kubernetes platform + DevSecOps pipeline hardening",
        description="Need help securing our EKS clusters and CI/CD with Terraform.",
        company="Acme Corp",
        budget="$90/hr",
        tags=["kubernetes", "devsecops", "terraform"],
    )
    base.update(kw)
    return Lead(**base)


def test_qualifier_scores_and_matches_projects():
    chat = FakeChat(
        structured={
            "lead": _lead().model_dump(),
            "fit_score": 88,
            "reasons": ["strong k8s + devsecops overlap"],
            "matched_projects": ["multi-cloud-k8s-terraform", "not-a-real-repo"],
        }
    )
    scored = qualify(_lead(), chat=chat)
    assert isinstance(scored, ScoredLead)
    assert scored.fit_score == 88
    assert scored.reasons
    # Unknown repo names are filtered out, valid ones kept.
    assert scored.matched_projects == ["multi-cloud-k8s-terraform"]


def test_qualifier_shows_the_model_the_portfolio_it_is_scoring_against():
    """`qualify` used to accept a retriever and throw it away.

    Its docstring said "unused here" while `build_graph` passed one in on every call.
    So the one agent whose entire job is a *comparison* held only one side of it: a
    list of repo names, with no evidence of what those repos are. Both agents that
    merely write prose already retrieved real chunks. A lead asking for exactly what
    `rag-platform-k8s` proves got no lift from it.
    """
    captured: list[str] = []

    class RecordingChat(FakeChat):
        def with_structured_output(self, schema):
            # FakeChat.with_structured_output returns a COPY, so overriding invoke on
            # this class alone records nothing — the same trap CountingChat documents
            # in test_contact_gate.py. Reach into the clone instead.
            clone = super().with_structured_output(schema)

            class _Recording(type(clone)):  # pragma: no cover - thin shim
                def invoke(self, messages):
                    captured.append(messages[-1]["content"])
                    return super().invoke(messages)

            clone.__class__ = _Recording
            return clone

    chat = RecordingChat(
        structured={
            "lead": _lead().model_dump(),
            "fit_score": 88,
            "reasons": ["k8s overlap"],
            "matched_projects": [],
        }
    )
    qualify(_lead(), retriever=FakeRetriever(), chat=chat)

    assert captured, "the qualifier never called the model"
    user_msg = captured[-1]
    assert "Portfolio evidence" in user_msg
    # The retrieved chunk text itself must reach the prompt, not just its name.
    assert "reusable Terraform modules" in user_msg


def test_qualifier_survives_a_missing_portfolio_store():
    """A KB that cannot be read must score conservatively, never fail the run.

    The whole pipeline is unattended; an exception here would stop a scheduled run
    over a degraded index rather than a real error.
    """

    class BrokenRetriever:
        def retrieve(self, query, k=5):
            raise RuntimeError("index corrupt")

    chat = FakeChat(
        structured={
            "lead": _lead().model_dump(),
            "fit_score": 55,
            "reasons": ["no evidence available"],
            "matched_projects": [],
        }
    )
    scored = qualify(_lead(), retriever=BrokenRetriever(), chat=chat)
    assert scored.fit_score == 55


def test_researcher_returns_enrichment():
    chat = FakeChat(
        structured={
            "summary": "Acme runs EKS and wants CI/CD security hardening.",
            "tech_stack": ["EKS", "Terraform", "GitHub Actions"],
            "pain_points": ["insecure pipelines", "cluster drift"],
            "contacts": ["Jane (CTO)"],
        }
    )
    enrichment = research(_lead(), chat=chat)
    assert isinstance(enrichment, CompanyResearch)
    assert "EKS" in enrichment.tech_stack
    assert enrichment.pain_points


def test_proposal_writer_cites_a_project():
    scored = ScoredLead(
        lead=_lead(),
        fit_score=88,
        reasons=["k8s + devsecops"],
        matched_projects=["multi-cloud-k8s-terraform"],
    )
    enrichment = CompanyResearch(
        summary="Acme runs EKS.",
        tech_stack=["EKS", "Terraform"],
        pain_points=["insecure pipelines"],
    )
    body = (
        "Hi Acme — I have hardened EKS clusters and CI/CD before. My "
        "multi-cloud-k8s-terraform project cut infra cost 40% and gave 50% faster "
        "deploys. I'd love to help secure your pipelines. Happy to chat: "
        "https://cal.com/surya-devsecops/15min"
    )
    chat = FakeChat(responses=[body])
    draft = write_proposal(scored, enrichment, retriever=FakeRetriever(), chat=chat)
    assert isinstance(draft, ProposalDraft)
    # RAG step cited a real portfolio project that appears in the body.
    assert "multi-cloud-k8s-terraform" in draft.cited_projects
    assert "multi-cloud-k8s-terraform" in draft.body
    assert any(win.split()[0] in draft.body for win in QUANTIFIED_WINS)


def _good_draft():
    body = (
        "Hi Acme, I have spent years hardening Kubernetes platforms and CI/CD "
        "pipelines. On multi-cloud-k8s-terraform I cut infrastructure cost 40% "
        "and lifted deploy frequency 75% while keeping security gates green. I can "
        "audit your EKS setup, add policy-as-code guardrails, and wire signed "
        "supply-chain checks into your pipeline. I work transparently and ship in "
        "small reviewable increments so you stay in control the whole time. If "
        "useful, I'm happy to walk through a concrete plan on a short call at your "
        "convenience — no pressure either way."
    )
    return ProposalDraft(
        lead_external_id="job-123",
        title="Proposal: Kubernetes platform hardening",
        body=body,
        suggested_rate="$90/hr",
        cited_projects=["multi-cloud-k8s-terraform"],
    )


def test_compliance_approves_a_good_draft():
    verdict = review(_good_draft())
    assert verdict.approved is True
    assert verdict.issues == []
    assert verdict.quality_score >= 90


def test_compliance_rejects_spam():
    spam = ProposalDraft(
        lead_external_id="job-999",
        title="Proposal: anything",
        body="Dear Sir/Madam, I can do this easily for the cheapest price. Buy now!",
        suggested_rate="$5/hr",
        cited_projects=[],  # no project cited -> generic
    )
    verdict = review(spam)
    assert verdict.approved is False
    assert any("generic" in i for i in verdict.issues)
    assert any("forbidden" in i for i in verdict.issues)


def test_compliance_detects_duplicate():
    draft = _good_draft()
    key = f"{draft.lead_external_id}:{draft.title}".lower()
    verdict = review(draft, existing_keys={key})
    assert verdict.is_duplicate is True
    assert verdict.approved is False


def test_followup_is_short_and_nonempty():
    chat = FakeChat(responses=["Hi Acme, just circling back on the EKS work — "
                               "no rush, happy to help whenever it's useful."])
    msg = draft_followup(_lead(), days_since=5, chat=chat)
    assert isinstance(msg, str) and msg.strip()


# --------------------------------------------------------------------------- #
# qualifier: citable portfolio projects are derived, not hand-maintained
# --------------------------------------------------------------------------- #
def test_portfolio_projects_come_from_the_rag_store():
    """The hard-coded list had drifted: 4 names, one of which did not exist, while
    8 real repos (including every AI-infra one) were missing. Since matched_projects
    is filtered against this list, the qualifier could not cite its best evidence."""
    from agents.qualifier import portfolio_projects

    projects = portfolio_projects()
    assert len(projects) >= 10
    # AI-infra repos — the premium segment — must be citable.
    for repo in ("rag-platform-k8s", "llm-inference-k8s", "agentic-sre-platform"):
        assert repo in projects
    # The phantom repo must never come back.
    assert "devsecops-pipeline-templates" not in projects


def test_portfolio_projects_exclude_non_repo_kb_sources():
    """The KB also holds achievements and won-project notes; offering those as
    "repos" would put a non-existent link in a proposal."""
    from agents.qualifier import portfolio_projects

    projects = portfolio_projects()
    assert "achievements" not in projects
    assert not any(":" in p for p in projects)  # e.g. "won:ext-1"


def test_portfolio_projects_falls_back_when_store_missing(monkeypatch):
    import config
    from agents import qualifier

    real = config.get_settings

    def s():
        cfg = real()
        cfg.rag_store_path = "/nonexistent/kb.json"
        return cfg

    monkeypatch.setattr(qualifier, "get_settings", s)
    projects = qualifier.portfolio_projects()
    assert projects == qualifier._FALLBACK_PROJECTS
    assert "rag-platform-k8s" in projects


def test_qualifier_prompt_names_the_target_segments():
    """A lead is scored against this prompt; omitting a segment scores it low."""
    from agents.qualifier import SKILLS, _system_prompt, portfolio_projects

    prompt = _system_prompt(portfolio_projects())
    low = (SKILLS + prompt).lower()
    for segment in ("llm", "agent", "forward-deployed", "kubernetes", "devsecops"):
        assert segment in low
    # And it must warn off the measured false positives.
    assert "customer service" in prompt.lower()


# --------------------------------------------------------------------------- #
# the rubric is linted, because a score distribution cannot be
# --------------------------------------------------------------------------- #
def test_qualifier_rubric_anchors_the_scale():
    """An unanchored 0-100 scale plus a list of deductions makes an LLM score low.

    Measured: p50 28, p90 58 across three production runs, with the same model that
    had scored 13 of 13 leads at 72-88 in July. The prompt told the model only what 0
    and 100 mean, then gave it two things to deduct for — so the bands below are the
    fix, and this lint is what keeps them from being deleted as verbose. A prompt lint
    is checkable offline; the distribution it protects needs a live run and a week.
    """
    from agents.qualifier import _system_prompt, portfolio_projects

    prompt = _system_prompt(portfolio_projects())
    # The threshold the code actually enforces must be stated to the model.
    assert "70" in prompt
    # Named bands, not just the endpoints.
    for band in ("90-100", "75-89", "60-74", "40-59", "20-39", "0-19"):
        assert band in prompt, f"missing anchor band {band}"


def test_qualifier_rubric_does_not_penalise_missing_information():
    """Only 1 of 7 adapters sets `budget`, so the prompt reads "Budget: unknown".

    That made the rubric asymmetric — every reason to deduct was checkable in the
    input, and the headline reason to award (day-rate contract work) was not. Terse
    board listings were being capped low for being terse.
    """
    from agents.qualifier import _system_prompt, portfolio_projects

    low = _system_prompt(portfolio_projects()).lower()
    assert "missing information is not a negative" in low
    assert "must never cap" in low
    # Permanent framing must not subtract: these segments hire perm and contract-to-hire.
    assert "does not subtract" in low


def test_qualifier_rubric_scores_remote_roles_worldwide_not_just_the_uk():
    """The prompt used to deduct for "on-site presence outside the UK".

    That was written when the only source was a UK feed. Sourcing now spans ten
    country endpoints across the US, EU and ANZ, every one queried with a remote
    filter — so the clause penalised every lead from the nine new markets for being
    foreign, which is the opposite of what the expansion was for. A gate must not
    fight the product it is part of.
    """
    from agents.qualifier import _system_prompt, portfolio_projects

    prompt = _system_prompt(portfolio_projects())
    assert "outside the UK" not in prompt
    low = prompt.lower()
    assert "remotely" in low or "remote" in low
    assert "neither is the country" in low


def test_the_model_is_never_asked_to_echo_the_lead_back():
    """The crash of 2026-08-20, as a test.

    ``qualify`` overwrites the model's ``lead`` with the one it was passed, so asking for
    it bought nothing and cost a validation surface: on production run 32339343714 Sonnet
    returned ``lead`` as a JSON *string* and omitted ``fit_score``, pydantic raised inside
    ``with_structured_output``, and the exception ended a 39-minute, $2.46 run — digest,
    five apply packs and four proposed addresses included.

    Asserting on the SCHEMA rather than on a happy path, because the bug was not that a
    field parsed wrongly. It was that the field was requested at all.
    """
    from core.schemas import FitVerdict

    seen: list[object] = []

    class SchemaSpy(FakeChat):
        def with_structured_output(self, schema, **kw):
            seen.append(schema)
            return super().with_structured_output(schema, **kw)

    chat = SchemaSpy(structured={"fit_score": 74, "reasons": ["ok"], "matched_projects": []})
    scored = qualify(_lead(), chat=chat)

    assert seen == [FitVerdict], "the wire schema must be the verdict, not the whole lead"
    assert "lead" not in FitVerdict.model_fields
    # The lead still arrives intact on the way out — attached locally, not parsed back.
    assert scored.lead.external_id == "job-123"
    assert scored.fit_score == 74


def test_a_verdict_without_the_lead_field_is_enough_to_score():
    """Exactly the payload that used to fail: scoring fields only, no ``lead`` key."""
    chat = FakeChat(structured={"fit_score": 91, "reasons": ["k8s"], "matched_projects": []})
    scored = qualify(_lead(), chat=chat)
    assert scored.fit_score == 91
    assert scored.lead.title.startswith("Kubernetes")


# --------------------------------------------------------------------------- #
# The model stringifies list fields. Decode, don't discard.
#
# Narrowing the wire schema to FitVerdict stopped the 2026-08-20 crash from taking the
# whole run down, and the very next run proved the habit itself had not gone away:
#
#   WARNING pipeline: lead failed, skipping it: https://www.adzuna.de/details/5845898630
#   (ValidationError: 1 validation error for FitVerdict
#    reasons  Input should be a valid list
#    [type=list_type, input_value='["Core requirement is Sc...qualifier on its own."]'])
#
# The content was right and the encoding was one layer too deep. Refusing it discards a
# Sonnet call that already succeeded, so the boundary decodes: lenient parser at the
# edge, strict type inside.
# --------------------------------------------------------------------------- #
def test_a_json_encoded_list_of_reasons_is_decoded_not_rejected():
    from core.schemas import FitVerdict

    verdict = FitVerdict(
        fit_score=82,
        reasons='["Core requirement is Scala", "Remote-friendly"]',
        matched_projects='["ai-freelance-copilot"]',
    )
    assert verdict.reasons == ["Core requirement is Scala", "Remote-friendly"]
    assert verdict.matched_projects == ["ai-freelance-copilot"]


def test_a_single_bare_sentence_becomes_one_reason():
    """A real answer in the wrong container is still a real answer."""
    from core.schemas import FitVerdict

    assert FitVerdict(fit_score=50, reasons="Strong Kubernetes overlap").reasons == [
        "Strong Kubernetes overlap"
    ]


def test_prose_that_merely_starts_with_a_bracket_is_kept_verbatim():
    """Looked like JSON, was not: keep the text rather than lose the judgement."""
    from core.schemas import FitVerdict

    verdict = FitVerdict(fit_score=40, reasons="[unparseable, no closing bracket")
    assert verdict.reasons == ["[unparseable, no closing bracket"]


def test_null_and_empty_collapse_to_no_reasons_rather_than_a_literal():
    from core.schemas import FitVerdict

    assert FitVerdict(fit_score=10, reasons=None).reasons == []
    assert FitVerdict(fit_score=10, reasons="").reasons == []
    assert FitVerdict(fit_score=10, reasons="   ").reasons == []


def test_a_proper_list_is_untouched():
    """The coercion is a fallback, not a rewrite of the happy path."""
    from core.schemas import FitVerdict

    assert FitVerdict(fit_score=90, reasons=["a", "b"]).reasons == ["a", "b"]


def test_non_string_items_inside_a_stringified_list_are_stringified():
    """[1, 2] decodes to a list; list[str] must not then fail on the ints."""
    from core.schemas import FitVerdict

    assert FitVerdict(fit_score=70, reasons="[1, 2]").reasons == ["1", "2"]


def test_fit_score_stays_required_and_bounded():
    """Leniency is scoped to encoding. A missing or absurd score is still a defect —
    it is the one field the caller cannot reconstruct, so guessing it would be worse
    than failing the lead."""
    import pydantic
    import pytest as _pytest

    from core.schemas import FitVerdict

    with _pytest.raises(pydantic.ValidationError):
        FitVerdict(reasons=["x"])  # type: ignore[call-arg]
    with _pytest.raises(pydantic.ValidationError):
        FitVerdict(fit_score=140)
