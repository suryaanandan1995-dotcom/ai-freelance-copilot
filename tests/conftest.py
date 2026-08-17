"""Test-suite isolation from the developer's real environment.

``Settings`` reads ``.env`` by design, which is right for production but wrong for
tests: whatever happens to be in a contributor's ``.env`` silently changes what the
suite asserts. Two real instances of that, both found on 2026-08-03:

* ``COPILOT_DASHBOARD_PASSWORD`` in ``.env`` turned on HTTP Basic auth, so 15
  ``test_dashboard.py`` tests got 401 instead of 200 — failures that had nothing to
  do with the code under test.
* ``COPILOT_DATABASE_URL`` pointed at a live Postgres, so tests could write to it.

Environment variables take priority over ``.env`` in pydantic-settings, so setting
them explicitly here pins the suite to safe, offline values regardless of ``.env``.
"""
from __future__ import annotations

import pytest

#: Pinned for every test. Values are the safe offline defaults from config.py.
_TEST_ENV: dict[str, str] = {
    # Auth off — dashboard tests assert unauthenticated 200s.
    "COPILOT_DASHBOARD_PASSWORD": "",
    # Never let a test touch a real database.
    "COPILOT_DATABASE_URL": "sqlite:///:memory:",
    # No network in unit tests: fixture domains ("acme.io", "startup.dev") do not
    # publish MX records, so the production deliverability gate would discard
    # every fixture lead. The gate itself is covered by its own targeted tests.
    "COPILOT_VERIFY_CONTACT_DOMAIN": "false",
    # Nothing sends, posts, or replies from a test run.
    "COPILOT_AUTO_EMAIL": "false",
    "COPILOT_AUTO_REPLY": "false",
    "COPILOT_ALLOW_SEND": "false",
    "COPILOT_DRY_RUN": "true",
    "COPILOT_LINKEDIN_AUTO_POST": "false",
    "COPILOT_LINKEDIN_ACCESS_TOKEN": "",
    "COPILOT_SELF_OPTIMIZE": "false",
    # No live Claude calls: tests inject FakeChat.
    "COPILOT_ANTHROPIC_API_KEY": "",
    "COPILOT_SMTP_HOST": "",
    # The credentials too, not just the host: monitor.doctor._check_imap_login performs
    # a REAL IMAP login, so a contributor's .env could otherwise make the offline suite
    # open a network connection and authenticate as them.
    "COPILOT_SMTP_USER": "",
    "COPILOT_SMTP_PASSWORD": "",
    "COPILOT_IMAP_HOST": "",
    # These two decide the CONTENTS of the source registry: ``reddit_forhire`` joins it
    # only when both are set. Unpinned, configuring the Reddit app locally turns
    # tests/test_sources.py::test_get_default_sources_are_the_live_ones red by set
    # equality — a green suite that goes red on a correct production change, which is
    # exactly the leak this file exists to stop. Tests that want the source present set
    # them explicitly instead.
    "COPILOT_REDDIT_CLIENT_ID": "",
    "COPILOT_REDDIT_CLIENT_SECRET": "",
    # Contact discovery makes REAL outbound HTTP requests, and it is on by default in
    # production. Left unpinned it ran during the offline suite and reached the live
    # internet: a fixture lead for "Acme Corp" resolved acme.com, fetched its homepage
    # and returned frobozz07@mail.acme.com — a stranger's address, from a unit test, in a
    # suite whose whole premise is that it touches no network. Tests that exercise
    # discovery turn it on and inject an httpx.MockTransport.
    "COPILOT_DISCOVER_CONTACTS": "false",
    # Apply packs are an extra Opus call per qualified-uncontactable lead, on by default in
    # production. Left on here they spend an injected FakeChat's scripted responses inside
    # tests that are counting model calls to prove something else entirely — the pre-gate
    # test asserting "exactly one call: qualification" started reading 2. Tests that cover
    # packs enable the flag themselves.
    "COPILOT_APPLY_PACKS": "false",
}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Pin env vars that ``.env`` would otherwise leak into every test."""
    for key, value in _TEST_ENV.items():
        monkeypatch.setenv(key, value)
    yield
