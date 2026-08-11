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


# --------------------------------------------------------------------------- #
# "Apply yourself" — qualified leads with no contact address
#
# Measured 2026-08-07: contract_jobs (the day-rate feed, aimed squarely at paid
# contract work) fetched 36 leads scoring up to 78 and produced ZERO outreach, because
# those listings carry an apply form and no address. They were scored at real cost and
# then dropped in silence. auto_submit is permanently off, so a human hand-off is the
# ONLY route these leads have — which makes the digest the only place they can appear.
# --------------------------------------------------------------------------- #
def _apply_stats(n: int = 2) -> dict:
    return {
        "queued": 0,
        "apply_yourself": [
            {
                "title": f"Contract role {i}",
                "url": f"https://boards.example.com/job/{i}",
                "company": f"Company {i}",
                "source": "contract_jobs",
                "fit_score": 80 - i,
            }
            for i in range(n)
        ],
    }


def test_uncontactable_qualified_leads_reach_the_digest():
    from interfaces.notify import _plaintext

    text = _plaintext(_apply_stats(2), [], "http://localhost:8000")
    assert "APPLY YOURSELF" in text
    # The real listing URL, not a dashboard link: with no hosted UI this link IS the
    # entire hand-off, so it has to work on its own.
    assert "https://boards.example.com/job/0" in text
    assert "/lead/" not in text.split("APPLY YOURSELF")[1]


def test_the_apply_section_is_absent_when_there_is_nothing_to_apply_to():
    """No empty scaffolding: a section that always renders stops being read."""
    from interfaces.notify import _plaintext

    text = _plaintext({"queued": 0}, [], "http://localhost:8000")
    assert "APPLY YOURSELF" not in text


def test_the_apply_section_says_how_many_it_is_not_showing():
    """A truncated list must state the total AND what the cut removed.

    The first production run produced 46 qualified uncontactable leads; the cap was 5,
    so 41 were hidden behind "and 41 more" — no way to tell whether they were worth
    looking at. Because the list is sorted best-fit-first, truncation is a score cut, so
    the note names the score it cut at and the setting that shortens the list.
    """
    from interfaces.notify import _APPLY_SHOWN, _plaintext

    n = _APPLY_SHOWN + 7
    text = _plaintext(_apply_stats(n), [], "http://localhost:8000")
    assert f"{n} qualified lead(s)" in text
    assert "and 7 more" in text
    # _apply_stats scores descend as 80 - i, so the first hidden lead scores this.
    assert f"score {80 - _APPLY_SHOWN} or below" in text
    # Name the lever, not just the symptom.
    assert "min_fit_score" in text


def test_the_apply_section_shows_a_realistic_days_volume_in_full():
    """46 qualified leads/run is the measured reality, not a hypothetical.

    The cap was 5, set before any run measured the volume, which meant the digest
    re-imposed a second bar far above the min_fit_score these leads had already cleared
    — hiding 41 of 46. The section exists precisely so the automation's output reaches
    the owner; a cap that drops 89% of it recreates the bug at one order of magnitude
    smaller.
    """
    from interfaces.notify import _plaintext

    text = _plaintext(_apply_stats(46), [], "http://localhost:8000")
    # Everything the owner set the bar for is listed, not just a teaser.
    assert text.count("https://boards.example.com/job/") >= 30
    assert "and 16 more" in text


def test_the_html_digest_does_not_show_fewer_leads_than_the_plaintext():
    """Both parts are the same message; a cap applied to one is a silent discrepancy.

    The HTML alternative is what a mail client renders, so if it truncated shorter than
    the plaintext the owner would never see the difference existed.
    """
    from interfaces.notify import _html, _plaintext

    stats = _apply_stats(46)
    html = _html(stats, [], "http://localhost:8000")
    text = _plaintext(stats, [], "http://localhost:8000")
    assert html.count("https://boards.example.com/job/") == text.count(
        "https://boards.example.com/job/"
    )
    # And the HTML says what it cut, in the same terms.
    assert "and 16 more" in html
    assert "46 qualified" in html


def test_apply_leads_are_ordered_best_fit_first():
    """The list is capped, so ordering decides what the owner actually sees."""
    from interfaces.notify import _plaintext

    stats = _apply_stats(3)
    text = _plaintext(stats, [], "http://localhost:8000")
    body = text.split("APPLY YOURSELF")[1]
    assert body.index("[80]") < body.index("[79]") < body.index("[78]")


def test_third_party_job_titles_are_escaped_in_the_html_digest():
    """Titles and companies come from remote job posts.

    The HTML digest is assembled by f-string concatenation, so unescaped remote text
    in it is an injection into the owner's own inbox.
    """
    from interfaces.notify import _html

    stats = {
        "queued": 0,
        "apply_yourself": [
            {
                "title": "<script>alert(1)</script>",
                "url": 'https://x.example/j"onmouseover="alert(1)',
                "company": "Acme <b>&</b> Co",
                "source": "contract_jobs",
                "fit_score": 77,
            }
        ],
    }
    html = _html(stats, [], "http://localhost:8000")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert 'onmouseover="alert(1)"' not in html
    assert "&amp;" in html


# --------------------------------------------------------------------------- #
# a rejecting filter is a targeting note, not a source to retire
# --------------------------------------------------------------------------- #
def test_a_filtered_out_source_is_not_listed_among_the_dead(monkeypatch):
    """Ordering carries the advice, so the two cases must not share a rank.

    "dead" sits at the top of the table under "retire what never produces". A source that
    read a full feed and matched none of it needs the opposite response — widen the
    keyword list — so it ranks with off-ICP, the other targeting verdict, below the
    genuinely silent sources.
    """
    _email_settings(monkeypatch)
    rows = dict(_by_source())
    rows["hn_freelancer"] = {
        "scanned": 13, "fetched": 0, "new": 0, "contactable": 0, "queued": 0,
        "verdict": "filtered-out: read 13 listing(s), none matched the skill keywords "
                   "— widen the keywords, not the sources",
    }

    notify.send_digest(dict(_stats(), by_source=rows), _top())
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()

    # Below the genuinely dead source, above the productive one.
    assert body.index("uk_contract") < body.index("hn_freelancer")
    assert body.index("hn_freelancer") < body.index("hn_hiring")


def test_the_table_reports_how_much_each_source_read(monkeypatch):
    """`read` is what makes fetched=0 legible.

    read=13 fetched=0 is a filter decision; read=- fetched=0 is a silent upstream. Both
    printed as fetched=0 and nothing else, so the table could not tell them apart.
    """
    _email_settings(monkeypatch)
    rows = {
        "hn_freelancer": {
            "scanned": 13, "fetched": 0, "new": 0, "contactable": 0, "queued": 0,
            "verdict": "filtered-out: read 13 listing(s), none matched the skill keywords",
        },
        "upwork_rss": {
            "fetched": 0, "new": 0, "contactable": 0, "queued": 0,
            "verdict": "dead: fetched nothing",
        },
    }

    notify.send_digest(dict(_stats(), by_source=rows), _top())
    body = _FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()

    assert "read=13" in body
    # A source that reports no count prints a dash, not a zero that it never measured.
    assert "read=-" in body


def test_the_subject_does_not_call_a_filtered_source_dead(monkeypatch):
    """`EVERY SOURCE DEAD` is a sourcing emergency; a narrow keyword list is not.

    The subject is the only part guaranteed to be read, so a filter verdict misreported
    there sends the owner to replace feeds that work.
    """
    _email_settings(monkeypatch)
    rows = {
        "hn_freelancer": {
            "scanned": 13, "fetched": 0, "new": 0, "contactable": 0, "queued": 0,
            "verdict": "filtered-out: read 13 listing(s), none matched the skill keywords",
        },
    }

    notify.send_digest(
        dict(_stats(), queued=0, emailed=0, contactable=0, by_source=rows), []
    )
    subject = _FakeSMTP.sent[0]["Subject"]
    assert "DEAD" not in subject
