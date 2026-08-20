"""Offline tests for the health monitor ("doctor")."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as dbsession
import monitor.doctor as doctor
from db.models import Base, RunRecord


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr(dbsession, "engine", engine)
    monkeypatch.setattr(dbsession, "SessionLocal", SessionLocal)
    Base.metadata.create_all(engine)
    yield engine


def _no_linkedin(monkeypatch):
    # Ensure the LinkedIn check is skipped (no token) unless a test sets one.
    from config import Settings

    monkeypatch.setattr(
        doctor,
        "run_healthcheck",
        doctor.run_healthcheck,  # keep real function
    )
    # patch get_settings used inside _check_linkedin_token
    import config

    base = Settings(linkedin_access_token="")
    monkeypatch.setattr(config, "get_settings", lambda: base)


def test_all_healthy_no_alert(temp_db, monkeypatch):
    _no_linkedin(monkeypatch)
    alerts = []
    import runlog

    monkeypatch.setattr(runlog, "send_alert", lambda subject, body: alerts.append(subject))

    result = doctor.run_healthcheck()
    assert result["ok"] is True
    assert result["issues"] == []
    assert alerts == []  # nothing to alert
    names = {c["name"] for c in result["checks"]}
    assert names == {
        "schema",
        "database",
        "recent_runs",
        "linkedin_token",
        # Proves the IMAP credentials work instead of inferring it from silence, which
        # the reply_detection check below cannot do until 25 emails have gone out.
        "imap_login",
        # Output-based funnel checks (monitor/funnel.py): a run that "succeeds"
        # while emailing nobody is the failure mode that went unnoticed for a
        # month, so these must always be part of the healthcheck surface.
        "outreach_flow",
        "queue_flow",
        "contactable_supply",
        # The inbound half. fetch_replies swallows IMAP errors and returns [], so a
        # broken reader is indistinguishable from a quiet mailbox — and an undetected
        # reply never stops the follow-up sequence, so the system keeps nudging people
        # who already answered.
        "reply_detection",
        # Contact discovery is the lever on the biggest loss in the funnel (262 of 269
        # qualified leads published no address), and it fails silently: it fetches other
        # people's sites, so a blocked user-agent looks exactly like "nobody publishes
        # an address". Watched on attempts vs hits, never on hits alone.
        "discovery",
        # The lead loop now survives one lead's exception, which converts a loud total
        # failure into a quiet partial one. This is what stops the quiet one — a run that
        # skipped 60 of 175 leads and still exited 0 — from reading as healthy.
        "lead_errors",
    }


def test_recent_failure_is_flagged_and_alerts(temp_db, monkeypatch):
    _no_linkedin(monkeypatch)
    alerts = []
    import runlog

    monkeypatch.setattr(runlog, "send_alert", lambda subject, body: alerts.append((subject, body)))

    # seed a failed run
    with dbsession.get_session() as s:
        s.add(RunRecord(workflow="optimize", ok=False, error="boom"))

    result = doctor.run_healthcheck()
    assert result["ok"] is False
    assert any("optimize" in i for i in result["issues"])
    assert alerts and "issue" in alerts[0][0].lower()


def test_linkedin_invalid_token_flagged(temp_db, monkeypatch):
    import config
    from config import Settings

    monkeypatch.setattr(config, "get_settings", lambda: Settings(linkedin_access_token="tok"))

    # make the LinkedIn client raise LinkedInError on whoami
    import linkedin.client as lc

    def boom_whoami(self):
        raise lc.LinkedInError("Invalid access token")

    monkeypatch.setattr(lc.LinkedInClient, "whoami", boom_whoami)

    alerts = []
    import runlog

    monkeypatch.setattr(runlog, "send_alert", lambda subject, body: alerts.append(subject))

    result = doctor.run_healthcheck()
    assert result["ok"] is False
    assert any("token" in i.lower() for i in result["issues"])


def test_format_report_readable():
    result = {
        "ok": False,
        "checks": [{"name": "database", "ok": False, "detail": "database unreachable: x"}],
        "auto_fixed": ["outreach.replied"],
        "issues": ["database unreachable: x"],
    }
    text = doctor.format_report(result)
    assert "ISSUES FOUND" in text
    assert "outreach.replied" in text
    assert "database unreachable" in text


# --------------------------------------------------------------------------- #
# IMAP host derivation + a real login check
#
# Reading replies uses the SMTP credentials, so the IMAP host must belong to the same
# provider as smtp_host. It used to be an independent setting defaulting to
# imap.gmail.com, which meant any non-Gmail SMTP provider read from a server that would
# refuse those credentials — and fetch_replies degrades every IMAP error to [], so the
# symptom was "nobody ever replies" plus follow-ups nudging people who had.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "smtp_host,expected",
    [
        ("smtp.gmail.com", "imap.gmail.com"),
        ("smtp.zoho.eu", "imap.zoho.eu"),
        ("smtp.fastmail.com", "imap.fastmail.com"),
        ("mail.privateemail.com", "imap.privateemail.com"),
        # Office 365 breaks the label-swap pattern, so it is named explicitly.
        ("smtp.office365.com", "outlook.office365.com"),
        ("smtp-mail.outlook.com", "outlook.office365.com"),
        # Already an IMAP-style or bare host: pass through rather than mangle it.
        ("imap.example.com", "imap.example.com"),
        ("example.com", "example.com"),
    ],
)
def test_imap_host_is_derived_from_the_smtp_provider(smtp_host, expected):
    from config import Settings

    assert Settings(smtp_host=smtp_host, imap_host="").resolved_imap_host() == expected


def test_an_explicit_imap_host_always_wins():
    """A provider that breaks the naming pattern must stay configurable."""
    from config import Settings

    cfg = Settings(smtp_host="smtp.gmail.com", imap_host="mail.weirdhost.net")
    assert cfg.resolved_imap_host() == "mail.weirdhost.net"


def test_no_smtp_host_means_no_imap_host_rather_than_a_guess():
    """Unconfigured must read as unconfigured, not as "try Gmail"."""
    from config import Settings

    assert Settings(smtp_host="", imap_host="").resolved_imap_host() == ""


def test_imap_login_check_skips_cleanly_when_unconfigured(monkeypatch):
    import config
    from config import Settings

    monkeypatch.setattr(config, "get_settings", lambda: Settings(smtp_user="", smtp_password=""))
    check = doctor._check_imap_login()
    assert check["ok"] is True
    assert "skipped" in check["detail"]


def test_imap_login_failure_is_reported_with_the_host_and_the_consequence(monkeypatch):
    """The whole point of this check: a refused login must become a visible issue.

    fetch_replies cannot report this — it returns [] on every IMAP error by design, so
    the pipeline stays green. If the doctor also stayed quiet, a wrong host would only
    ever show up as a market that never answers.
    """
    import imaplib

    import config
    from config import Settings

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: Settings(smtp_host="smtp.zoho.eu", imap_host="", smtp_user="me@x.io",
                         smtp_password="pw"),
    )

    def refuse(host, port, timeout=None):
        raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", refuse)
    check = doctor._check_imap_login()
    assert check["ok"] is False
    assert "imap.zoho.eu" in check["detail"]          # names the host it tried
    assert "derived from smtp_host" in check["detail"]  # and where that came from
    assert "follow-ups" in check["detail"]             # and why it matters


def test_imap_login_success_names_the_host_it_verified(monkeypatch):
    import imaplib

    import config
    from config import Settings

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: Settings(smtp_host="smtp.gmail.com", imap_host="", smtp_user="me@x.io",
                         smtp_password="pw"),
    )

    class OK:
        def __init__(self, host, port, timeout=None):
            self.host = host

        def login(self, user, password):
            return ("OK", [b""])

        def logout(self):
            return ("BYE", [b""])

    monkeypatch.setattr(imaplib, "IMAP4_SSL", OK)
    check = doctor._check_imap_login()
    assert check["ok"] is True
    assert "imap.gmail.com" in check["detail"]


@pytest.mark.parametrize(
    "relay",
    [
        "smtp.sendgrid.net",
        "smtp-relay.brevo.com",
        "smtp.resend.com",
        "email-smtp.eu-west-1.amazonaws.com",  # SES is regional: matched by shape
    ],
)
def test_send_only_relays_yield_no_imap_host(relay):
    """A relay exposes no IMAP server, so there is nothing to derive.

    Naively swapping the label would produce imap.sendgrid.net — a host that does not
    exist — and the login check would then report a credentials problem when the real
    problem is that replies land in a different mailbox entirely.
    """
    from config import Settings

    assert Settings(smtp_host=relay, imap_host="").resolved_imap_host() == ""


def test_a_send_only_relay_without_an_imap_override_is_a_reported_defect(monkeypatch):
    """"No IMAP host" is only benign when nothing is configured at all.

    Sending through a relay while reading nowhere is the exact silent failure this pair
    of checks exists to surface, so it must not be filed under "skipped".
    """
    import config
    from config import Settings

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: Settings(smtp_host="smtp.sendgrid.net", imap_host="", smtp_user="apikey",
                         smtp_password="pw"),
    )
    check = doctor._check_imap_login()
    assert check["ok"] is False
    assert "send-only relay" in check["detail"]
    assert "COPILOT_IMAP_HOST" in check["detail"]


def test_an_imap_override_makes_a_relay_setup_valid(monkeypatch):
    """The relay case is configuration, not an unsupported setup."""
    import imaplib

    import config
    from config import Settings

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: Settings(smtp_host="smtp.sendgrid.net", imap_host="imap.gmail.com",
                         smtp_user="me@x.io", smtp_password="pw"),
    )

    class OK:
        def __init__(self, host, port, timeout=None):
            pass

        def login(self, user, password):
            return ("OK", [b""])

        def logout(self):
            return ("BYE", [b""])

    monkeypatch.setattr(imaplib, "IMAP4_SSL", OK)
    check = doctor._check_imap_login()
    assert check["ok"] is True
    assert "imap.gmail.com" in check["detail"]
