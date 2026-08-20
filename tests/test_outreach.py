"""Offline tests for the auto-email outreach subsystem (no API key, no network).

Each test that touches the DB gets its own isolated SQLite database by rebinding
``db.session``'s engine + sessionmaker to a fresh temp file. SMTP is never hit:
``send_outreach`` is monkeypatched (or returns False because nothing is config).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
from agents.llm import FakeChat
from core.schemas import CompanyResearch, Lead, ScoredLead
from db.models import Base, OutreachRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
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


def _lead(i: int, *, desc: str | None = None, raw: dict | None = None) -> Lead:
    return Lead(
        source="hn_hiring",
        external_id=f"job-{i}",
        title=f"Kubernetes + DevSecOps hardening #{i}",
        description=(
            desc
            if desc is not None
            else "Secure our EKS clusters. Email jobs@acme.io to apply."
        ),
        company="Acme Corp",
        budget="$90/hr",
        tags=["kubernetes", "devsecops"],
        raw=raw or {},
    )


def _scored(lead: Lead, fit: int = 90) -> ScoredLead:
    return ScoredLead(
        lead=lead,
        fit_score=fit,
        reasons=["strong k8s + devsecops fit"],
        matched_projects=["multi-cloud-k8s-terraform"],
    )


def _route_structured(messages):
    system = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
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


_EMAIL_BODY = (
    "Subject: Hardening your EKS clusters\n\n"
    "Saw you're working on securing EKS clusters and tightening your CI/CD "
    "pipelines, which is squarely what I do. I've done exactly this kind of "
    "work on multi-cloud-k8s-terraform, where I cut cloud cost 40% while "
    "keeping the security gates green and the developers happy. I'd audit your "
    "clusters, add policy-as-code guardrails, and wire signed supply-chain "
    "checks into the pipeline, shipping in small reviewable increments so you "
    "stay in control the whole way through. If any of that sounds useful, I'm "
    "happy to walk through a concrete plan on a short call: "
    "https://cal.com/surya-devsecops/15min\n"
    "Surya A — https://suryaanandan1995-dotcom.github.io"
)


def _email_chat() -> FakeChat:
    return FakeChat(
        responses=[_EMAIL_BODY] * 6,
        structured=_route_structured,
    )


class FakeSource:
    name = "hn_hiring"

    def __init__(self, leads):
        self._leads = leads

    def fetch(self, limit: int = 50):
        return list(self._leads[:limit])


# --------------------------------------------------------------------------- #
# extract
#
# Smoke coverage only. *Which* of several addresses in a long JD we may write to —
# and the four measured institutional inboxes that used to pass this gate — lives in
# tests/test_contact_targeting.py.
# --------------------------------------------------------------------------- #
def test_find_contact_email_extracts_real_email():
    from outreach.extract import find_contact_email

    lead = _lead(1, desc="We're hiring SRE. Reach me at hiring@startup.dev today.")
    assert find_contact_email(lead) == "hiring@startup.dev"


def test_find_contact_email_from_raw():
    from outreach.extract import find_contact_email

    lead = _lead(1, desc="No address in the body.", raw={"comment": "ping ceo@foo.co"})
    assert find_contact_email(lead) == "ceo@foo.co"


def test_find_contact_email_rejects_noreply_and_none():
    from outreach.extract import find_contact_email

    bad = _lead(1, desc="Auto from no-reply@news.ycombinator.com only.", raw={})
    assert find_contact_email(bad) is None

    none = _lead(2, desc="No contact details here at all.", raw={})
    assert find_contact_email(none) is None


# --------------------------------------------------------------------------- #
# pitch
# --------------------------------------------------------------------------- #
def test_draft_email_returns_subject_and_body_with_project():
    from outreach.pitch import draft_email

    lead = _lead(1)
    draft = draft_email(
        _scored(lead), CompanyResearch(), retriever=FakeRetriever(), chat=_email_chat()
    )
    assert draft["subject"] and "Subject:" not in draft["subject"]
    assert "multi-cloud-k8s-terraform" in draft["body"]
    assert "!" not in draft["subject"]


# --------------------------------------------------------------------------- #
# sender (no real SMTP)
# --------------------------------------------------------------------------- #
def test_send_outreach_noop_when_auto_email_off(monkeypatch):
    import config
    import outreach.sender as sender

    real = config.get_settings

    def s():
        cfg = real()
        cfg.auto_email = False
        cfg.smtp_host = "smtp.example.com"
        return cfg

    monkeypatch.setattr(sender, "get_settings", s)
    assert sender.send_outreach("a@b.com", "hi", "body") is False


def test_send_outreach_noop_when_smtp_host_empty(monkeypatch):
    import config
    import outreach.sender as sender

    real = config.get_settings

    def s():
        cfg = real()
        cfg.auto_email = True
        cfg.smtp_host = ""
        return cfg

    monkeypatch.setattr(sender, "get_settings", s)
    assert sender.send_outreach("a@b.com", "hi", "body") is False


# --------------------------------------------------------------------------- #
# pipeline integration
# --------------------------------------------------------------------------- #
def _patch_send_true(monkeypatch):
    """Make the pipeline's send_outreach succeed without touching SMTP."""
    import outreach.sender as sender

    monkeypatch.setattr(sender, "send_outreach", lambda to, subject, body: True)


def test_run_pipeline_auto_email_writes_outreach_record(temp_db, monkeypatch):
    from pipeline import run_pipeline

    _patch_send_true(monkeypatch)
    sources = [FakeSource([_lead(1)])]
    stats = run_pipeline(
        sources=sources,
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["queued"] == 1
    assert stats["emailed"] == 1
    with dbsession.get_session() as session:
        rows = session.query(OutreachRecord).all()
        assert len(rows) == 1
        assert rows[0].email == "jobs@acme.io"
        assert rows[0].status == "sent"
        assert rows[0].subject


def test_run_pipeline_auto_email_dedupes_across_runs(temp_db, monkeypatch):
    from pipeline import run_pipeline

    _patch_send_true(monkeypatch)
    # Pre-seed an OutreachRecord for the address the lead exposes.
    with dbsession.get_session() as session:
        session.add(OutreachRecord(email="jobs@acme.io", subject="prior", status="sent"))

    sources = [FakeSource([_lead(1)])]
    stats = run_pipeline(
        sources=sources,
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["emailed"] == 0
    assert stats["emailed_skipped"].get("duplicate") == 1
    with dbsession.get_session() as session:
        assert session.query(OutreachRecord).count() == 1  # no duplicate row


def test_run_pipeline_auto_email_respects_daily_cap(temp_db, monkeypatch):
    import config
    from pipeline import run_pipeline

    _patch_send_true(monkeypatch)

    real = config.get_settings

    def capped():
        cfg = real()
        cfg.max_emails_per_day = 1
        cfg.outreach_min_fit = 80
        return cfg

    monkeypatch.setattr("pipeline.get_settings", capped)

    # Two distinct leads with two distinct contact emails.
    l1 = _lead(1, desc="Secure EKS. Email a@one.io to apply.")
    l2 = _lead(2, desc="Secure EKS. Email b@two.io to apply.")
    stats = run_pipeline(
        sources=[FakeSource([l1, l2])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["emailed"] == 1
    assert stats["emailed_skipped"].get("daily_cap") == 1
    with dbsession.get_session() as session:
        assert session.query(OutreachRecord).filter_by(status="sent").count() == 1


def test_run_pipeline_auto_email_skips_suppressed(temp_db, monkeypatch, tmp_path):
    from pipeline import run_pipeline

    _patch_send_true(monkeypatch)
    # Point the suppression list at a temp file containing the lead's address.
    supp = tmp_path / "suppressed.txt"
    supp.write_text("jobs@acme.io\n", encoding="utf-8")
    import outreach.suppression as suppression

    monkeypatch.setattr(suppression, "SUPPRESSION_PATH", supp)

    stats = run_pipeline(
        sources=[FakeSource([_lead(1)])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["emailed"] == 0
    assert stats["emailed_skipped"].get("suppressed") == 1
    with dbsession.get_session() as session:
        assert session.query(OutreachRecord).count() == 0


def test_run_pipeline_auto_email_skips_low_fit(temp_db, monkeypatch):
    import config
    from pipeline import run_pipeline

    _patch_send_true(monkeypatch)

    real = config.get_settings

    def cfg_fn():
        cfg = real()
        cfg.outreach_min_fit = 95  # above the lead's 90 fit
        return cfg

    monkeypatch.setattr("pipeline.get_settings", cfg_fn)

    stats = run_pipeline(
        sources=[FakeSource([_lead(1)])],
        retriever=FakeRetriever(),
        chat=_email_chat(),
        auto_email=True,
    )

    assert stats["emailed"] == 0
    assert stats["emailed_skipped"].get("low_fit") == 1


# --------------------------------------------------------------------------- #
# Attribution: a send must be identifiable afterwards
#
# Until 2026-08-20 a successful send logged NOTHING — only failures named the
# recipient. That morning a cal.com call was booked the day after a run reporting
# 'emailed': 1, and no readable source could say which company had been mailed: the
# run log listed the four *proposed* (never-sent) addresses and the rejected post,
# the sent one was the only address absent, and the database holding it is reachable
# only with a DSN kept as a repo secret.
#
# The constraint that shapes the fix: this repository is PUBLIC, so its Actions logs
# are public. Logging the full address would publish a prospect's personal data and
# feed scrapers. The domain answers "which company?" and was published by the poster
# themselves; the local part is masked.
# --------------------------------------------------------------------------- #
def test_masking_keeps_the_company_and_drops_the_person():
    from outreach.sender import mask_address

    assert mask_address("HR@bactrix.com") == "h***@bactrix.com"
    assert mask_address("hello@proxify.ai") == "h***@proxify.ai"
    # Case-normalised so the same mailbox never reads as two different companies.
    assert mask_address("Team@Infisical.COM") == "t***@infisical.com"


def test_masking_never_reveals_the_local_part_however_short():
    """A one-character local part must not become the whole address."""
    from outreach.sender import mask_address

    masked = mask_address("a@example.com")
    assert masked == "a***@example.com"
    # The point of the mask is that the reader cannot reconstruct the mailbox: the
    # only literal local-part character is the first, and *** stands for the rest
    # whether the rest exists or not.
    assert "***" in masked


def test_masking_degrades_safely_on_junk():
    """Never raise inside a log call, and never echo a full unknown string."""
    from outreach.sender import mask_address

    assert mask_address("") == "(none)"
    assert mask_address(None) == "(none)"  # type: ignore[arg-type]
    assert mask_address("   ") == "(none)"
    # Not an address shape: keep one character so a typo stays recognisable in the
    # log without printing whatever the value actually was.
    assert mask_address("notanaddress") == "n***"


def test_a_successful_send_names_the_recipient_domain_in_the_log(monkeypatch, caplog):
    """The whole point: 'emailed: 1' must be traceable to a company from the log alone."""
    import logging

    from config import Settings
    from outreach import sender

    # sender.py does `from config import get_settings`, so the module-level name is
    # what must be patched — patching config.get_settings leaves the bound reference.
    monkeypatch.setattr(
        sender,
        "get_settings",
        lambda: Settings(auto_email=True, smtp_host="smtp.example.com", smtp_user="",
                         smtp_password="", owner_email="me@x.io"),
    )

    class FakeSMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            return None

        def starttls(self):
            return None

        def send_message(self, msg):
            return {}

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    with caplog.at_level(logging.INFO, logger="outreach.sender"):
        assert sender.send_outreach("HR@bactrix.com", "Quick question", "hi") is True

    logged = caplog.text
    assert "bactrix.com" in logged           # the company is identifiable
    assert "HR@bactrix.com" not in logged    # the address itself is not published
    assert "Quick question" in logged        # and the subject ties it to the thread


def test_a_failed_send_also_masks_the_recipient(monkeypatch, caplog):
    """The failure path predates the mask and leaked the full address into a public log."""
    import logging
    import smtplib

    from config import Settings
    from outreach import sender

    monkeypatch.setattr(
        sender,
        "get_settings",
        lambda: Settings(auto_email=True, smtp_host="smtp.example.com", owner_email="me@x.io"),
    )

    def boom(host, port):
        raise smtplib.SMTPException("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", boom)

    with caplog.at_level(logging.WARNING, logger="outreach.sender"):
        assert sender.send_outreach("HR@bactrix.com", "s", "b") is False

    assert "bactrix.com" in caplog.text
    assert "HR@bactrix.com" not in caplog.text
