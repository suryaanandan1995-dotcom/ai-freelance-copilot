"""Offline tests for ``main.py ledger`` — naming the rows behind the funnel counts.

The gap this closes, from 2026-08-20: a cal.com call was booked the morning after an
outreach run that reported ``'emailed': 1``, and nothing readable could say who had
been emailed. ``kpi`` reported the counts (contactable -> emailed -> replied -> booked)
and every one of them was true, but a count cannot be prepared for. The recipient was
in ``OutreachRecord``, the database DSN is a repo secret, the dashboard is deliberately
unhosted, and ``outreach.sender`` logged only *failed* sends — so the row existed and
was unreachable from every surface a human actually reads.

So the tests below pin the two properties that make the ledger worth having: it names
the company for each contact, and it never prints a full address (this repository is
public, so its Actions logs are public).
"""
from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
import main
from db.models import Base, LeadRecord, OutreachRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'ledger.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


def _args(days: int = 30):
    import argparse

    return argparse.Namespace(days=days)


def _seed_contact(
    *,
    email: str,
    days_ago: int = 1,
    replied: bool = False,
    booked: bool = False,
    followups: int = 0,
    company: str | None = "Bactrix",
    title: str = "Founding Software Engineer",
    url: str = "https://news.ycombinator.com/item?id=49260542",
    source: str = "hn_hiring",
    link_lead: bool = True,
) -> None:
    now = _dt.datetime.utcnow()
    sent_at = now - _dt.timedelta(days=days_ago)
    with dbsession.get_session() as s:
        lead_id = None
        if link_lead:
            lead = LeadRecord(
                source=source,
                external_id=f"ext-{email}",
                title=title,
                company=company,
                url=url,
            )
            s.add(lead)
            s.flush()
            lead_id = lead.id
        s.add(
            OutreachRecord(
                lead_id=lead_id,
                email=email,
                subject="Quick question about your AI platform",
                status="sent",
                sent_at=sent_at,
                last_contact_at=sent_at,
                replied=replied,
                followups_sent=followups,
                call_booked_at=now if booked else None,
            )
        )


def test_the_ledger_names_the_company_behind_a_send(temp_db, capsys):
    """'emailed: 1' becomes 'Bactrix — Founding Software Engineer', which is workable."""
    _seed_contact(email="HR@bactrix.com")
    assert main._cmd_ledger(_args()) == 0
    out = capsys.readouterr().out
    assert "Bactrix" in out
    assert "Founding Software Engineer" in out
    assert "hn_hiring" in out
    assert "https://news.ycombinator.com/item?id=49260542" in out  # the post to re-read
    assert "Quick question about your AI platform" in out          # the thread to continue


def test_the_ledger_never_prints_a_full_address(temp_db, capsys):
    """A public Actions log must not publish the prospect's mailbox."""
    _seed_contact(email="HR@bactrix.com")
    main._cmd_ledger(_args())
    out = capsys.readouterr().out
    assert "h***@bactrix.com" in out
    assert "HR@bactrix.com" not in out


def test_a_booked_call_is_the_flag_that_leads_the_line(temp_db, capsys):
    """The whole reason the command exists: find the booking without reading every row."""
    _seed_contact(email="a@one.com", company="One")
    _seed_contact(email="b@two.com", company="Two", replied=True)
    _seed_contact(email="c@three.com", company="Three", booked=True)
    main._cmd_ledger(_args())
    out = capsys.readouterr().out
    assert "CALL BOOKED" in out
    # Exactly one row is the booking — a flag that appeared on all three would be
    # useless for the question being asked.
    assert out.count("CALL BOOKED") == 1
    booked_line = [ln for ln in out.splitlines() if "CALL BOOKED" in ln][0]
    assert "three.com" in booked_line


def test_silence_is_reported_with_the_number_of_nudges_already_sent(temp_db, capsys):
    """Before writing again, the useful fact is how many times we already have."""
    _seed_contact(email="quiet@example.com", followups=2)
    main._cmd_ledger(_args())
    out = capsys.readouterr().out
    assert "no reply" in out
    assert "2 follow-up" in out


def test_contacts_outside_the_window_are_excluded(temp_db, capsys):
    _seed_contact(email="old@example.com", days_ago=90, company="Ancient")
    _seed_contact(email="new@example.com", days_ago=2, company="Recent")
    main._cmd_ledger(_args(days=30))
    out = capsys.readouterr().out
    assert "Recent" in out
    assert "Ancient" not in out
    assert "1 contact(s)" in out


def test_an_empty_window_says_so_rather_than_printing_nothing(temp_db, capsys):
    """Silence from a diagnostic is indistinguishable from the diagnostic being broken."""
    assert main._cmd_ledger(_args()) == 0
    out = capsys.readouterr().out
    assert "0 contact(s)" in out
    assert "nothing sent in this window" in out


def test_a_send_with_no_linked_lead_still_lists_and_says_why(temp_db, capsys):
    """Older sends predate the lead link; a blank line would read as 'no such company'."""
    _seed_contact(email="orphan@example.com", link_lead=False)
    main._cmd_ledger(_args())
    out = capsys.readouterr().out
    assert "o***@example.com" in out
    assert "not linked to a stored lead" in out


def test_ledger_is_wired_into_the_cli(temp_db):
    """A diagnostic nobody can invoke is not a diagnostic."""
    parser = main.build_parser()
    parsed = parser.parse_args(["ledger", "--days", "7"])
    assert parsed.func is main._cmd_ledger
    assert parsed.days == 7


# --------------------------------------------------------------------------- #
# --body: what did we actually claim?
#
# The pitch is written by an agent and sent without a human reading it. That is fine
# right up to the moment a call is booked, at which point the person taking the call
# has to defend claims they have never seen. The ledger is the only place that can
# hand those back.
# --------------------------------------------------------------------------- #
def _seed_pitch(body: str) -> None:
    from db.models import ProposalRecord

    with dbsession.get_session() as s:
        lead = s.query(LeadRecord).first()
        s.add(ProposalRecord(lead_id=lead.id, body=body))


def test_body_flag_prints_the_pitch_that_was_sent(temp_db, capsys):
    _seed_contact(email="chris@earl.partners", company="Earl")
    _seed_pitch("I build production LLM systems.\nHappy to start with a paid trial.")

    import argparse

    main._cmd_ledger(argparse.Namespace(days=30, body=True))
    out = capsys.readouterr().out
    assert "what we claimed" in out
    assert "I build production LLM systems." in out
    assert "Happy to start with a paid trial." in out  # multi-line pitch kept intact


def test_the_pitch_is_omitted_by_default(temp_db, capsys):
    """The default listing stays scannable; bodies are opt-in."""
    _seed_contact(email="chris@earl.partners", company="Earl")
    _seed_pitch("secret sauce")
    main._cmd_ledger(_args())
    out = capsys.readouterr().out
    assert "secret sauce" not in out
    assert "what we claimed" not in out


def test_a_contact_with_no_stored_pitch_says_so(temp_db, capsys):
    """Missing must read as missing, not as an empty pitch."""
    _seed_contact(email="chris@earl.partners", company="Earl")

    import argparse

    main._cmd_ledger(argparse.Namespace(days=30, body=True))
    assert "no stored pitch for this contact" in capsys.readouterr().out


def test_body_is_wired_into_the_cli(temp_db):
    parser = main.build_parser()
    assert parser.parse_args(["ledger", "--body"]).body is True
    assert parser.parse_args(["ledger"]).body is False
