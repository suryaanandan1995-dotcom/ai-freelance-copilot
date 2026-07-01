"""Shared keyword matching used by several source adapters.

The copilot targets DevSecOps / cloud / SRE / platform / security freelance
work. Adapters use :func:`matches_keywords` to filter listings down to those
with a *genuine* DevSecOps signal (not merely a generic word like "remote" or
"cloud" appearing anywhere) and :func:`extract_tags` to derive coarse tags.

Matching is word-boundary-ish so that short terms don't match inside unrelated
words (e.g. "sre" must not match inside "stores", "aws" not inside "flaws").
"""
from __future__ import annotations

import re

# Lowercased role/skill keywords that constitute a genuine DevSecOps-relevant
# signal. Multi-word phrases are matched as phrases; short tokens are matched on
# word boundaries. Order here is also the order tags are emitted in.
KEYWORDS: tuple[str, ...] = (
    "kubernetes",
    "k8s",
    "devsecops",
    "devops",
    "dev ops",
    "sre",
    "site reliability",
    "platform engineer",
    "platform engineering",
    "infrastructure engineer",
    "cloud engineer",
    "cloud security",
    "security engineer",
    "terraform",
    "ci/cd",
    "cicd",
    "aws",
    "gcp",
    "azure",
    "eks",
    "gke",
    "aks",
    "docker",
    "helm",
    "argocd",
    "argo cd",
    "istio",
    "ansible",
    "observability",
    "prometheus",
)


def _compile(kw: str) -> re.Pattern[str]:
    """Word-boundary-ish matcher for a keyword.

    Uses lookaround on alphanumerics so that keywords containing non-word
    characters (``ci/cd``) still match, while short tokens (``sre``, ``aws``)
    only match as standalone words.
    """
    return re.compile(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])")


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kw, _compile(kw)) for kw in KEYWORDS
)


def matches_keywords(*texts: str | None) -> bool:
    """True only if a genuinely DevSecOps-relevant term appears in the texts."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return False
    return any(pat.search(blob) for _, pat in _PATTERNS)


def extract_tags(*texts: str | None) -> list[str]:
    """Return the subset of KEYWORDS found in the texts (deduped, ordered)."""
    blob = " ".join(t for t in texts if t).lower()
    out: list[str] = []
    if not blob:
        return out
    for kw, pat in _PATTERNS:
        if pat.search(blob) and kw not in out:
            out.append(kw)
    return out
