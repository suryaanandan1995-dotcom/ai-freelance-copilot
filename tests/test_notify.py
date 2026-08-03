"""Offline tests for the notification digest (no real SMTP / HTTP)."""
from __future__ import annotations

import config
import interfaces.notify as notify


def _stats():
    return {
        "fetched": 10,
        "new": 8,
        "queued": 3,
        "dropped": 5,
        "skipped": 2,
        "cost_usd": 0.1234,
        "budget_exhausted": False,
    }


def _top():
    return [
        {"id": 1, "title": "K8s hardening", "fit_score": 92},
        {"id": 2, "title": "CI/CD security", "fit_score": 81},
    ]


def _settings_with(monkeypatch, **overrides):
    real_get = config.get_settings

    def patched():
        s = real_get()
        for key, value in overrides.items():
            setattr(s, key, value)
        return s

    monkeypatch.setattr(notify, "get_settings", patched)


def test_email_noop_when_smtp_host_empty(monkeypatch):
    _settings_with(monkeypatch, notify_channel="email", smtp_host="")
    assert notify.send_digest(_stats(), _top()) is False


def test_channel_none_returns_false(monkeypatch):
    _settings_with(monkeypatch, notify_channel="none")
    assert notify.send_digest(_stats(), _top()) is False


class _FakeSMTP:
    """Minimal context-manager stand-in for smtplib.SMTP."""

    sent = []
    instances = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        _FakeSMTP.instances.append(self)
        self.logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        return (250, b"ok")

    def starttls(self):
        return (220, b"ready")

    def login(self, user, password):
        self.logged_in = True

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


def test_email_composes_and_sends_when_configured(monkeypatch):
    _FakeSMTP.sent = []
    _FakeSMTP.instances = []
    _settings_with(
        monkeypatch,
        notify_channel="email",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="secret",
        smtp_from="bot@example.com",
        notify_email_to="me@example.com",
        dashboard_base_url="http://localhost:8000",
    )
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)

    ok = notify.send_digest(_stats(), _top())
    assert ok is True
    assert len(_FakeSMTP.sent) == 1

    msg = _FakeSMTP.sent[0]
    assert msg["From"] == "bot@example.com"
    assert msg["To"] == "me@example.com"
    # The subject reports drafts AND sends: a draft count alone read as healthy
    # through a month of zero emails, so both numbers are now in the one line
    # guaranteed to be seen.
    assert "3 draft(s)" in msg["Subject"]
    assert "email(s) sent" in msg["Subject"]

    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "K8s hardening" in body
    assert "http://localhost:8000/lead/1" in body
    # login attempted because smtp_user is set
    assert _FakeSMTP.instances[0].logged_in is True


def test_email_swallows_exceptions(monkeypatch):
    _settings_with(monkeypatch, notify_channel="email", smtp_host="smtp.example.com")

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(notify.smtplib, "SMTP", boom)
    # Must never raise; degrades to False.
    assert notify.send_digest(_stats(), _top()) is False


# --------------------------------------------------------------------------- #
# outcome-first reporting
# --------------------------------------------------------------------------- #
def _email_settings(monkeypatch):
    _FakeSMTP.sent = []
    _FakeSMTP.instances = []
    _settings_with(
        monkeypatch,
        notify_channel="email",
        smtp_host="smtp.example.com",
        smtp_user="",
        smtp_from="bot@example.com",
        notify_email_to="me@example.com",
    )
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)


def test_subject_shouts_when_nothing_was_queued(monkeypatch):
    """A run that drafts nothing must not look like a normal run in the inbox.

    17 of 24 production runs queued zero and every one of them arrived with a
    business-as-usual subject line.
    """
    _email_settings(monkeypatch)
    stats = dict(_stats(), queued=0, emailed=0, contactable=6)

    assert notify.send_digest(stats, []) is True
    assert "NOTHING QUEUED" in _FakeSMTP.sent[0]["Subject"]


def test_subject_shouts_when_there_are_no_contactable_leads(monkeypatch):
    """The top-of-funnel failure gets its own subject, because it needs a different
    fix (sourcing) than a drafting failure (thresholds/prompts)."""
    _email_settings(monkeypatch)
    stats = dict(_stats(), queued=0, emailed=0, contactable=0)

    assert notify.send_digest(stats, []) is True
    assert "NO CONTACTABLE LEADS" in _FakeSMTP.sent[0]["Subject"]


def test_body_reports_contactable_and_emailed(monkeypatch):
    _email_settings(monkeypatch)
    stats = dict(_stats(), contactable=4, emailed=2)

    notify.send_digest(stats, _top())
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "Contactable" in body
    assert "Emailed" in body


def test_body_explains_why_leads_were_not_emailed(monkeypatch):
    """emailed_skipped was the single most diagnostic field and it was never shown."""
    _email_settings(monkeypatch)
    stats = dict(_stats(), emailed=0, emailed_skipped={"no_email_pregate": 7, "low_fit": 2})

    notify.send_digest(stats, _top())
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "no_email_pregate=7" in body
    assert "low_fit=2" in body


def test_body_names_the_fit_bottleneck_when_present(monkeypatch):
    _email_settings(monkeypatch)
    stats = dict(
        _stats(),
        queued=0,
        fit={
            "n": 12,
            "threshold": 70,
            "p50": 64,
            "max": 68,
            "bottleneck": "threshold: 9/12 scored within 10 points of 70.",
        },
    )

    notify.send_digest(stats, [])
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "Bottleneck" in body
    assert "within 10 points" in body


def test_digest_still_sends_when_kpis_are_unavailable(monkeypatch):
    """KPIs are a nice-to-have in the digest; they must never block the digest."""
    _email_settings(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(notify, "_kpi_block", lambda: "")
    assert notify.send_digest(_stats(), _top()) is True
