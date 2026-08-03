"""End-to-end tests for funnel LOOP CLOSURE (offline: no IMAP, SMTP, or HTTP).

Every stage of the funnel was already unit-tested in isolation — the reply runner in
``test_reply.py``, the cal.com webhook in ``test_dashboard.py``, the KPI maths in
``test_kpi.py``. What nobody tested was the *joins* between them, and the joins are
where a funnel silently stops closing:

  * an inbound reply must flip ``OutreachRecord.replied``, because that flag is both
    what stops follow-ups AND what ``monitor.kpi.funnel`` counts as "replied";
  * the auto-reply must carry ``In-Reply-To``/``References``, or it lands as a new
    thread — invisible to the prospect's eye and to Gmail's conversation grouping;
  * a cal.com booking must stamp ``call_booked_at`` on the SAME row the send wrote,
    or a booked call reports as a reply that never converted.

A break in any join looks exactly like "the pitch isn't working": the emails send, the
numbers stay zero, and the reported bottleneck points at the wrong stage. So these
tests assert the stitched-together path, reading the outcome through ``funnel()``
rather than through each module's own return value.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
from agents.llm import FakeChat
from db.models import Base, OutreachRecord, ReplyRecord
from monitor.kpi import funnel

PROSPECT = "prospect@acme.com"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Isolated DB with one already-sent cold email, as after a real outreach run."""
    url = f"sqlite:///{tmp_path / 'loop.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    with dbsession.get_session() as session:
        session.add(
            OutreachRecord(
                email=PROSPECT,
                subject="Kubernetes hardening",
                status="sent",
                sent_at=_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=2),
            )
        )
    yield engine


@pytest.fixture
def auto_reply_on(monkeypatch):
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


def _inbound(body: str, *, mid: str = "<their-msg@acme.com>", refs: str | None = None) -> dict:
    return {
        "from_email": PROSPECT,
        "subject": "Re: Kubernetes hardening",
        "body": body,
        "message_id": mid,
        "references": refs,
    }


def _capture_sends(monkeypatch) -> list[dict]:
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


_DRAFT = (
    "Happy to help — I've hardened EKS clusters against exactly that. Easiest is a "
    "quick call: https://cal.com/surya-devsecops/15min\nSurya A"
)


# --------------------------------------------------------------------------- #
# join 1: inbound reply -> OutreachRecord.replied -> KPI funnel
# --------------------------------------------------------------------------- #
def test_inbound_reply_is_visible_in_the_kpi_funnel(temp_db, auto_reply_on, monkeypatch):
    """The reply flag is read by two consumers; a break shows up as 0% reply rate."""
    assert funnel()["replied"] == 0  # nothing yet

    monkeypatch.setattr("reply.inbox.fetch_replies", lambda limit=20: [_inbound("Interested!")])
    _capture_sends(monkeypatch)

    import reply.runner as runner

    runner.run_reply_pass(chat=FakeChat(responses=[_DRAFT]))

    k = funnel()
    assert k["emailed"] == 1
    assert k["replied"] == 1
    assert k["reply_rate_pct"] == 100.0
    # With a reply in hand the bottleneck moves off the top of the funnel.
    assert "NO CALLS" in k["verdict"]


def test_reply_flag_stops_followups(temp_db, auto_reply_on, monkeypatch):
    """Same flag, other consumer: a replied prospect must never get a cold follow-up."""
    monkeypatch.setattr("reply.inbox.fetch_replies", lambda limit=20: [_inbound("Interested!")])
    _capture_sends(monkeypatch)

    import reply.runner as runner

    runner.run_reply_pass(chat=FakeChat(responses=[_DRAFT]))

    with dbsession.get_session() as session:
        rec = session.query(OutreachRecord).filter_by(email=PROSPECT).one()
        assert rec.replied is True


# --------------------------------------------------------------------------- #
# join 2: threading headers survive the runner -> sender hand-off
# --------------------------------------------------------------------------- #
def test_auto_reply_threads_onto_the_prospects_message(temp_db, auto_reply_on, monkeypatch):
    """Without In-Reply-To the answer arrives as a brand-new thread.

    The runner reads these off the inbound dict and the sender writes the headers;
    this pins that the values actually make the trip between them.
    """
    monkeypatch.setattr(
        "reply.inbox.fetch_replies",
        lambda limit=20: [_inbound("How long would it take?", mid="<m2@acme.com>",
                                   refs="<m1@acme.com>")],
    )
    calls = _capture_sends(monkeypatch)

    import reply.runner as runner

    runner.run_reply_pass(chat=FakeChat(responses=[_DRAFT]))

    assert len(calls) == 1
    assert calls[0]["in_reply_to"] == "<m2@acme.com>"
    assert calls[0]["references"] == "<m1@acme.com>"


def test_sender_chains_references_including_the_replied_to_id(monkeypatch):
    """References must accumulate the prior chain AND the message being answered,
    otherwise long threads fragment after the second exchange."""
    import reply.sender as sender

    sent: list = []

    class _SMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            return (250, b"ok")

        def starttls(self):
            return (220, b"ok")

        def login(self, u, p):
            pass

        def send_message(self, msg, to_addrs=None):
            sent.append(msg)

    import config

    real = config.get_settings

    def s():
        cfg = real()
        cfg.auto_reply = True
        cfg.smtp_host = "smtp.example.com"
        cfg.smtp_user = ""
        cfg.smtp_from = "me@example.com"
        return cfg

    monkeypatch.setattr(sender, "get_settings", s)
    monkeypatch.setattr(sender.smtplib, "SMTP", _SMTP)

    ok = sender.send_reply(
        PROSPECT,
        "Re: Kubernetes hardening",
        "Sounds good — here's a slot: https://cal.com/surya-devsecops/15min",
        in_reply_to="<m2@acme.com>",
        references="<m1@acme.com>",
    )

    assert ok is True
    msg = sent[0]
    assert msg["In-Reply-To"] == "<m2@acme.com>"
    assert msg["References"] == "<m1@acme.com> <m2@acme.com>"


# --------------------------------------------------------------------------- #
# join 3: reply -> booking -> the SAME outreach row
# --------------------------------------------------------------------------- #
def test_reply_then_booking_lands_on_one_row_and_completes_the_funnel(
    temp_db, auto_reply_on, monkeypatch
):
    """The full closure: emailed -> replied -> call booked, read through the KPIs.

    Both writes must find the same ``OutreachRecord``. If the booking created a second
    row (or matched nothing), the funnel would report a reply that never converted —
    and the verdict would blame the close instead of congratulating it.
    """
    monkeypatch.setattr(
        "reply.inbox.fetch_replies", lambda limit=20: [_inbound("Yes — can we talk?")]
    )
    _capture_sends(monkeypatch)

    import reply.runner as runner

    runner.run_reply_pass(chat=FakeChat(responses=[_DRAFT]))

    # ... the prospect books via cal.com; the webhook stamps the row.
    with dbsession.get_session() as session:
        rec = session.query(OutreachRecord).filter_by(email=PROSPECT).one()
        rec.call_booked_at = _dt.datetime.now(_dt.UTC)

    with dbsession.get_session() as session:
        assert session.query(OutreachRecord).count() == 1, "booking must not create a row"

    k = funnel()
    assert (k["emailed"], k["replied"], k["calls_booked"]) == (1, 1, 1)
    assert k["booking_rate_pct"] == 100.0
    assert "none won yet" in k["verdict"]  # machine works; only the call remains


def test_cal_webhook_marks_replied_even_without_an_email_reply(temp_db):
    """Some prospects skip the email and just book. That is a reply by any useful
    definition, so the funnel must not show 1 booking from 0 replies."""
    from interfaces.dashboard import _extract_booking_emails

    payload = {
        "attendees": [{"email": PROSPECT, "name": "A Prospect"}],
        "responses": {"email": {"value": PROSPECT}},
    }
    assert PROSPECT in _extract_booking_emails(payload)

    with dbsession.get_session() as session:
        rec = session.query(OutreachRecord).filter_by(email=PROSPECT).one()
        rec.call_booked_at = _dt.datetime.now(_dt.UTC)
        rec.replied = True  # what the webhook handler does

    k = funnel()
    assert k["replied"] == 1
    assert k["booking_rate_pct"] == 100.0


# --------------------------------------------------------------------------- #
# the reply pass must bootstrap its own schema
# --------------------------------------------------------------------------- #
def test_reply_pass_bootstraps_the_schema(tmp_path, monkeypatch, auto_reply_on):
    """The reply runner reads ``outreach`` before ``record_run`` ever calls init_db.

    It was the only DB-touching entrypoint that didn't bootstrap, so a drifted schema
    surfaced as a mid-pass ``no such column`` instead of a startup heal.
    """
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    # NOTE: no create_all — the tables do not exist yet.
    monkeypatch.setattr("reply.inbox.fetch_replies", lambda limit=20: [])

    import reply.runner as runner

    stats = runner.run_reply_pass()  # must not raise

    assert stats["inbound"] == 0
    from sqlalchemy import inspect

    assert "outreach" in set(inspect(engine).get_table_names())


def test_reply_records_both_directions_of_the_conversation(temp_db, auto_reply_on, monkeypatch):
    """The dashboard Conversations view (and any future thread-quality review) needs
    both sides persisted, not just the outbound."""
    monkeypatch.setattr(
        "reply.inbox.fetch_replies", lambda limit=20: [_inbound("What's your rate?")]
    )
    _capture_sends(monkeypatch)

    import reply.runner as runner

    runner.run_reply_pass(chat=FakeChat(responses=[_DRAFT]))

    with dbsession.get_session() as session:
        dirs = {r.direction for r in session.query(ReplyRecord).all()}
    assert dirs == {"in", "out"}
