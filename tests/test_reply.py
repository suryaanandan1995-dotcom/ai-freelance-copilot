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
def test_run_reply_pass_noop_when_both_gates_off(temp_db, monkeypatch):
    import config
    import reply.runner as runner

    real = config.get_settings

    def off():
        cfg = real()
        cfg.auto_reply = False
        cfg.reply_detection = False
        return cfg

    monkeypatch.setattr(runner, "get_settings", off)
    # fetch_replies must never be called when gated fully off.
    monkeypatch.setattr(
        "reply.inbox.fetch_replies",
        lambda limit=20: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )

    stats = runner.run_reply_pass()
    assert stats == {
        "inbound": 0,
        "replied": 0,
        "detected_only": 0,
        # Replies the owner had already opened: recorded so follow-ups stop, but never
        # auto-answered, so the bot cannot talk over a live human conversation.
        "human_handled": 0,
        "suppressed": 0,
        "skipped": 0,
        "capped": 0,
        "flagged": 0,
    }


# --------------------------------------------------------------------------- #
# 1b. detection is NOT gated on the send flag
# --------------------------------------------------------------------------- #
def _detection_only(monkeypatch):
    """auto_reply OFF, reply_detection ON — the default shipped configuration."""
    import config

    real = config.get_settings

    def s():
        cfg = real()
        cfg.auto_reply = False
        cfg.reply_detection = True
        cfg.smtp_host = "smtp.example.com"
        cfg.smtp_user = "me@example.com"
        cfg.smtp_password = "app-pw"
        return cfg

    for mod in ("reply.runner", "reply.respond", "reply.sender", "reply.inbox"):
        monkeypatch.setattr(f"{mod}.get_settings", s, raising=False)
    return s


def test_a_reply_is_marked_replied_even_with_auto_reply_off(temp_db, monkeypatch):
    """The defect this split exists to prevent.

    Marking a lead replied is what stops the follow-up sequence and what the optimizer
    measures as reply_rate. It used to share one gate with *sending* auto-replies, so
    under the default config (auto_reply off) the inbox was never read at all: prospects
    who answered kept receiving nudges, and reply_rate read 0.0 forever — which is
    indistinguishable from a pitch nobody wants, and points the optimizer at the wrong
    stage of the funnel.
    """
    import reply.runner as runner
    from db.models import OutreachRecord
    from db.session import get_session

    _detection_only(monkeypatch)
    _patch_fetch(monkeypatch, [_inbound("Interested — can you send more detail?")])
    monkeypatch.setattr(
        "reply.sender.send_reply",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    # temp_db already seeds PROSPECT as contacted, with replied defaulting to False.
    with get_session() as session:
        assert session.query(OutreachRecord).filter_by(email=PROSPECT).one().replied is False

    stats = runner.run_reply_pass(chat=FakeChat(responses=["unused"]))

    assert stats["inbound"] == 1
    assert stats["detected_only"] == 1
    assert stats["replied"] == 0  # nothing was auto-answered
    with get_session() as session:
        rec = session.query(OutreachRecord).filter_by(email=PROSPECT).one()
        assert rec.replied is True


def test_detection_only_still_honours_an_opt_out(temp_db, temp_suppress, monkeypatch):
    """Someone who asks to be removed must be suppressed whether or not we auto-answer.

    The opt-out check is deterministic and costs no model call, so there is no reason to
    skip it in detection-only mode — and continuing to email a person who said "stop" is
    the one failure here with legal weight rather than merely commercial weight.
    """
    import reply.runner as runner

    _detection_only(monkeypatch)
    _patch_fetch(monkeypatch, [_inbound("Please unsubscribe me.")])
    monkeypatch.setattr(
        "reply.sender.send_reply",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    stats = runner.run_reply_pass(chat=FakeChat(responses=["unused"]))

    assert stats["suppressed"] == 1
    assert stats["detected_only"] == 0
    assert PROSPECT in temp_suppress.read_text(encoding="utf-8").lower()


def test_fetch_replies_does_not_require_auto_reply(monkeypatch):
    """``fetch_replies`` reads and marks \\Seen; it never sends. Only credentials gate it."""
    import config
    import reply.inbox as inbox

    real = config.get_settings

    def s():
        cfg = real()
        cfg.auto_reply = False
        cfg.reply_detection = True
        cfg.smtp_host = ""  # no credentials -> still a no-op, but for the right reason
        cfg.smtp_user = ""
        return cfg

    monkeypatch.setattr(inbox, "get_settings", s)
    assert inbox.fetch_replies() == []

    calls: list[str] = []
    monkeypatch.setattr(
        inbox, "_known_senders", lambda: calls.append("loaded") or {PROSPECT}
    )

    def s2():
        cfg = s()
        cfg.smtp_host = "smtp.example.com"
        cfg.smtp_user = "me@example.com"
        return cfg

    monkeypatch.setattr(inbox, "get_settings", s2)

    def boom(*a, **k):
        raise OSError("no network in tests")

    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", boom)
    # Got past the gate (loaded known senders) and degraded to [] on the network error
    # rather than raising — the unattended schedule depends on that.
    assert inbox.fetch_replies() == []
    assert calls == ["loaded"]


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


# --------------------------------------------------------------------------- #
# Replies the owner reads first
#
# Measured 2026-08-07, on the first real reply this system ever received: the prospect
# answered at 16:58 UTC, the owner read and answered it by hand at 17:30, and the reply
# runner had last swept at 16:46. Searching UNSEEN only, every later pass saw nothing —
# not temporarily, but permanently, because nothing restores the \Seen flag. The lead
# stayed replied=False, so followup.runner would keep nudging a live conversation.
# --------------------------------------------------------------------------- #
def test_a_reply_the_owner_already_read_still_stops_the_followups(
    temp_db, auto_reply_on, monkeypatch
):
    """The whole point: recorded and marked replied even though it arrived pre-read."""
    import reply.runner as runner
    from db.session import get_session

    _patch_fetch(monkeypatch, [dict(_inbound("Could you share your resume?"),
                                    human_handled=True)])
    monkeypatch.setattr(
        "reply.sender.send_reply",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    stats = runner.run_reply_pass(chat=FakeChat(responses=["unused"]))

    assert stats["inbound"] == 1
    assert stats["human_handled"] == 1
    assert stats["replied"] == 0  # never auto-answered
    with get_session() as session:
        rec = session.query(OutreachRecord).filter_by(email=PROSPECT).one()
        assert rec.replied is True  # <- what stops followup.runner selecting it


def test_the_bot_does_not_talk_over_the_owner_even_with_auto_reply_on(
    temp_db, auto_reply_on, monkeypatch
):
    """Not gated on auto_reply, deliberately.

    A thread the owner has opened is one the owner is handling. Two different voices
    from one address is a worse outcome than no autonomous reply at all, so this holds
    even with autonomous replying fully enabled.
    """
    import reply.runner as runner

    sent: list = []
    _patch_fetch(monkeypatch, [dict(_inbound("What's your rate?"), human_handled=True)])
    monkeypatch.setattr("reply.sender.send_reply", lambda *a, **k: sent.append(a) or True)

    runner.run_reply_pass(chat=FakeChat(responses=["Subject: x\n\nbody"]))

    assert sent == []


def test_an_unread_reply_is_still_auto_answered(temp_db, auto_reply_on, monkeypatch):
    """The mirror guard: the already-read sweep must not disable normal auto-reply."""
    import reply.runner as runner

    sent: list = []
    _patch_fetch(monkeypatch, [dict(_inbound("Tell me more."), human_handled=False)])
    monkeypatch.setattr("reply.sender.send_reply", lambda *a, **k: sent.append(a) or True)

    stats = runner.run_reply_pass(chat=FakeChat(responses=["Subject: x\n\nbody"]))

    assert stats["human_handled"] == 0
    assert stats["replied"] == 1
    assert len(sent) == 1


def test_the_same_read_message_is_not_recorded_twice(temp_db, auto_reply_on, monkeypatch):
    """The already-read sweep re-surfaces the same message on every pass.

    Without message_id dedupe, one reply would inflate the reply rate every two hours
    and — with auto_reply on — be answered repeatedly.
    """
    import reply.runner as runner
    from db.session import get_session

    _patch_fetch(monkeypatch, [dict(_inbound("Same message"), human_handled=True)])
    first = runner.run_reply_pass(chat=FakeChat(responses=["unused"]))
    second = runner.run_reply_pass(chat=FakeChat(responses=["unused"]))

    assert first["inbound"] == 1
    assert second["inbound"] == 0  # deduped on message_id
    with get_session() as session:
        assert session.query(ReplyRecord).filter_by(direction="in").count() == 1
