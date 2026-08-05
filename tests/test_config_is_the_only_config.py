"""Every setting must be reachable from ``.env``.

pydantic-settings loads ``.env`` into the ``Settings`` object and **never** into
``os.environ``. So any module that reads ``os.environ.get("COPILOT_…")`` directly is
invisible to the file the operator actually edits: the value is set, and ignored.

That failure mode is worse than a crash. The source reports itself DISABLED, or quietly
falls back to a default, and the message reads as "you never configured it" when the
truth is "your config is being ignored" — so the operator re-applies a fix that was
already applied. Three sources shipped this way (contract_jobs, contra_startup,
upwork_rss); upwork_rss has no default feed list, so being ignored switched it off
entirely while it still reported a successful, empty fetch.

This is a lint, not a unit test: it guards the convention rather than any one caller.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from config import Settings

REPO = Path(__file__).resolve().parent.parent

#: Files allowed to mention COPILOT_ env vars: config.py owns the mapping, and tests
#: legitimately set them to exercise it.
_ALLOWED = {"config.py"}

_DIRECT_READ = re.compile(
    r"""os\.(?:environ\.get|getenv)\(\s*["']COPILOT_[A-Z0-9_]+["']"""
)


def _python_files():
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        parts = set(rel.parts)
        if parts & {".venv", "build", "__pycache__", "tests"}:
            continue
        if rel.name in _ALLOWED:
            continue
        yield rel, path


def test_no_module_reads_a_copilot_env_var_directly():
    offenders: list[str] = []
    for rel, path in _python_files():
        text = path.read_text(encoding="utf-8")
        # Skip docstring/comment mentions: only flag real calls.
        for match in _DIRECT_READ.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            line = text.splitlines()[line_no - 1].strip()
            if line.startswith("#"):
                continue
            offenders.append(f"{rel}:{line_no}: {line}")

    assert not offenders, (
        "These read a COPILOT_ env var directly, so a value set in .env is silently "
        "ignored. Declare the field on Settings in config.py and read it via "
        "get_settings():\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "field",
    ["adzuna_app_id", "adzuna_app_key", "startup_feeds", "upwork_feeds"],
)
def test_source_settings_are_declared_on_settings(field):
    """The fields the lead sources depend on exist and default to empty.

    An undeclared field would be dropped by ``extra="ignore"`` rather than raising, so
    a typo'd or missing declaration fails exactly as silently as the bug above.
    """
    assert field in Settings.model_fields
    assert getattr(Settings(_env_file=None), field) == ""


def test_a_value_in_a_dotenv_file_reaches_settings(tmp_path):
    """The property the whole file exists to protect, asserted end to end."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "COPILOT_ADZUNA_APP_ID=id-from-file\n"
        "COPILOT_UPWORK_FEEDS=https://example.com/a.rss,https://example.com/b.rss\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=str(env_file))
    assert settings.adzuna_app_id == "id-from-file"
    assert settings.upwork_feeds.count(",") == 1
