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
    assert "3 new proposal draft" in msg["Subject"]

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
