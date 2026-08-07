"""Does anything notice when the INBOUND half of the loop dies?

``monitor/funnel.py`` already asserts on output rather than exit codes:
``check_contactable_supply``, ``check_queue_stalled`` and ``check_outreach_stalled``
catch a pipeline that "succeeds" while emailing nobody. All three watch OUTBOUND flow.

Nothing watched whether replies are being **read**, and that failure is invisible by
construction. :func:`reply.inbox.fetch_replies` swallows every IMAP error and returns
``[]``, so a wrong host, an expired app password, or a provider mismatch produces the
identical, cheerful ``inbound: 0`` with exit code 0 as a genuinely quiet mailbox. The
mismatch is easy to hit: ``imap_host`` defaults to ``imap.gmail.com`` while the reader
logs in with ``smtp_user``/``smtp_password``, so pointing SMTP at any non-Gmail
provider silently decouples reading from sending.

Why that is worse than "we miss a reply": a reply is what **stops** the follow-up
sequence (``replied is False`` is a selection criterion in ``followup.runner``). If
replies are never detected, nobody is ever marked replied, so the system keeps nudging
prospects who already answered — and it reads as disinterest, not as a bug.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import session as dbsession
from db.models import Base


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Isolated database per test.

    The first draft of these tests omitted this and wrote to the developer's real
    ``copilot.db``; worse, the inbound row one test inserts leaked into the next, so
    the file passed on its own and the full suite failed.
    """
    url = f"sqlite:///{tmp_path / 'funnel_alerts.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


def test_reply_detection_stays_quiet_until_enough_mail_has_been_sent(
    temp_db, monkeypatch
):
    """Zero inbound is normal at low volume — the check must not cry wolf.

    This is the guard against the over-alerting mirror: a check that fails before it
    could possibly know anything trains you to ignore it, which is the same as not
    having it at all.
    """
    from monitor import funnel

    monkeypatch.setattr(funnel, "_recent_stats", lambda limit=60: [{"emailed": 3}])
    check = funnel.check_reply_detection_alive()
    assert check["ok"] is True
    assert "below the" in check["detail"]


def test_reply_detection_alerts_when_much_mail_has_gone_out_and_nothing_came_back(
    temp_db, monkeypatch
):
    """The real signal: enough volume that never seeing one reply is suspicious."""
    from monitor import funnel

    monkeypatch.setattr(
        funnel, "_recent_stats", lambda limit=60: [{"emailed": 20}, {"emailed": 20}]
    )
    check = funnel.check_reply_detection_alive()
    assert check["ok"] is False
    # It must name the ambiguity rather than assert a cause, and it must name the
    # specific misconfiguration that produces it: the reader logs in with the SMTP
    # credentials while imap_host defaults to Gmail.
    assert "imap_host" in check["detail"]
    assert "follow-ups" in check["detail"]


def test_reply_detection_passes_once_any_inbound_message_exists(temp_db, monkeypatch):
    """One recorded inbound message proves the reader works; the market may then be as
    quiet as it likes without this firing again."""
    from monitor import funnel

    monkeypatch.setattr(funnel, "_recent_stats", lambda limit=60: [{"emailed": 99}])

    from db.models import ReplyRecord

    with dbsession.get_session() as session:
        session.add(
            ReplyRecord(email="prospect@corp.com", direction="in", subject="re: hi")
        )
    check = funnel.check_reply_detection_alive()
    assert check["ok"] is True


def test_a_missing_replies_table_is_not_reported_as_a_broken_reader(
    temp_db, monkeypatch
):
    """"No data yet" and "the reader is broken" are different answers.

    On a fresh database the count query raises ``no such table: replies``, and
    returning ``ok=False`` for that would be the same conflation this module exists to
    fight: reporting a defect where there is only an absence of evidence. The check
    calls ``init_db()`` so the answer is real rather than an artefact of setup order.
    """
    from monitor import funnel

    monkeypatch.setattr(funnel, "_recent_stats", lambda limit=60: [{"emailed": 99}])
    check = funnel.check_reply_detection_alive()
    assert isinstance(check["ok"], bool)
    assert "could not read reply history" not in check["detail"]


def test_reply_detection_is_part_of_the_standard_check_set(temp_db):
    """A check nobody runs is not a check. It must be in ``funnel_checks()``."""
    from monitor.funnel import funnel_checks

    assert "reply_detection" in {c["name"] for c in funnel_checks()}
