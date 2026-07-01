"""Offline tests for the auto-reply subsystem (no API key, no network, no SMTP).

Each DB-touching test gets an isolated SQLite database by rebinding
``db.session``'s engine + sessionmaker to a fresh temp file (same pattern as
``test_outreach.py``). ``fetch_replies`` is monkeypatched to hand the runner a
crafted inbound, ``send_reply`` is monkeypatched so nothing leaves the box, and a
``FakeChat`` supplies deterministic drafts.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
from agents.llm import FakeChat
from db.models import Base, OutreachRecord, ReplyRecord

PROSPECT = "prospect@acme.com"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'reply.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    # Seed the prospect as someone we contacted.
    with dbsession.get_session() as session:
        session.add(OutreachRecord(email=PROSPECT, subject="cold email", status="sent"))
    yield engine


@pytest.fixture
def temp_suppress(tmp_path, monkeypatch):
    """Point the suppression list at a temp file for the whole reply subsystem."""
    supp = tmp_path / "suppressed.txt"
    import outreach.suppression as suppression
    import reply.runner as runner

    monkeypatch.setattr(suppression, "SUPPRESSION_PATH", supp)
    monkeypatch.setattr(runner, "SUPPRESSION_PATH", supp)
    return supp


@pytest.fixture
def auto_reply_on(monkeypatch):
    """Flip auto_reply True + SMTP set, everywhere get_settings is imported."""
    import config

    real = config.get_settings

    def s():
        cfg = real()
        cfg.auto_reply = True
        cfg.smtp_host = "smtp.example.com"
        cfg.smtp_user = "me@example.com"
        cfg.smtp_password = "app-pw"
        cfg.max_replies_per_thread = 6
        cfg.standard_rate = ""
        return cfg

    for mod in ("reply.runner", "reply.respond", "reply.sender", "reply.inbox"):
        monkeypatch.setattr(f"{mod}.get_settings", s, raising=False)
    return s


def _inbound(body: str, subject: str = "Re: your project", mid: str = "<abc@acme.com>") -> dict:
    return {
        "from_email": PROSPECT,
        "subject": subject,
        "body": body,
        "message_id": mid,
        "references": None,
    }


def _patch_fetch(monkeypatch, replies: list[dict]):
    monkeypatch.setattr("reply.inbox.fetch_replies", lambda limit=20: list(replies))


def _patch_send_true(monkeypatch):
    """Make send_reply succeed without touching SMTP; capture the calls."""
    calls: list[dict] = []

    def fake_send(to, subject, body, in_reply_to=None, references=None):
        calls.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "in_reply_to": in_reply_to,
                "references": references,
            }
        )
        return True

    monkeypatch.setattr("reply.sender.send_reply", fake_send)
    return calls


# --------------------------------------------------------------------------- #
# 1. master gate off -> no-op
# --------------------------------------------------------------------------- #
def test_run_reply_pass_noop_when_auto_reply_off(temp_db, monkeypatch):
    import config
    import reply.runner as runner

    real = config.get_settings

    def off():
        cfg = real()
        cfg.auto_reply = False
        return cfg

    monkeypatch.setattr(runner, "get_settings", off)
    # fetch_replies must never be called when gated off.
    monkeypatch.setattr(
        "reply.inbox.fetch_replies",
        lambda limit=20: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )

    stats = runner.run_reply_pass()
    assert stats == {"inbound": 0, "replied": 0, "suppressed": 0, "skipped": 0, "capped": 0}


# --------------------------------------------------------------------------- #
# 2. "what's your rate?" -> reply defers pricing to the cal.com link
# --------------------------------------------------------------------------- #
def test_rate_question_defers_to_call_link(temp_db, auto_reply_on, monkeypatch):
    import reply.runner as runner

    draft = (
        "Happy to dig in. Pricing really depends on the specifics, so let's grab "
        "15 minutes and I'll give you a straight answer: "
        "https://cal.com/surya-devsecops/15min\nSurya A"
    )
    _patch_fetch(monkeypatch, [_inbound("Sounds good — what's your rate?")])
    calls = _patch_send_true(monkeypatch)

    stats = runner.run_reply_pass(chat=FakeChat(responses=[draft]))

    assert stats["inbound"] == 1
    assert stats["replied"] == 1
    assert len(calls) == 1
    assert "cal.com" in calls[0]["body"]


# --------------------------------------------------------------------------- #
# 3. "unsubscribe" -> suppress + address appended to temp suppressed.txt
# --------------------------------------------------------------------------- #
def test_unsubscribe_suppresses_and_appends(temp_db, temp_suppress, auto_reply_on, monkeypatch):
    import reply.runner as runner

    _patch_fetch(monkeypatch, [_inbound("Please unsubscribe me, not interested.")])
    _patch_send_true(monkeypatch)

    stats = runner.run_reply_pass(chat=FakeChat(responses=["unused"]))

    assert stats["suppressed"] == 1
    assert stats["replied"] == 0
    assert temp_suppress.exists()
    assert PROSPECT in temp_suppress.read_text(encoding="utf-8").lower()


# --------------------------------------------------------------------------- #
# 4. per-thread cap -> new inbound skipped once the cap is reached
# --------------------------------------------------------------------------- #
def test_per_thread_cap_skips(temp_db, auto_reply_on, monkeypatch):
    import reply.runner as runner

    # Pre-seed max_replies_per_thread (6) outbound records for this prospect.
    with dbsession.get_session() as session:
        for _ in range(6):
            session.add(ReplyRecord(email=PROSPECT, direction="out", subject="prior"))

    _patch_fetch(monkeypatch, [_inbound("One more question about the timeline?")])
    calls = _patch_send_true(monkeypatch)

    stats = runner.run_reply_pass(chat=FakeChat(responses=["should not send"]))

    assert stats["capped"] == 1
    assert stats["replied"] == 0
    assert calls == []  # nothing sent


# --------------------------------------------------------------------------- #
# 5. normal question -> an outbound ReplyRecord is written
# --------------------------------------------------------------------------- #
def test_normal_question_writes_outbound_record(temp_db, auto_reply_on, monkeypatch):
    import reply.runner as runner

    draft = (
        "Yep, I've hardened EKS clusters exactly like that. Want to walk through "
        "the specifics on a quick call? https://cal.com/surya-devsecops/15min\nSurya A"
    )
    _patch_fetch(monkeypatch, [_inbound("Do you have experience with EKS hardening?")])
    _patch_send_true(monkeypatch)

    stats = runner.run_reply_pass(chat=FakeChat(responses=[draft]))

    assert stats["replied"] == 1
    with dbsession.get_session() as session:
        ins = session.query(ReplyRecord).filter_by(direction="in").all()
        outs = session.query(ReplyRecord).filter_by(direction="out").all()
        assert len(ins) == 1
        assert len(outs) == 1
        assert outs[0].email == PROSPECT
        assert outs[0].snippet


# --------------------------------------------------------------------------- #
# 6. send_reply no-ops when auto_reply off / smtp empty
# --------------------------------------------------------------------------- #
def test_send_reply_noop_when_auto_reply_off(monkeypatch):
    import config
    import reply.sender as sender

    real = config.get_settings

    def s():
        cfg = real()
        cfg.auto_reply = False
        cfg.smtp_host = "smtp.example.com"
        return cfg

    monkeypatch.setattr(sender, "get_settings", s)
    assert sender.send_reply("a@b.com", "Re: hi", "body") is False


def test_send_reply_noop_when_smtp_host_empty(monkeypatch):
    import config
    import reply.sender as sender

    real = config.get_settings

    def s():
        cfg = real()
        cfg.auto_reply = True
        cfg.smtp_host = ""
        return cfg

    monkeypatch.setattr(sender, "get_settings", s)
    assert sender.send_reply("a@b.com", "Re: hi", "body") is False


# --------------------------------------------------------------------------- #
# 7. classify_and_draft: rate question keeps the cal.com link; subject gets "Re:"
# --------------------------------------------------------------------------- #
def test_classify_and_draft_reply_shape(auto_reply_on):
    from reply.respond import classify_and_draft

    draft = "Depends on scope — let's talk: https://cal.com/surya-devsecops/15min\nSurya A"
    res = classify_and_draft(
        PROSPECT, "What would this cost me?", chat=FakeChat(responses=[draft])
    )
    assert res["action"] == "reply"
    assert res["subject"].lower().startswith("re:")
    assert "cal.com" in res["body"]


# --------------------------------------------------------------------------- #
# 8. classify_and_draft: opt-out short-circuits to suppress without a model call
# --------------------------------------------------------------------------- #
def test_classify_and_draft_optout_suppress(auto_reply_on):
    from reply.respond import classify_and_draft

    def boom(_messages):
        raise AssertionError("model should not be called for an opt-out")

    chat = FakeChat()
    chat.invoke = boom  # type: ignore[assignment]

    res = classify_and_draft(PROSPECT, "unsubscribe please", chat=chat)
    assert res["action"] == "suppress"
