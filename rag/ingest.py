"""Ingest the user's portfolio repos + curated achievements into chunked docs.

Each repo's README.md is parsed for its title and the "Overview" and "Problem It
Solves" sections; the curated `rag/knowledge/achievements.md` is ingested
section-by-section. Output docs are `{text, source, kind}` chunked to ~200-400
words. The walker is robust to missing or malformed READMEs.
"""
from __future__ import annotations

import re
from pathlib import Path

CHUNK_MIN_WORDS = 200
CHUNK_MAX_WORDS = 400

# Section headings we care about from portfolio READMEs (matched case-insensitively).
_WANTED_SECTIONS = ("overview", "the problem it solves", "problem it solves")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BADGE_RE = re.compile(r"^\s*\[!\[.*$")  # shield/badge lines


def _strip_markdown_noise(line: str) -> str:
    return line.rstrip()


def _parse_readme(text: str) -> tuple[str, dict[str, str]]:
    """Return (title, {section_title_lower: body}) for a README's markdown."""
    title = ""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf, current
        if current is not None:
            sections[current] = "\n".join(buf).strip()
        buf = []

    for raw in text.splitlines():
        line = _strip_markdown_noise(raw)
        m = _HEADING_RE.match(line)
        if m:
            level, heading = len(m.group(1)), m.group(2).strip()
            if level == 1 and not title:
                title = heading
                continue
            flush()
            current = heading.lower()
            continue
        if _BADGE_RE.match(line):
            continue
        if current is not None:
            buf.append(line)
    flush()
    return title, sections


def _word_chunks(text: str, source: str, kind: str) -> list[dict]:
    """Split `text` into ~200-400 word chunks on paragraph boundaries."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[dict] = []
    cur: list[str] = []
    cur_words = 0

    def emit() -> None:
        nonlocal cur, cur_words
        if cur:
            chunks.append({"text": "\n\n".join(cur).strip(), "source": source, "kind": kind})
        cur = []
        cur_words = 0

    for para in paragraphs:
        words = len(para.split())
        if cur_words and cur_words + words > CHUNK_MAX_WORDS:
            emit()
        cur.append(para)
        cur_words += words
        if cur_words >= CHUNK_MIN_WORDS:
            emit()
    emit()
    return chunks


def _ingest_repo(readme_path: Path, repo_name: str) -> list[dict]:
    try:
        text = readme_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    title, sections = _parse_readme(text)
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    for key in _WANTED_SECTIONS:
        body = sections.get(key)
        if body:
            heading = "Overview" if key == "overview" else "The Problem It Solves"
            parts.append(f"## {heading}\n{body}")
    if len(parts) <= (1 if title else 0):
        # No wanted sections found; fall back to the title only (skip if nothing).
        if not title:
            return []
    combined = "\n\n".join(parts).strip()
    if not combined:
        return []
    return _word_chunks(combined, source=repo_name, kind="portfolio")


def _ingest_achievements() -> list[dict]:
    path = Path(__file__).resolve().parent / "knowledge" / "achievements.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _word_chunks(text, source="achievements", kind="achievement")


def ingest_portfolio(repos_path: str) -> list[dict]:
    """Walk the portfolio repos dir + curated achievements -> chunked docs.

    Returns a list of `{text, source, kind}`. Robust to a missing repos dir or
    missing/empty READMEs.
    """
    docs: list[dict] = []
    self_repo = Path(__file__).resolve().parents[1].name  # ai-freelance-copilot

    root = Path(repos_path)
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name == self_repo:
                continue  # don't ingest this tool's own README
            readme = entry / "README.md"
            if not readme.is_file():
                continue
            docs.extend(_ingest_repo(readme, entry.name))

    docs.extend(_ingest_achievements())
    return docs
