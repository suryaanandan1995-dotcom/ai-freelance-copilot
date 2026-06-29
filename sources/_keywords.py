"""Shared keyword matching used by several source adapters.

The copilot targets DevOps / cloud / SRE / platform / security freelance work.
Adapters use :func:`matches_keywords` to filter listings and
:func:`extract_tags` to derive coarse tags from free text.
"""
from __future__ import annotations

# Lowercased role/skill keywords the copilot cares about.
KEYWORDS: tuple[str, ...] = (
    "devops",
    "dev ops",
    "sre",
    "site reliability",
    "platform engineer",
    "platform engineering",
    "cloud",
    "aws",
    "azure",
    "gcp",
    "kubernetes",
    "k8s",
    "terraform",
    "ansible",
    "docker",
    "ci/cd",
    "cicd",
    "infrastructure",
    "iac",
    "security",
    "devsecops",
    "remote",
)


def matches_keywords(*texts: str | None) -> bool:
    """True if any target keyword appears in any of the given texts."""
    blob = " ".join(t for t in texts if t).lower()
    return any(kw in blob for kw in KEYWORDS)


def extract_tags(*texts: str | None) -> list[str]:
    """Return the subset of KEYWORDS found in the texts (deduped, ordered)."""
    blob = " ".join(t for t in texts if t).lower()
    out: list[str] = []
    for kw in KEYWORDS:
        if kw in blob and kw not in out:
            out.append(kw)
    return out
