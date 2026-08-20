"""Offline tests for outcome KPIs (monitor/kpi.py).

The reporting defect these fix: for 24 runs the owner was told "workflow succeeded"
plus activity counts, all of which looked healthy while the outcome was 1 email, 0
replies, 0 calls, 0 wins, $8.55. The tests below pin that the verdict names the
*correct bottleneck stage*, because naming the wrong one sends effort to a stage that
cannot change the result.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
from db.models import Base, LeadRecord, LeadStatus, OutreachRecord, RunRecord
from monitor import kpi
from monitor.kpi import format_kpis, funnel, verdict


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'kpi.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


def _now() -> _dt.datetime:
    return _dt.datetime(2026, 8, 3, 12, 0, 0)


def _seed(
    *,
    emails: int = 0,
    replied: int = 0,
    booked: int = 0,
    contactable_per_run: int = 0,
    runs: int = 1,
    cost_per_run: float = 0.35,
    won: int = 0,
    days_ago: int = 1,
) -> None:
    when = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days_ago)
    with dbsession.get_session() as session:
        for i in range(emails):
            session.add(
                OutreachRecord(
                    email=f"p{i}@example.org",
                    subject="hi",
                    status="sent",
                    sent_at=when,
                    replied=i < replied,
                    call_booked_at=when if i < booked else None,
                    last_contact_at=when,
                )
            )
        for _ in range(runs):
            session.add(
                RunRecord(
                    workflow="outreach",
                    ok=True,
                    cost_usd=cost_per_run,
                    stats={"contactable": contactable_per_run},
                    created_at=when,
                )
            )
        for i in range(won):
            session.add(
                LeadRecord(
                    source="hn_hiring",
                    external_id=f"won-{i}",
                    title="won deal",
                    status=LeadStatus.won,
                    created_at=when,
                )
            )


# --------------------------------------------------------------------------- #
# the real July shape
# --------------------------------------------------------------------------- #
def test_the_real_month_reports_the_top_of_funnel_as_the_bottleneck(temp_db):
    """1 email, 0 replies, 0 contactable across 24 runs — the actual July 2026 data.

    The verdict must point at sourcing, which is where the fix was, and must NOT
    suggest rewriting the pitch.
    """
    _seed(emails=1, contactable_per_run=0, runs=24, cost_per_run=0.3563)

    k = funnel(window_days=30)
    assert k["emailed"] == 1
    assert k["replied"] == 0
    assert k["calls_booked"] == 0
    assert k["contactable"] == 0
    assert 8.0 < k["cost_usd"] < 9.0
    assert "TOP OF FUNNEL EMPTY" in k["verdict"]
    assert "sourcing" in k["verdict"].lower()


def test_empty_database_is_reported_as_empty_not_healthy(temp_db):
    k = funnel()
    assert k["emailed"] == 0
    assert "TOP OF FUNNEL EMPTY" in k["verdict"]


# --------------------------------------------------------------------------- #
# each stage becomes the bottleneck in turn
# --------------------------------------------------------------------------- #
def test_contactable_but_not_sending_blames_the_send_path(temp_db):
    _seed(emails=0, contactable_per_run=12, runs=3)
    k = funnel()
    assert "NOT SENDING" in k["verdict"]
    assert "auto_email" in k["verdict"]


def test_low_volume_no_replies_does_not_blame_the_pitch(temp_db):
    """With 5 emails, zero replies is not yet evidence of anything — advising a
    rewrite here would be advice based on noise."""
    _seed(emails=5, contactable_per_run=10, runs=2)
    k = funnel()
    assert "NO REPLIES" in k["verdict"]
    assert "send more" in k["verdict"]


def test_high_volume_no_replies_does_blame_targeting_or_pitch(temp_db):
    _seed(emails=40, contactable_per_run=50, runs=5)
    k = funnel()
    assert "NO REPLIES" in k["verdict"]
    assert "targeting or the pitch" in k["verdict"]


def test_replies_without_bookings_blames_the_close(temp_db):
    _seed(emails=30, replied=6, contactable_per_run=40, runs=4)
    k = funnel()
    assert k["replied"] == 6
    assert "NO CALLS" in k["verdict"]
    assert "close" in k["verdict"]


def test_bookings_without_wins_says_the_machine_works(temp_db):
    _seed(emails=30, replied=6, booked=2, contactable_per_run=40, runs=4)
    k = funnel()
    assert k["calls_booked"] == 2
    assert "none won yet" in k["verdict"]


def test_full_funnel_reports_working(temp_db):
    _seed(emails=30, replied=6, booked=2, won=1, contactable_per_run=40, runs=4)
    k = funnel()
    assert k["won"] == 1
    assert "WORKING" in k["verdict"]


# --------------------------------------------------------------------------- #
# rates and efficiency
# --------------------------------------------------------------------------- #
def test_conversion_rates(temp_db):
    _seed(emails=20, replied=5, booked=2, won=1, contactable_per_run=30, runs=2)
    k = funnel()
    assert k["reply_rate_pct"] == 25.0        # 5/20
    assert k["booking_rate_pct"] == 40.0      # 2/5
    assert k["win_rate_pct"] == 50.0          # 1/2


def test_rates_are_none_not_zero_when_there_is_no_denominator(temp_db):
    """0.0% invites fixing a stage that had no input; None says 'no data'."""
    _seed(emails=0, contactable_per_run=5, runs=1)
    k = funnel()
    assert k["reply_rate_pct"] is None
    assert k["cost_per_reply_usd"] is None


def test_cost_per_reply_and_per_call(temp_db):
    _seed(emails=10, replied=4, booked=2, contactable_per_run=20, runs=4, cost_per_run=1.0)
    k = funnel()
    assert k["cost_usd"] == 4.0
    assert k["cost_per_reply_usd"] == 1.0   # 4.00 / 4
    assert k["cost_per_call_usd"] == 2.0    # 4.00 / 2


# --------------------------------------------------------------------------- #
# windowing
# --------------------------------------------------------------------------- #
def test_window_excludes_older_activity(temp_db):
    _seed(emails=5, replied=2, contactable_per_run=10, runs=2, days_ago=60)
    assert funnel(window_days=30)["emailed"] == 0
    assert funnel(window_days=90)["emailed"] == 5


def test_failed_and_suppressed_sends_are_not_counted_as_emailed(temp_db):
    """Only status == "sent" is a real send; counting failures would overstate reach."""
    when = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)
    with dbsession.get_session() as session:
        session.add(OutreachRecord(email="a@x.org", status="failed", sent_at=when))
        session.add(OutreachRecord(email="b@x.org", status="suppressed", sent_at=when))
        session.add(OutreachRecord(email="c@x.org", status="sent", sent_at=when))
    assert funnel()["emailed"] == 1


# --------------------------------------------------------------------------- #
# robustness + formatting
# --------------------------------------------------------------------------- #
def test_non_numeric_contactable_stat_does_not_raise(temp_db):
    when = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)
    with dbsession.get_session() as session:
        session.add(
            RunRecord(workflow="outreach", ok=True, stats={"contactable": "x"}, created_at=when)
        )
    assert funnel()["contactable"] == 0


def test_funnel_never_raises_on_a_broken_db(monkeypatch):
    """KPIs feed the notification path; they must degrade, not break a run."""
    import db.session as ds

    def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(ds, "get_session", boom)
    k = funnel()
    assert "error" in k


def test_format_kpis_includes_every_stage(temp_db):
    _seed(emails=10, replied=3, booked=1, contactable_per_run=20, runs=2)
    text = format_kpis(funnel())
    for label in ("contactable", "emailed", "replied", "calls booked", "won", "spend"):
        assert label in text


def test_format_kpis_shows_na_instead_of_zero_percent(temp_db):
    _seed(emails=0, contactable_per_run=3, runs=1)
    assert "(n/a)" in format_kpis(funnel())


def test_format_kpis_handles_an_error_dict():
    assert "unavailable" in format_kpis({"error": "db gone"})


def test_downstream_activity_overrides_a_zero_contactable_count():
    """A window with replies/bookings is not a top-of-funnel failure.

    ``contactable`` comes from RunRecord.stats, so it reads 0 whenever the window's
    runs predate that stat or the window holds no runs. Blaming sourcing there would
    bury the exact outcomes (replies, booked calls) the report exists to surface.
    """
    v = verdict({"contactable": 0, "emailed": 1, "replied": 1, "calls_booked": 0, "won": 0})
    assert "TOP OF FUNNEL EMPTY" not in v
    assert "NO CALLS" in v

    booked = verdict({"contactable": 0, "emailed": 1, "replied": 1, "calls_booked": 1, "won": 0})
    assert "none won yet" in booked


def test_verdict_is_pure_and_ordered_top_down():
    """Bottleneck order matters: an empty top of funnel must win over an empty
    downstream stage, since fixing downstream first changes nothing."""
    assert "TOP OF FUNNEL EMPTY" in verdict(
        {"contactable": 0, "emailed": 0, "replied": 0, "calls_booked": 0, "won": 0}
    )
    assert "NOT SENDING" in verdict(
        {"contactable": 5, "emailed": 0, "replied": 0, "calls_booked": 0, "won": 0}
    )


# ---------------------------------------------------------------------------
# Both channels, not one. `call_booked_at` is stamped only for people we cold-emailed, so
# for a month `calls_booked` could not rise for the channel that actually converted: the
# first real booking this system produced was inbound, from a LinkedIn post, and every
# report said `calls_booked: 0` while a briefing about it sat in the owner's inbox.
# ---------------------------------------------------------------------------
def _booking(**kw):
    from db.models import CallRecord

    defaults = {
        "booking_uid": "uid-1",
        "invitee_name": "Someone Inbound",
        "invitee_email": "someone@gmail.com",
        "origin": "inbound",
        "status": "booked",
        "notified": True,
    }
    return CallRecord(**{**defaults, **kw})


def test_an_inbound_booking_is_counted(temp_db):
    from db.session import get_session

    with get_session() as s:
        s.add(_booking())

    k = kpi.funnel(30)
    assert k["calls_booked"] == 1
    assert k["calls_inbound"] == 1
    assert k["calls_from_outreach"] == 0


def test_an_outreach_booking_evidenced_twice_is_counted_once(temp_db):
    """The stamp on the send AND the CallRecord the sweep wrote are the same call."""
    import datetime as _dt

    from db.models import OutreachRecord
    from db.session import get_session

    now = _dt.datetime.now(_dt.UTC).replace(tzinfo=None)
    with get_session() as s:
        s.add(
            OutreachRecord(
                lead_id=None,
                email="hiring@acme.com",
                status="sent",
                sent_at=now,
                replied=True,
                call_booked_at=now,
            )
        )
        s.add(_booking(invitee_email="hiring@acme.com", origin="outreach", booking_uid="uid-2"))

    k = kpi.funnel(30)
    assert k["calls_from_outreach"] == 1
    assert k["calls_booked"] == 1


def test_reply_to_booking_rate_stays_an_outreach_rate(temp_db):
    """An inbound booking has no reply behind it, so it must not inflate this rate.

    Crediting the reply handler with calls it never touched would make the number that
    judges it unfalsifiable in exactly the direction nobody wants.
    """
    import datetime as _dt

    from db.models import OutreachRecord
    from db.session import get_session

    now = _dt.datetime.now(_dt.UTC).replace(tzinfo=None)
    with get_session() as s:
        for index in range(4):
            s.add(
                OutreachRecord(
                    lead_id=None,
                    email=f"lead{index}@acme.com",
                    status="sent",
                    sent_at=now,
                    replied=True,
                )
            )
        s.add(_booking(booking_uid="uid-3"))
        s.add(_booking(booking_uid="uid-4", invitee_email="other@gmail.com"))

    k = kpi.funnel(30)
    assert k["replied"] == 4
    assert k["calls_inbound"] == 2
    assert k["calls_booked"] == 2
    assert k["booking_rate_pct"] == 0.0  # not 50% — no reply produced either booking


def test_a_cancelled_call_is_not_a_booked_call(temp_db):
    from db.session import get_session

    with get_session() as s:
        s.add(_booking(status="cancelled"))

    assert kpi.funnel(30)["calls_booked"] == 0


def test_the_verdict_names_the_channel_that_converted(temp_db):
    """The ladder used to answer "rewrite the cold-email pitch" to a week with a booked call.

    Ranking a channel that produced a call below a channel that produced zero replies is
    how a month gets spent tuning the losing half of the system.
    """
    line = kpi.verdict(
        {
            "contactable": 30,
            "emailed": 23,
            "replied": 1,
            "calls_booked": 1,
            "calls_inbound": 1,
            "calls_from_outreach": 0,
            "posts_published": 3,
            "won": 0,
        }
    )
    assert "INBOUND IS THE CHANNEL THAT CONVERTS" in line
    assert "3 post(s)" in line
    assert "23 cold email" in line


def test_the_outreach_verdict_is_unchanged_when_outreach_is_what_converted():
    line = kpi.verdict(
        {
            "contactable": 30,
            "emailed": 23,
            "replied": 5,
            "calls_booked": 2,
            "calls_inbound": 0,
            "calls_from_outreach": 2,
            "posts_published": 3,
            "won": 0,
        }
    )
    assert "call(s) booked, none won yet" in line


def test_the_digest_splits_the_channels(temp_db):
    from db.session import get_session

    with get_session() as s:
        s.add(_booking())

    text = kpi.format_kpis(kpi.funnel(30))
    assert "from outreach" in text
    assert "inbound" in text
    assert "post(s) published" in text
