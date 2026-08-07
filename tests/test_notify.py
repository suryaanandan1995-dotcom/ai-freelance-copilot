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


# --------------------------------------------------------------------------- #
# per-source attribution in the digest
# --------------------------------------------------------------------------- #
def _by_source() -> dict:
    return {
        "hn_hiring": {
            "fetched": 12, "new": 12, "contactable": 7, "queued": 3,
            "verdict": "productive: 3 cleared 70",
        },
        "jobicy": {
            "fetched": 20, "new": 20, "contactable": 18, "queued": 0,
            "verdict": "off-ICP: best score 22 < 70",
        },
        "uk_contract": {
            "fetched": 0, "new": 0, "contactable": 0, "queued": 0,
            "verdict": "dead: fetched nothing",
        },
    }


def test_body_attributes_the_funnel_to_each_source(monkeypatch):
    """The digest is the only thing read, so the per-source table has to be in it.

    ``fit.bottleneck`` says the mix is off-ICP but not which sources produced it, so the
    actual fix — retire the dead ones, widen the productive one — stays invisible.
    """
    _email_settings(monkeypatch)

    notify.send_digest(dict(_stats(), by_source=_by_source()), _top())
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()

    assert "BY SOURCE" in body
    for name in ("hn_hiring", "jobicy", "uk_contract"):
        assert name in body
    assert "dead: fetched nothing" in body


def test_dead_sources_are_listed_before_the_working_ones(monkeypatch):
    """Ordering is the feature: a dead source buried under working ones is how
    uk_contract stayed DISABLED for a month of green runs."""
    _email_settings(monkeypatch)

    notify.send_digest(dict(_stats(), by_source=_by_source()), _top())
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()

    assert body.index("uk_contract") < body.index("jobicy") < body.index("hn_hiring")


def test_subject_names_a_dead_source_even_on_a_successful_run(monkeypatch):
    """A run can queue drafts and still have a broken source. The old subject read
    as unqualified success, so the breakage never surfaced."""
    _email_settings(monkeypatch)

    notify.send_digest(dict(_stats(), queued=3, emailed=1, by_source=_by_source()), _top())
    assert "dead source(s)" in _FakeSMTP.sent[0]["Subject"]


def test_subject_shouts_when_every_source_is_dead(monkeypatch):
    """Total sourcing failure is a different emergency from unreachable leads."""
    _email_settings(monkeypatch)
    dead_only = {
        name: {"fetched": 0, "new": 0, "contactable": 0, "queued": 0,
               "verdict": "dead: fetched nothing"}
        for name in ("uk_contract", "upwork_rss")
    }

    notify.send_digest(
        dict(_stats(), queued=0, emailed=0, contactable=0, by_source=dead_only), []
    )
    subject = _FakeSMTP.sent[0]["Subject"]
    assert "EVERY SOURCE DEAD" in subject
    assert "uk_contract" in subject


def test_starved_sources_rank_just_below_dead_ones(monkeypatch):
    """`starved` is a cap problem, `dead` is a source problem — both outrank off-ICP.

    A starved source looks fine in the fetched column, so if it sorted last nobody would
    notice the run cap was hiding six sources.
    """
    _email_settings(monkeypatch)
    rows = dict(_by_source())
    rows["working_nomads"] = {
        "fetched": 30, "considered": 0, "new": 0, "contactable": 0, "queued": 0,
        "verdict": "starved: fetched 30, none considered — raise max_leads_per_run",
    }

    notify.send_digest(dict(_stats(), by_source=rows), _top())
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()

    assert body.index("uk_contract") < body.index("working_nomads")
    assert body.index("working_nomads") < body.index("jobicy")
    # fetched=30 next to "none considered" is what makes the verdict believable.
    assert "considered=0" in body


def test_a_broken_source_sorts_above_a_dead_one(monkeypatch):
    """`broken` is the only verdict that is fixable today, so it reads first."""
    _email_settings(monkeypatch)
    rows = dict(_by_source())
    rows["contra_startup"] = {
        "fetched": 0, "considered": 0, "new": 0, "contactable": 0, "queued": 0,
        "error": "HTTPStatusError: 400 Bad Request",
        "verdict": "broken: HTTPStatusError: 400 Bad Request",
    }

    notify.send_digest(dict(_stats(), by_source=rows), _top())
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()

    assert body.index("contra_startup") < body.index("uk_contract")
    assert "400 Bad Request" in body


def test_subject_names_a_broken_source_even_on_a_run_that_queued_drafts(monkeypatch):
    """A rejected request is a code defect; three drafts must not bury it.

    uk_contract's 400 survived a month of green runs whose subjects read as successes.
    """
    _email_settings(monkeypatch)
    rows = dict(_by_source())
    rows["uk_contract"] = {
        "fetched": 0, "considered": 0, "new": 0, "contactable": 0, "queued": 0,
        "error": "HTTPStatusError: 400 Bad Request",
        "verdict": "broken: HTTPStatusError: 400 Bad Request",
    }

    notify.send_digest(dict(_stats(), by_source=rows), _top())
    subject = _FakeSMTP.sent[0]["Subject"]
    assert "3 draft(s)" in subject, "precondition: this is a successful-looking run"

    assert "uk_contract" in subject
    assert "BROKEN" in subject


def test_digest_without_source_data_omits_the_section(monkeypatch):
    """Older runs (and the reply/followup digests) carry no by_source key."""
    _email_settings(monkeypatch)

    notify.send_digest(_stats(), _top())
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "BY SOURCE" not in body


def test_digest_still_sends_when_kpis_are_unavailable(monkeypatch):
    """KPIs are a nice-to-have in the digest; they must never block the digest."""
    _email_settings(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(notify, "_kpi_block", lambda: "")
    assert notify.send_digest(_stats(), _top()) is True


def test_a_source_that_scores_well_but_queues_nothing_is_not_buried(monkeypatch):
    """`no-output` must outrank `email-blocked`, and it had no entry in the rank map.

    Unlisted verdicts fall to the default rank 9, which is BELOW email-blocked (7) —
    i.e. dead last, where the eye reads "nothing to do here". `no-output` means the
    opposite: the source scored well and still delivered nothing, so something between
    the score and the queue is eating its leads. Measured live on remote_boards
    (run 31172835060): 8 of 22 scores cleared 70, 0 queued.
    """
    _email_settings(monkeypatch)
    rows = dict(_by_source())
    rows["remote_boards"] = {
        "fetched": 68, "considered": 44, "new": 22, "contactable": 1, "queued": 0,
        "verdict": (
            "no-output: 0 queued, though 8 of 22 scores cleared 70 (1/22 contactable) "
            "— clearing the bar is not output"
        ),
    }
    rows["contract_jobs"] = {
        "fetched": 46, "considered": 44, "new": 27, "contactable": 0, "queued": 0,
        "verdict": (
            "email-blocked: 27 new leads, none with a contact (best score 82) "
            "— human-submit channel only"
        ),
    }

    notify.send_digest(dict(_stats(), by_source=rows), _top())
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()

    # Above the routing note, and above the source that is simply working.
    assert body.index("remote_boards") < body.index("contract_jobs")
    assert body.index("remote_boards") < body.index("hn_hiring")
    # But still below the verdicts that name their own cause.
    assert body.index("uk_contract") < body.index("remote_boards")
    assert body.index("jobicy") < body.index("remote_boards")
