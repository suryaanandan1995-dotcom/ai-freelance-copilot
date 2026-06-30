"""Opt-out suppression list.

A plain text file (``data/suppressed.txt``), one lowercased email per line, of
addresses that asked not to be contacted. Honoured before every send so a reply
of "unsubscribe" can be recorded by appending the address here. A missing file
means nobody is suppressed yet (empty set).
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPRESSION_PATH = Path("data/suppressed.txt")


def load_suppressed(path: Path | str | None = None) -> set[str]:
    """Return the set of lowercased suppressed emails (missing file -> empty)."""
    p = Path(path if path is not None else SUPPRESSION_PATH)
    if not p.exists():
        return set()
    out: set[str] = set()
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            addr = line.strip().lower()
            if addr and not addr.startswith("#"):
                out.add(addr)
    except OSError as exc:
        logger.warning("could not read suppression list %s: %s", p, exc)
        return set()
    return out


def is_suppressed(email: str, path: Path | str | None = None) -> bool:
    """True if ``email`` is on the suppression list."""
    if not email:
        return True
    return email.strip().lower() in load_suppressed(path)
