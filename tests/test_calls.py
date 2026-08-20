"""Offline tests for booked-call detection and the owner's briefing email.

What broke, on 2026-08-20: someone booked a real 15-minute call through cal.com and every
automated surface stayed silent. ``POST /webhooks/cal`` was implemented and correct, but
it needs a publicly reachable host and hosting the dashboard was declined — so it had
never fired once, ``OutreachRecord.call_booked_at`` was NULL for every row ever written,
and the KPI report said "0 calls booked" while the call was sitting in the inbox. A
metric that cannot rise is not a metric, and a mechanism that reports success while
quietly not doing its job is worse than one that fails loudly.

The replacement reads the confirmation email, because the inbox is the only production
surface that already works. These tests pin the properties that make it trustworthy:

* the real cal.com email parses to the right *person* (the owner's own address must never
  come back as the invitee — that would brief the owner about themselves);
* a cancellation never briefs as a booking, because a cancellation quotes the original
  event and therefore contains the booking wording too;
* the briefing is sent exactly once per booking, and ``notified`` flips only after the
  send actually returns True, so an SMTP outage means "retry", not "silently lost";
* an inbound booking's purpose is reported as UNKNOWN. It would be easy to guess one and
  read well; the owner would then walk into the call confidently wrong.
"""
from __future__ import annotations

import datetime as _dt
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
from calls import brief as brief_mod
from calls import detect as detect_mod
from calls import parse as parse_mod
from db.models import Base, CallRecord, LeadRecord, OutreachRecord, ProposalRecord

OWNER_EMAIL = "suryaanandan1995@gmail.com"
OWNER_NAME = "Surya A"
OURS = {OWNER_EMAIL}

# The real thing, retyped from the confirmation that arrived on 2026-08-20. Kept verbatim
# in shape (blank lines, role labels on their own line, the timezone in parentheses)
# because every one of those details is something the parser walks over.
REAL_BOOKING_SUBJECT = (
    "Confirmed: 15 min meeting between Surya Anandan and Senthil Govindarajan "
    "on Friday, August 21, 2026"
)
REAL_BOOKING_BODY = """\
This meeting is scheduled

What
15 min meeting between Surya Anandan and Senthil Govindarajan

When
Friday, August 21, 2026
4:00pm - 4:15pm (Atlantic/Reykjavik)

Who
Surya Anandan
Organizer
suryaanandan1995@gmail.com

Senthil Govindarajan
Guest
senthil.govindarajan@gmail.com

Where
Cal Video
https://app.cal.com/video/gwFiJkXTZGJToYpdXVFoPB

Need to make a change?
Reschedule or Cancel
Powered by Cal.com
"""

CANCELLED_BODY = """\
This event has been cancelled

What
15 min meeting between Surya Anandan and Senthil Govindarajan

When
Friday, August 21, 2026
4:00pm - 4:15pm (Atlantic/Reykjavik)

Who
Surya Anandan
Organizer
suryaanandan1995@gmail.com

Senthil Govindarajan
Guest
senthil.govindarajan@gmail.com

Where
Cal Video
https://app.cal.com/video/gwFiJkXTZGJToYpdXVFoPB
"""


def _parse(subject=REAL_BOOKING_SUBJECT, body=REAL_BOOKING_BODY, message_id="<m1@cal.com>"):
    return parse_mod.parse_booking(
        subject=subject,
        body=body,
        message_id=message_id,
        owner_addresses=OURS,
        owner_name=OWNER_NAME,
    )


# --------------------------------------------------------------------------- parsing


def test_real_cal_com_email_parses_to_the_guest_not_the_owner():
    booking = _parse()
    assert booking is not None
    assert booking["kind"] == "booked"
    # The owner's address appears FIRST in the body. Returning it would brief the owner
    # about themselves and match the wrong outreach row.
    assert booking["invitee_email"] == "senthil.govindarajan@gmail.com"
    assert booking["invitee_name"] == "Senthil Govindarajan"


def test_booking_uid_is_the_cal_video_slug():
    # Stable across re-reads of the same message, which is what makes "brief exactly
    # once" work. The Message-ID would also be stable, but the slug also survives cal.com
    # re-sending the confirmation with a fresh Message-ID.
    assert _parse()["booking_uid"] == "gwFiJkXTZGJToYpdXVFoPB"


def test_when_text_joins_the_date_and_time_lines():
    when = _parse()["when_text"]
    assert "Friday, August 21, 2026" in when
    assert "4:00pm - 4:15pm" in when
    assert "Atlantic/Reykjavik" in when


def test_join_url_is_captured():
    assert _parse()["join_url"] == "https://app.cal.com/video/gwFiJkXTZGJToYpdXVFoPB"


def test_cancellation_is_never_read_as_a_booking():
    # A cancellation quotes the original event, so it contains "is scheduled"/"meeting
    # between" too. Classifying booked-first would send the owner to a dead call.
    booking = _parse(subject="Cancelled: 15 min meeting", body=CANCELLED_BODY)
    assert booking is not None
    assert booking["kind"] == "cancelled"
    assert booking["booking_uid"] == "gwFiJkXTZGJToYpdXVFoPB"


def test_non_booking_cal_com_mail_is_ignored():
    # cal.com also sends product mail, digests and receipts. None of it is an event.
    assert (
        _parse(
            subject="Your Cal.com weekly summary",
            body="You had 0 bookings this week. Upgrade to Teams to add round-robin.",
        )
        is None
    )


def test_infrastructure_addresses_are_never_the_invitee():
    body = REAL_BOOKING_BODY.replace(
        "senthil.govindarajan@gmail.com", "no-reply@cal.com"
    )
    # Everything left is ours or cal.com's, so there is nobody to brief about — and
    # "CALL BOOKED — (unknown)" is worse than silence.
    assert _parse(body=body) is None


def test_a_cal_com_product_email_is_not_a_booking():
    """The false positive from the first production sweep, pinned.

    cal.com's release notes matched the old marker list ("new event", "confirmed") and the
    owner was emailed a briefing for a call that did not exist. A check that fires for a
    reason unrelated to the one it exists for is worse than no check — so the guard is now
    structural: a booking names a person and a time, and marketing mail names neither.
    """
    changelog = """\
Changelog: Cal.com v6.8 - Cal Events, New troubleshooter, AI chat in routing forms & more

Cal Events is here. Your new event types are confirmed instantly and the
troubleshooter shows why a slot is scheduled or not.

Questions? support@cal.com
Unsubscribe from product updates
"""
    assert (
        _parse(
            subject="Changelog: Cal.com v6.8 - Cal Events, New troubleshooter",
            body=changelog,
            message_id="<JBjEJYhrQ3iQcb8mra2yIw@geopod-ismtpd-67>",
        )
        is None
    )


def test_neither_a_time_nor_a_link_is_not_a_booking():
    # A person alone is not enough: mail that merely mentions somebody, with no time and
    # nothing to join, describes no event. The owner cannot prepare for "at ?", and the
    # stored row would then be protected by the dedupe key forever.
    body = (
        REAL_BOOKING_BODY.replace("Friday, August 21, 2026\n", "")
        .replace("4:00pm - 4:15pm (Atlantic/Reykjavik)\n", "")
        .replace("https://app.cal.com/video/gwFiJkXTZGJToYpdXVFoPB", "")
    )
    assert _parse(body=body) is None


def test_message_id_is_the_fallback_uid_without_a_video_link():
    body = REAL_BOOKING_BODY.replace(
        "https://app.cal.com/video/gwFiJkXTZGJToYpdXVFoPB", "Phone call"
    )
    booking = _parse(body=body, message_id="<abc-123@cal.com>")
    assert booking["booking_uid"] == "<abc-123@cal.com>"
    assert booking["join_url"] == ""


def test_name_survives_a_role_label_glued_to_it():
    body = REAL_BOOKING_BODY.replace(
        "Senthil Govindarajan\nGuest\n", "Senthil GovindarajanGuest\n"
    )
    assert _parse(body=body)["invitee_name"] == "Senthil Govindarajan"


def test_subject_name_never_swallows_the_date():
    # Subject-derived names are the fallback path; " on Friday, August 21, 2026" must not
    # end up inside the person's name (and therefore in the email subject line).
    name = parse_mod._name_from_subject(REAL_BOOKING_SUBJECT, OWNER_NAME)
    assert name == "Senthil Govindarajan"


def test_subject_name_handles_the_owner_listed_second():
    name = parse_mod._name_from_subject(
        "30 min meeting between Dana Whitfield and Surya Anandan", OWNER_NAME
    )
    assert name == "Dana Whitfield"


def test_name_falls_back_to_the_address_local_part():
    booking = parse_mod.parse_booking(
        subject="Your meeting is scheduled",
        body=(
            "Confirmed\n"
            "Monday, September 1, 2026\n"
            "10:00am - 10:15am (UTC)\n"
            "someone@acme.io\n"
            "https://app.cal.com/video/xyz789"
        ),
        message_id=None,
        owner_addresses=OURS,
        owner_name=OWNER_NAME,
    )
    # Never blank: "CALL BOOKED — " with nothing after it reads as a bug, not a booking.
    assert booking["invitee_name"] == "Someone"


def test_is_freemail_distinguishes_a_company_from_a_person():
    assert parse_mod.is_freemail("senthil.govindarajan@gmail.com") is True
    assert parse_mod.is_freemail("chris@earl.partners") is False
    assert parse_mod.is_freemail("") is False


# --------------------------------------------------------------------------- briefing


def test_inbound_brief_says_the_purpose_is_unknown_and_gives_the_question():
    subject, body = brief_mod.build_brief(booking=_parse(), origin="inbound")
    assert "Senthil Govindarajan" in subject
    assert "August 21, 2026" in subject
    assert "unknown" in body.lower()
    # The one thing that resolves an inbound booking of unknown purpose.
    assert "what made you book" in body.lower()
    assert "NOT in the outreach ledger" in body
    # No company to research, so the briefing must say so rather than imply homework.
    assert "personal mailbox" in body


def test_inbound_brief_names_the_likely_referrer_post():
    class _Post:
        body = "Prompt injection is not a prompt problem. Least privilege at the boundary."
        published_at = _dt.datetime(2026, 8, 19, 9, 25)

    _, body = brief_mod.build_brief(
        booking=_parse(), origin="inbound", latest_post=_Post()
    )
    assert "2026-08-19" in body
    assert "Prompt injection" in body


def test_company_address_brief_asks_for_two_minutes_of_research():
    booking = dict(_parse(), invitee_email="chris@earl.partners")
    _, body = brief_mod.build_brief(booking=booking, origin="inbound")
    assert "earl.partners" in body
    assert "personal mailbox" not in body


def test_outreach_brief_names_the_job_and_warns_about_the_unread_pitch():
    class _Lead:
        title = "AI product engineer"
        company = "Earl"
        source = "hn_hiring"
        url = "https://news.ycombinator.com/item?id=49275045"
        description = "Embedded with a Premier League club's commercial team."

    _, body = brief_mod.build_brief(
        booking=_parse(),
        origin="outreach",
        lead=_Lead(),
        pitch="I build LLM systems that survive production.",
    )
    assert "Earl" in body
    assert "49275045" in body
    assert "Premier League" in body
    # The owner never read the email an agent sent in their name.
    assert "Read it before you join" in body
    assert "I build LLM systems that survive production." in body
    assert "unknown" not in body.lower()


def test_every_brief_carries_a_minute_by_minute_plan_and_a_do_not_list():
    for origin in ("inbound", "outreach"):
        _, body = brief_mod.build_brief(booking=_parse(), origin=origin)
        assert "HOW TO HANDLE IT" in body
        assert "0:00" in body and "13:00" in body
        # The two ways a 15-minute call is lost: pricing undefined scope, and free work.
        assert "Do NOT" in body
        assert "unpaid" in body


def test_missing_time_is_admitted_rather_than_left_blank():
    booking = dict(_parse(), when_text="")
    subject, body = brief_mod.build_brief(booking=booking, origin="inbound")
    assert "see the cal.com email" in subject or "see the cal.com email" in body


def test_cancellation_email_has_no_plan_to_prepare():
    subject, body = brief_mod.build_cancellation(_parse(subject="Cancelled: 15 min meeting"))
    assert subject.startswith("CALL CANCELLED")
    assert "Nothing to prepare" in body
    assert "HOW TO HANDLE IT" not in body


# --------------------------------------------------------------------------- detection


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'calls.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


@pytest.fixture
def sent(monkeypatch):
    """Capture owner alerts. ``detect`` imports send_alert from runlog at call time."""
    box: list[tuple[str, str]] = []

    def _send(subject, body):
        box.append((subject, body))
        return True

    import runlog

    monkeypatch.setattr(runlog, "send_alert", _send)
    return box


def _fake_inbox(monkeypatch, messages):
    monkeypatch.setattr(detect_mod, "_fetch_cal_mail", lambda settings, limit=40: messages)


def _real_message(subject=REAL_BOOKING_SUBJECT, body=REAL_BOOKING_BODY, mid="<m1@cal.com>"):
    return {"subject": subject, "body": body, "message_id": mid}


def test_detection_off_is_a_silent_no_op(monkeypatch, temp_db, sent):
    monkeypatch.setenv("COPILOT_DETECT_CALLS", "false")
    _fake_inbox(monkeypatch, [_real_message()])
    stats = detect_mod.scan_for_bookings()
    assert stats == {
        "scanned": 0,
        "booked": 0,
        "cancelled": 0,
        "briefed": 0,
        "already_known": 0,
        "purged": 0,
        "errors": 0,
    }
    assert sent == []


def test_inbound_booking_is_recorded_and_briefed_once(monkeypatch, temp_db, sent):
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    _fake_inbox(monkeypatch, [_real_message()])

    first = detect_mod.scan_for_bookings()
    assert first["booked"] == 1
    assert first["briefed"] == 1
    assert len(sent) == 1
    assert sent[0][0].startswith("CALL BOOKED — Senthil Govindarajan")

    with dbsession.get_session() as s:
        row = s.query(CallRecord).one()
        assert row.booking_uid == "gwFiJkXTZGJToYpdXVFoPB"
        assert row.origin == "inbound"
        assert row.status == "booked"
        assert row.notified is True

    # Same mail, next pass. The already-read sweep re-surfaces it every two hours, so
    # "brief once" has to come from the database, not from mail flags.
    second = detect_mod.scan_for_bookings()
    assert second["already_known"] == 1
    assert second["booked"] == 0
    assert len(sent) == 1


def test_a_booking_from_a_contacted_lead_stamps_the_funnel(monkeypatch, temp_db, sent):
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    with dbsession.get_session() as s:
        lead = LeadRecord(
            source="hn_hiring",
            external_id="hn-49275045",
            title="AI product engineer",
            company="Earl",
            url="https://news.ycombinator.com/item?id=49275045",
            description="Embedded with a Premier League club.",
        )
        s.add(lead)
        s.flush()
        s.add(
            OutreachRecord(
                lead_id=lead.id,
                email="chris@earl.partners",
                subject="Shipping production LLM systems",
                sent_at=_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1),
            )
        )
        s.add(
            ProposalRecord(
                lead_id=lead.id,
                body="I build LLM systems that survive production.",
            )
        )

    body = REAL_BOOKING_BODY.replace(
        "senthil.govindarajan@gmail.com", "chris@earl.partners"
    )
    _fake_inbox(monkeypatch, [_real_message(body=body)])
    stats = detect_mod.scan_for_bookings()

    assert stats["booked"] == 1
    with dbsession.get_session() as s:
        call = s.query(CallRecord).one()
        assert call.origin == "outreach"
        assert call.lead_id is not None
        # This is the write the webhook never performed. Without it the booked stage of
        # every KPI report stays 0 no matter how many calls actually happen.
        assert s.query(OutreachRecord).one().call_booked_at is not None
    assert "Earl" in sent[0][1]


def test_an_unsent_briefing_is_retried_not_lost(monkeypatch, temp_db):
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    import runlog

    monkeypatch.setattr(runlog, "send_alert", lambda subject, body: False)
    _fake_inbox(monkeypatch, [_real_message()])

    first = detect_mod.scan_for_bookings()
    assert first["booked"] == 1
    # The row exists, so dedupe will skip this mail forever. If notified were set True
    # here the owner would never hear about the call at all.
    assert first["briefed"] == 0
    with dbsession.get_session() as s:
        assert s.query(CallRecord).one().notified is False

    box: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runlog, "send_alert", lambda subject, body: bool(box.append((subject, body)) or True)
    )
    second = detect_mod.scan_for_bookings()
    assert second["briefed"] == 1
    assert len(box) == 1
    with dbsession.get_session() as s:
        assert s.query(CallRecord).one().notified is True

    # And it is not re-sent a third time.
    third = detect_mod.scan_for_bookings()
    assert third["briefed"] == 0
    assert len(box) == 1


def test_cancelling_a_known_booking_warns_the_owner(monkeypatch, temp_db, sent):
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    _fake_inbox(monkeypatch, [_real_message()])
    detect_mod.scan_for_bookings()

    _fake_inbox(
        monkeypatch,
        [_real_message(subject="Cancelled: 15 min meeting", body=CANCELLED_BODY, mid="<m2@cal.com>")],
    )
    stats = detect_mod.scan_for_bookings()
    assert stats["cancelled"] == 1
    assert sent[-1][0].startswith("CALL CANCELLED")
    with dbsession.get_session() as s:
        assert s.query(CallRecord).one().status == "cancelled"


def test_a_cancellation_seen_first_never_becomes_a_booking(monkeypatch, temp_db, sent):
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    _fake_inbox(
        monkeypatch,
        [_real_message(subject="Cancelled: 15 min meeting", body=CANCELLED_BODY)],
    )
    stats = detect_mod.scan_for_bookings()
    assert stats["cancelled"] == 1
    assert stats["booked"] == 0
    assert sent == []

    # The confirmation for the same event may still be sitting in the mailbox, later in
    # the sweep or in a subsequent pass. It must not resurrect a cancelled call.
    _fake_inbox(monkeypatch, [_real_message()])
    again = detect_mod.scan_for_bookings()
    assert again["booked"] == 0
    assert sent == []


def test_one_unparseable_message_does_not_end_the_sweep(monkeypatch, temp_db, sent):
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    _fake_inbox(
        monkeypatch,
        [
            {"subject": "Confirmed: broken", "body": None, "message_id": None},
            _real_message(),
        ],
    )
    stats = detect_mod.scan_for_bookings()
    assert stats["booked"] == 1
    assert len(sent) == 1


def test_imap_failure_degrades_to_zeros(monkeypatch, temp_db, sent):
    """A broken mailbox must not take down the pass this runs inside."""

    def _boom(settings, limit=40):
        raise OSError("connection reset")

    monkeypatch.setattr(detect_mod, "_fetch_cal_mail", _boom)
    stats = detect_mod.scan_for_bookings()
    assert stats["errors"] == 1
    assert stats["booked"] == 0
    assert sent == []


def test_no_smtp_host_configured_is_a_no_op(monkeypatch, temp_db):
    # An unconfigured mailbox is the normal state on a developer machine, and it must not
    # look like "no bookings found" in a log — _fetch_cal_mail says so and returns [].
    monkeypatch.setenv("COPILOT_SMTP_HOST", "")
    monkeypatch.setenv("COPILOT_IMAP_HOST", "")
    from config import get_settings

    assert detect_mod._fetch_cal_mail(get_settings()) == []


def test_reply_pass_reports_call_counters_even_with_replies_off(monkeypatch, temp_db):
    """A booked call must reach the owner when both reply gates are closed.

    The reply pass returns early when auto_reply and reply_detection are both off. cal.com
    mail is not a reply from a lead — fetch_replies filters to known senders and skips it
    entirely — so if the sweep sat after that gate, the single most valuable event in the
    pipeline would be the one thing the pipeline never reported.
    """
    monkeypatch.setenv("COPILOT_AUTO_REPLY", "false")
    monkeypatch.setenv("COPILOT_REPLY_DETECTION", "false")
    monkeypatch.setattr(
        detect_mod,
        "scan_for_bookings",
        lambda: {
            "scanned": 1,
            "booked": 1,
            "cancelled": 0,
            "briefed": 1,
            "already_known": 0,
            "errors": 0,
        },
    )
    from reply.runner import run_reply_pass

    stats = run_reply_pass()
    assert stats["calls_booked"] == 1
    assert stats["calls_briefed"] == 1
    assert stats["inbound"] == 0  # replies really were skipped


def test_list_masks_addresses_and_names_the_unbriefed(monkeypatch, temp_db, capsys):
    """`calls --list` names the rows behind ``booked=2``, without publishing addresses.

    The first production sweep reported ``booked=2`` when exactly one booking was known
    about, and identifying the second required the database password. A count is not an
    answer. This repository is public, so its Actions logs are public: the domain answers
    "which company?", the local part is the invitee's own.
    """
    import main

    with dbsession.get_session() as s:
        s.add(
            CallRecord(
                booking_uid="uid-briefed",
                invitee_name="Senthil Govindarajan",
                invitee_email="senthil.govindarajan@gmail.com",
                when_text="Friday, August 21, 2026 4:00pm - 4:15pm",
                subject="Confirmed: 15 min meeting",
                origin="inbound",
                status="booked",
                notified=True,
            )
        )
        s.add(
            CallRecord(
                booking_uid="uid-silent",
                invitee_name="Dana Whitfield",
                invitee_email="dana@acme.io",
                when_text="Monday, August 24, 2026 9:00am - 9:30am",
                origin="inbound",
                status="booked",
                notified=False,
            )
        )

    import argparse

    assert main._cmd_calls(argparse.Namespace(list=True)) == 0
    out = capsys.readouterr().out
    assert "Senthil Govindarajan" in out
    assert "Dana Whitfield" in out
    # Masked, never whole.
    assert "senthil.govindarajan@gmail.com" not in out
    assert "s***@gmail.com" in out
    assert "d***@acme.io" in out
    # The one state worth acting on has to be visible per row, not only in an aggregate.
    assert "BRIEFING NOT SENT" in out


def test_list_says_so_when_there_are_no_bookings(temp_db, capsys):
    import argparse

    import main

    assert main._cmd_calls(argparse.Namespace(list=True)) == 0
    # "no bookings recorded yet" and an empty stdout read very differently in a log.
    assert "no bookings recorded yet" in capsys.readouterr().out


def test_list_never_sweeps_the_inbox(monkeypatch, temp_db):
    """--list is a read. It must not log into IMAP and must not email anyone."""
    import argparse

    import main

    monkeypatch.setattr(
        detect_mod,
        "scan_for_bookings",
        lambda: (_ for _ in ()).throw(AssertionError("--list must not sweep")),
    )
    assert main._cmd_calls(argparse.Namespace(list=True)) == 0


def test_rows_the_old_parser_wrote_are_healed_away(monkeypatch, temp_db, sent):
    """A row with neither person nor time is deleted, not left in every listing forever.

    Self-healing rather than a cleanup command: the standing requirement is no manual
    steps. The set is closed — the parser now refuses such mail outright — so this can only
    ever remove rows written by the version that was wrong.
    """
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    with dbsession.get_session() as s:
        s.add(
            CallRecord(
                booking_uid="<JBjEJYhrQ3iQcb8mra2yIw@geopod-ismtpd-67>",
                invitee_name="(unknown)",
                invitee_email="",
                when_text="",
                subject="Changelog: Cal.com v6.8 - Cal Events, New troubleshooter",
                origin="inbound",
                status="booked",
                notified=True,
            )
        )
    _fake_inbox(monkeypatch, [_real_message()])

    stats = detect_mod.scan_for_bookings()
    assert stats["purged"] == 1
    with dbsession.get_session() as s:
        rows = s.query(CallRecord).all()
        # The real booking survives; the changelog row does not.
        assert [r.invitee_email for r in rows] == ["senthil.govindarajan@gmail.com"]


def test_purge_never_touches_a_real_booking(monkeypatch, temp_db, sent):
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    _fake_inbox(monkeypatch, [_real_message()])
    detect_mod.scan_for_bookings()
    # A booking with an address but no parsed time is incomplete, not junk: it names
    # somebody, so deleting it would lose a real call.
    with dbsession.get_session() as s:
        s.add(
            CallRecord(
                booking_uid="uid-no-time",
                invitee_name="Dana Whitfield",
                invitee_email="dana@acme.io",
                when_text="",
                origin="inbound",
                status="booked",
                notified=True,
            )
        )
    second = detect_mod.scan_for_bookings()
    assert second["purged"] == 0
    with dbsession.get_session() as s:
        assert s.query(CallRecord).count() == 2


# The shape that actually arrives: cal.com's confirmation is HTML-only. Table cells and
# styled paragraphs, non-breaking spaces, the role label glued to the name — all of it is
# why the first stored booking read "Senthil Govindarajan — ?" and the briefing had to
# tell the owner to go and look up the time of a call happening the next day.
HTML_BOOKING_BODY = """\
<html><head><style>.p{color:#000}</style></head><body>
<table><tr><td><p style="font-size:24px">Cal.com&nbsp;&nbsp;Confirmed</p></td></tr>
<tr><td><h1>A new event has been scheduled</h1></td></tr>
<tr><td><p><strong>What</strong></p>
<p>15 min meeting between Surya Anandan and Senthil Govindarajan</p></td></tr>
<tr><td><p><strong>When</strong></p>
<p style="color:#101010">Friday,&nbsp;August 21, 2026</p>
<p style="color:#101010">4:00pm - 4:15pm (Atlantic/Reykjavik)</p></td></tr>
<tr><td><p><strong>Who</strong></p>
<p>Surya Anandan<span>Organizer</span></p><p><a href="mailto:suryaanandan1995@gmail.com">suryaanandan1995@gmail.com</a></p>
<p>Senthil Govindarajan<span>Guest</span></p><p><a href="mailto:senthil.govindarajan@gmail.com">senthil.govindarajan@gmail.com</a></p>
</td></tr>
<tr><td><p><strong>Where</strong></p><p>Cal Video</p>
<p><a href="https://app.cal.com/video/gwFiJkXTZGJToYpdXVFoPB">https://app.cal.com/video/gwFiJkXTZGJToYpdXVFoPB</a></p></td></tr>
</table></body></html>
"""


def test_the_html_only_confirmation_parses_completely():
    """The bug the very first stored booking exposed, pinned end to end.

    Addresses survive markup, so the invitee was found and everything else was silently
    lost: `when_text` was empty because "Friday, August 21, 2026" sat inside a styled
    <p> and no line began with a weekday. A field that is blank for a reason unrelated to
    the data is indistinguishable from a field the sender omitted.
    """
    booking = _parse(body=HTML_BOOKING_BODY)
    assert booking is not None
    assert booking["kind"] == "booked"
    assert booking["invitee_email"] == "senthil.govindarajan@gmail.com"
    assert booking["invitee_name"] == "Senthil Govindarajan"
    assert "Friday, August 21, 2026" in booking["when_text"]
    assert "4:00pm - 4:15pm" in booking["when_text"]
    assert booking["booking_uid"] == "gwFiJkXTZGJToYpdXVFoPB"
    assert booking["join_url"] == "https://app.cal.com/video/gwFiJkXTZGJToYpdXVFoPB"


def test_visible_text_drops_style_blocks_and_keeps_the_layout():
    text = parse_mod.visible_text(HTML_BOOKING_BODY)
    # A <style> body would otherwise be searched for names and times.
    assert "color:#000" not in text
    assert "font-size" not in text
    # One field per line is what every heuristic depends on.
    lines = text.splitlines()
    assert "Friday, August 21, 2026" in lines
    assert "senthil.govindarajan@gmail.com" in lines
    # &nbsp; must not survive as U+00A0: the regexes do not treat it as whitespace.
    assert "\xa0" not in text


def test_visible_text_leaves_plain_text_untouched():
    assert parse_mod.visible_text(REAL_BOOKING_BODY) == REAL_BOOKING_BODY


def test_a_booking_with_a_link_but_no_parsed_time_is_still_a_booking():
    # Two independent signals, either of which may fail. Requiring the time as well as the
    # person made a formatting change enough to lose a real booking silently — the same
    # brittleness as the keyword list, in the opposite direction.
    body = REAL_BOOKING_BODY.replace("Friday, August 21, 2026\n", "").replace(
        "4:00pm - 4:15pm (Atlantic/Reykjavik)\n", ""
    )
    booking = _parse(body=body)
    assert booking is not None
    assert booking["when_text"] == ""


def test_details_that_fill_in_later_are_backfilled_and_rebriefed(monkeypatch, temp_db, sent):
    """The stored booking says "?" for the time; the fixed parser knows it. Tell the owner.

    They were told to go and look the time up themselves, for a call that may be tomorrow.
    Guarded on the field having been blank, so a re-read cannot turn this into a re-send
    every two hours.
    """
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    with dbsession.get_session() as s:
        s.add(
            CallRecord(
                booking_uid="gwFiJkXTZGJToYpdXVFoPB",
                invitee_name="Senthil Govindarajan",
                invitee_email="senthil.govindarajan@gmail.com",
                when_text="",  # what the HTML-blind parser stored
                join_url="",
                subject="15 min meeting between Surya Anandan and Senthil Govindarajan",
                origin="inbound",
                status="booked",
                notified=True,
            )
        )

    _fake_inbox(monkeypatch, [_real_message(body=HTML_BOOKING_BODY)])
    stats = detect_mod.scan_for_bookings()
    assert stats["already_known"] == 1
    assert stats["booked"] == 0
    assert stats["briefed"] == 1
    assert "August 21, 2026" in sent[-1][0]

    with dbsession.get_session() as s:
        row = s.query(CallRecord).one()
        assert "4:00pm - 4:15pm" in row.when_text
        assert row.join_url.endswith("gwFiJkXTZGJToYpdXVFoPB")

    # And the next pass is silent: nothing is blank any more.
    third = detect_mod.scan_for_bookings()
    assert third["briefed"] == 0
    assert len(sent) == 1


def test_backfill_never_overwrites_a_field_the_owner_has_already_read(monkeypatch, temp_db, sent):
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    with dbsession.get_session() as s:
        s.add(
            CallRecord(
                booking_uid="gwFiJkXTZGJToYpdXVFoPB",
                invitee_name="Senthil Govindarajan",
                invitee_email="senthil.govindarajan@gmail.com",
                when_text="Friday, August 21, 2026 4:00pm - 4:15pm (Atlantic/Reykjavik)",
                join_url="https://app.cal.com/video/gwFiJkXTZGJToYpdXVFoPB",
                origin="inbound",
                status="booked",
                notified=True,
            )
        )
    # A reminder or a forwarded copy of the same event, formatted differently.
    altered = HTML_BOOKING_BODY.replace("4:00pm - 4:15pm", "9:00pm - 9:15pm")
    _fake_inbox(monkeypatch, [_real_message(body=altered)])

    stats = detect_mod.scan_for_bookings()
    assert stats["briefed"] == 0
    assert sent == []
    with dbsession.get_session() as s:
        assert "4:00pm" in s.query(CallRecord).one().when_text


def test_an_html_only_multipart_email_is_not_read_as_empty():
    """`reply.inbox._plain_body` returns "" when there is no text/plain part.

    cal.com writes its confirmation for humans, in HTML. Returning "" there makes a real
    booking look like an empty email — detected as nothing, with no error anywhere.
    """
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = REAL_BOOKING_SUBJECT
    msg.set_content("fallback")  # replaced below so no text/plain part remains
    msg.clear_content()
    msg.set_content(HTML_BOOKING_BODY, subtype="html")

    body = detect_mod._body_text(msg)
    assert "Senthil Govindarajan" in body
    booking = _parse(body=body)
    assert booking is not None
    assert "4:00pm - 4:15pm" in booking["when_text"]


def test_a_stub_text_plain_alternative_does_not_win_over_the_html(caplog):
    """The bug that survived the HTML fix: preferring text/plain whenever it is non-empty.

    The real confirmation ships *both* alternatives, and the plain one is a stub — enough
    text to be truthy, not enough to hold the date. So the plain part won, the time never
    parsed, and the counters still said `briefed=1`. The choice has to be made on content:
    a mechanism that reports success while quietly not doing its job is the failure this
    codebase keeps paying for.
    """
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = REAL_BOOKING_SUBJECT
    msg.set_content("Cal.com\n\nThis email was sent to you by Cal.com.\n")
    msg.add_alternative(HTML_BOOKING_BODY, subtype="html")

    body = detect_mod._body_text(msg)
    booking = _parse(body=body)
    assert booking is not None
    assert "August 21, 2026" in booking["when_text"]
    assert "4:00pm - 4:15pm" in booking["when_text"]


def test_a_complete_text_plain_alternative_is_still_preferred():
    """The fix must not mean "always read the HTML" — plain text is cheaper and safer."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = REAL_BOOKING_SUBJECT
    msg.set_content(REAL_BOOKING_BODY)
    msg.add_alternative("<p>ignored html</p>", subtype="html")

    assert "ignored html" not in detect_mod._body_text(msg)
    assert _parse(body=detect_mod._body_text(msg))["when_text"].startswith("Friday")


def test_when_text_is_public_and_answers_the_alternative_question():
    assert parse_mod.when_text(HTML_BOOKING_BODY)
    assert parse_mod.when_text("Cal.com\nThis email was sent to you by Cal.com.") == ""


@pytest.mark.parametrize(
    "line",
    [
        "August 21, 2026 | 4:00pm - 4:15pm (Atlantic/Reykjavik)",  # both halves, one line
        "When: Aug 21, 2026 at 16:00 (Atlantic/Reykjavik)",  # heading shares the line
        "21 August 2026 16:00 (Atlantic/Reykjavik)",  # no weekday, single 24h time
        "Fri, 21st Aug 2026, 4:00pm",  # abbreviated weekday, ordinal day
    ],
)
def test_a_template_change_to_the_date_format_does_not_lose_the_time(line):
    """The strict shape is cal.com's presentation choice, not the data.

    Every one of these is a real booking whose time the anchored regex would have dropped,
    leaving the owner a briefing that says "(time not parsed)" for a call the next day.
    """
    body = REAL_BOOKING_BODY.replace("Friday, August 21, 2026\n", "").replace(
        "4:00pm - 4:15pm (Atlantic/Reykjavik)", line
    )
    booking = _parse(body=body)
    assert booking is not None
    assert "2026" in booking["when_text"]
    assert booking["when_text"].count("2026") == 1  # not the date line joined to itself


def test_the_looser_date_pass_does_not_match_a_footer():
    # "© 2026 Cal.com" and a support phone number must not become the time of the meeting.
    body = REAL_BOOKING_BODY.replace("Friday, August 21, 2026\n", "").replace(
        "4:00pm - 4:15pm (Atlantic/Reykjavik)\n", ""
    )
    body += "\n© 2026 Cal.com, Inc.\nSan Francisco, CA 94107\n"
    assert _parse(body=body)["when_text"] == ""


def test_the_unparsed_time_diagnostic_never_logs_a_person(monkeypatch, temp_db, sent, caplog):
    """A public Actions log cannot carry the mail, so it carries only lines with no PII.

    Without *some* view of the real mail an unparseable date is unfixable — but the repo is
    public, so the diagnostic is restricted by construction to lines holding a digit and
    neither "@" nor a URL.
    """
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    body = REAL_BOOKING_BODY.replace("Friday, August 21, 2026\n", "").replace(
        "4:00pm - 4:15pm (Atlantic/Reykjavik)\n", "Sometime on the 3rd\n"
    )
    _fake_inbox(monkeypatch, [_real_message(body=body)])
    with caplog.at_level(logging.INFO, logger="calls.detect"):
        stats = detect_mod.scan_for_bookings()
    assert stats["booked"] == 1
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Sometime on the 3rd" in logged
    assert "senthil.govindarajan@gmail.com" not in logged
    assert "app.cal.com/video" not in logged


# ---------------------------------------------------------------------------
# The invitee's own words. cal.com renders "Additional notes" for the free-text field and
# renders each custom booking question as its own heading — so the answer to "What do you
# want help with?" arrives in this mail. Until now those headings existed only in
# _LABEL_WORDS, so nothing read what sat underneath them: the briefing said "purpose
# unknown, ask them" while the purpose was three lines further down.
# ---------------------------------------------------------------------------
NOTES_BODY = REAL_BOOKING_BODY.replace(
    "Where\nCal Video",
    "Additional notes\n"
    "We are getting prompt-injected through our support bot and I need someone who has\n"
    "actually shipped guardrails. Can you look at our setup?\n"
    "Where\nCal Video",
)

QUESTION_BODY = REAL_BOOKING_BODY.replace(
    "Where\nCal Video",
    "What do you want help with?\nKubernetes cost blowout on EKS\n"
    "How did you find me?\nYour LinkedIn post about guardrails\n"
    "Where\nCal Video",
)


def test_the_note_the_invitee_typed_is_parsed():
    booking = _parse(body=NOTES_BODY)
    assert "prompt-injected" in booking["notes"]
    assert "Can you look at our setup?" in booking["notes"]
    # It stops at the next section: "Cal Video" belongs to Where, not to the note.
    assert "Cal Video" not in booking["notes"]


def test_a_custom_booking_question_is_quoted_with_its_question():
    """The answer alone is meaningless — "LinkedIn" answers nothing without the question."""
    notes = _parse(body=QUESTION_BODY)["notes"]
    assert "What do you want help with? Kubernetes cost blowout on EKS" in notes
    assert "How did you find me? Your LinkedIn post about guardrails" in notes


def test_calcoms_own_footer_question_is_not_read_as_a_reason(caplog):
    """"Need to make a change?" is a heading phrased as a question, and it is cal.com's.

    Without the deny-list the briefing would quote "Need to make a change? Reschedule
    Cancel" back at the owner as the reason a stranger booked a call — a field populated for
    a reason unrelated to the data it claims to hold.
    """
    body = REAL_BOOKING_BODY + "\nNeed to make a change?\nReschedule\nCancel\n"
    assert _parse(body=body)["notes"] == ""


def test_a_booking_with_no_note_reports_no_note_rather_than_guessing():
    assert _parse(body=REAL_BOOKING_BODY)["notes"] == ""
    assert _parse(body=HTML_BOOKING_BODY)["notes"] == ""


def test_notes_survive_html_and_are_bounded():
    body = HTML_BOOKING_BODY.replace(
        "<tr><td><p><strong>Where</strong></p>",
        "<tr><td><p><strong>Additional notes</strong></p>"
        "<p>" + ("scaling pains " * 200) + "</p></td></tr>"
        "<tr><td><p><strong>Where</strong></p>",
    )
    notes = _parse(body=body)["notes"]
    assert notes.startswith("scaling pains")
    # It goes into an email, so it is capped rather than trusted.
    assert len(notes) <= 600


def test_the_briefing_leads_with_their_words_and_stops_telling_you_to_ask():
    """A briefing that says "ask them why" about someone who already wrote it down reads as
    a briefing nobody read. The quote outranks every inference in the module."""
    booking = _parse(body=NOTES_BODY)
    subject, body = brief_mod.build_brief(booking=booking, origin="inbound")
    assert "in their own words" in body
    assert "prompt-injected" in body
    assert "WHY THEY BOOKED — unknown" not in body
    # The four-possibilities triage exists to recover from not knowing.
    assert "A recruiter or agency sourcing" not in body
    # And the plan opens by going deeper, not by asking what they already answered.
    assert "I read your note" in body
    assert body.index("prompt-injected") < body.index("HOW TO HANDLE IT")


def test_the_briefing_still_admits_ignorance_when_there_is_no_note():
    booking = _parse(body=REAL_BOOKING_BODY)
    _, body = brief_mod.build_brief(booking=booking, origin="inbound")
    assert "WHY THEY BOOKED — unknown" in body
    assert "in their own words" not in body


def test_a_note_is_stored_and_backfilled_but_never_printed_by_the_cli(
    monkeypatch, temp_db, sent, capsys
):
    """Stored for the briefing, withheld from `--list`: that CLI runs in a PUBLIC log.

    The words are a stranger's own account of their business. The email is the private
    channel; the log gets "a note exists", which is the part that is actionable anyway.
    """
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    _fake_inbox(monkeypatch, [_real_message(body=NOTES_BODY)])
    assert detect_mod.scan_for_bookings()["booked"] == 1

    with dbsession.get_session() as s:
        assert "prompt-injected" in s.query(CallRecord).one().notes
    assert "prompt-injected" in sent[-1][1]

    import main

    main._list_calls()
    printed = capsys.readouterr().out
    assert "prompt-injected" not in printed
    assert "note   : yes" in printed


def test_a_note_that_only_parses_later_is_backfilled_and_rebriefed(monkeypatch, temp_db, sent):
    """The row predates the parser that can read notes; the owner was told "unknown"."""
    monkeypatch.setenv("COPILOT_OWNER_EMAIL", OWNER_EMAIL)
    with dbsession.get_session() as s:
        s.add(
            CallRecord(
                booking_uid="gwFiJkXTZGJToYpdXVFoPB",
                invitee_name="Senthil Govindarajan",
                invitee_email="senthil.govindarajan@gmail.com",
                when_text="Friday, August 21, 2026 4:00pm - 4:15pm (Atlantic/Reykjavik)",
                join_url="https://app.cal.com/video/gwFiJkXTZGJToYpdXVFoPB",
                notes="",
                origin="inbound",
                status="booked",
                notified=True,
            )
        )
    _fake_inbox(monkeypatch, [_real_message(body=NOTES_BODY)])
    stats = detect_mod.scan_for_bookings()
    assert stats["already_known"] == 1
    assert stats["briefed"] == 1
    assert "prompt-injected" in sent[-1][1]
    # And it does not re-send every two hours thereafter.
    assert detect_mod.scan_for_bookings()["briefed"] == 0


def test_a_note_containing_its_own_question_is_not_cut_in_half():
    """The discriminator, pinned. "…Can you look at our setup?" is a sentence, not a heading.

    Treating every short "?" line as the next custom question truncated the note at the
    invitee's own question mark — losing the half that said what they wanted. A question
    heading is followed by its answer; a closing sentence is followed by the next section.
    """
    body = REAL_BOOKING_BODY.replace(
        "Where\nCal Video",
        "Additional notes\nOur RAG pipeline leaks PII.\nCan you review it?\nWhere\nCal Video",
    )
    notes = _parse(body=body)["notes"]
    assert notes == "Our RAG pipeline leaks PII. Can you review it?"
