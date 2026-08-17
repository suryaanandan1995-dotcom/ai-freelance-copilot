"""Turn a qualified-but-uncontactable lead into a paste-and-submit apply pack.

WHY THIS EXISTS (measured over 6 production runs, 2026-08-10..17):

* 1,047 new leads discovered.
* 269 of them cleared the fit bar of 70.
* Only **7** were both qualified AND contactable, so **6** cold emails went out in
  10 days.
* The other **262 qualified leads have no email address at all** — they are
  job-board listings that route through an apply form.

``auto_submit`` is permanently OFF (bot-submitting to other people's job posts is a
ToS ban) and the dashboard is not hosted, so the ONLY route those 262 leads have is
the "APPLY YOURSELF" section of the owner's email digest
(:mod:`interfaces.notify`), which today lists a score, a title, a company and a
link.

A link is not a hand-off. Applying still means re-opening the post, re-reading it
and writing the pitch from scratch — 262 times. The standing lesson of this project
is that "fully automated" means automated *up to* the hand-off and then an explicit,
**complete** hand-off: being right about where automation must stop is not the same
as delivering what the automation already produced up to that point. This module
closes that gap, so each application is a ~60-second paste-and-submit job instead of
a 15-minute rewrite.

DESIGN CONSTRAINTS, each paid for by a past defect:

1. COST. One model call per pack, ``max_apply_packs_per_run`` packs per run, taken
   highest ``fit_score`` first. At Opus 4.8 prices (see :mod:`costs`) a pack is
   ~900 input + ~350 output tokens ≈ $0.013, so the default 5 packs ≈ $0.07
   against a ``max_usd_per_run`` ceiling of $5.00 and a measured run spend of
   ~$2.30. The retriever is not a model call. Nothing here loops or retries: a
   failed draft becomes a template pack, not a second bill.

2. NEVER INVENT A PROOF LINK. A live pitch once cited
   ``devsecops-pipeline-templates``, a repo that does not exist (see
   :func:`outreach.pitch._project_links`). A prospect who clicks a 404 concludes
   the projects are invented — which is the exact objection proof links exist to
   pre-empt — and it is the *interested* reader, the only one who matters, who
   clicks. So a dead proof link is worse than no proof link, and every URL in a
   pack must come from the allow-list built in :func:`_allowed_links`: the
   retriever's returned documents, or ``get_settings()``. A URL the model emits
   from anywhere else is dropped — from ``proof_links`` AND from the prose of the
   cover note and bullets, because a hallucinated link is just as dead inside a
   sentence.

3. ESCAPE EVERYTHING THIRD-PARTY. Titles and company names come from remote job
   posts and the digest's HTML is assembled by f-string concatenation, so
   :meth:`ApplyPack.to_html` escapes every interpolated remote string. Unescaped
   remote text there is an injection into the owner's own inbox (same reasoning as
   the ``apply_rows`` block in :mod:`interfaces.notify`).

4. NEVER RAISE. These leads were already paid for — scored at real cost — and this
   module is the last thing standing between that spend and the owner's hands.
   Failing to build one pack must not lose the other four and must not fail a run.

5. NO PRICE IS EVER QUOTED. ``suggested_rate`` is computed from
   ``settings.standard_rate`` (blank = defer to the call) and never comes from the
   model, matching the project-wide "pricing goes to the call" rule enforced in
   :mod:`reply.respond`.

The caller owns the wiring, including the ``apply_packs`` feature gate — this
module builds packs whenever it is asked to.
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from agents.llm import get_chat
from config import get_settings
from costs import BudgetExhausted
from voice import HUMAN_VOICE

logger = logging.getLogger(__name__)

#: Hard ceiling on the pasteable note. Apply forms cap their "cover letter" boxes and
#: a human reads maybe a screen of it; the model is asked for 110-140 and the result is
#: clamped, so a runaway draft can never turn into a wall of text in the digest.
MAX_NOTE_WORDS = 140

#: A bullet list that isn't scannable in one glance defeats its purpose.
MIN_BULLETS = 2
MAX_BULLETS = 4
MAX_BULLET_WORDS = 26

#: Retrieved KB chunks per pack. 3 matches ``outreach.pitch.draft_email`` — enough to
#: name real artifacts, small enough to keep the input token count near 900.
_PROOF_CHUNKS = 3

# Trailing sentence punctuation is not part of a URL; markdown/HTML wrappers aren't
# either. Kept deliberately conservative so a link is under- rather than over-matched.
_URL_RE = re.compile(r"https?://[^\s<>\"'`\)\]]+")
_URL_TRIM = ".,;:!?)]}>\"'"

#: Used only when the retriever returns nothing, so there is no artifact to name.
#: Deliberately free of numbers: an unverifiable count is the same defect as an
#: unverifiable link.
_GENERIC_BULLETS = (
    "Freelance DevSecOps / Kubernetes / AI-infrastructure engineer — the stack in "
    "your listing is my day-to-day work.",
    "Every claim is checkable: the links below are live repositories with tests and "
    "CI, not a slide deck.",
)

_SYSTEM = (
    "You are helping a freelance DevSecOps / Kubernetes / AI-infrastructure engineer "
    "APPLY BY HAND to a job listing that has no contact address, only an apply form. "
    "You are not writing a cold email — the reader asked for applications, so skip the "
    "apology for reaching out and get straight to fit.\n"
    "Return ONLY a single JSON object, no prose around it, with exactly these keys:\n"
    '  "why_you_fit": 2-4 short bullets (max ~25 words each). Each bullet must be tied '
    "to a REAL named portfolio artifact from the retrieved proof points below. No "
    "bullet may contain a statistic that is not in those proof points.\n"
    f'  "cover_note": 110-{MAX_NOTE_WORDS} words, ready to paste straight into an '
    "apply form. First line specific to THEIR listing. Plain text, no markdown.\n"
    '  "proof_links": 1-3 URLs, copied EXACTLY from the allowed-links list below.\n'
    "HARD RULES:\n"
    "- Use URLs ONLY from the allowed-links list, character for character. Never "
    "invent, guess, shorten or complete a repository name or URL. A link that 404s is "
    "worse than no link: the only reader who clicks is the interested one.\n"
    "- Never state a rate, price, budget, hourly or day figure, or a delivery deadline. "
    "Pricing is settled on a call.\n"
    "- Never fabricate anything about them, their company, or their stack.\n\n" + HUMAN_VOICE
)


@dataclass(frozen=True)
class ApplyPack:
    """Everything needed to submit one application, in one block.

    ``proof_links`` is verified by the builder against the allow-list before the pack
    is constructed — nothing downstream re-checks it, so nothing may bypass
    :func:`build_apply_packs` to create one of these with model-supplied URLs.
    """

    lead_url: str
    title: str
    company: str
    fit_score: int
    why_you_fit: list[str]
    cover_note: str
    suggested_rate: str
    proof_links: list[str]

    def to_text(self) -> str:
        """Plaintext block for the email digest.

        Leads with the listing URL on its own first line. With no hosted dashboard
        that link IS the hand-off: every other line helps the owner apply faster, but
        without the URL there is nothing to apply to.
        """
        head = f"[{self.fit_score}] {self.title}"
        if self.company:
            head += f" — {self.company}"
        lines = [self.lead_url, head, "", "  WHY YOU FIT"]
        lines += [f"    - {b}" for b in self.why_you_fit]
        lines += ["", f"  RATE: {self.suggested_rate}"]
        if self.proof_links:
            lines.append("  PROOF:")
            lines += [f"    {link}" for link in self.proof_links]
        lines += ["", "  PASTE INTO THE APPLY FORM:", ""]
        # Blank lines stay blank (no trailing whitespace) so the block survives a
        # copy-paste into a form field cleanly.
        lines += [f"    {line}" if line.strip() else "" for line in self.cover_note.splitlines()]
        return "\n".join(lines)

    def to_html(self) -> str:
        """Escaped HTML block for the same digest.

        Every interpolated value is remote text (job titles, company names) or derived
        from it, and this string is concatenated into a larger f-string document, so
        each one is escaped here rather than at the call site.
        """
        url = html.escape(self.lead_url, quote=True)
        title = html.escape(self.title) or "(untitled listing)"
        company = html.escape(self.company)
        bullets = "".join(f"<li>{html.escape(b)}</li>" for b in self.why_you_fit)
        proof = "".join(
            f'<li><a href="{html.escape(link, quote=True)}">{html.escape(link)}</a></li>'
            for link in self.proof_links
        )
        proof_html = f"<p><strong>Proof:</strong></p><ul>{proof}</ul>" if proof else ""
        return (
            f"<div><h4>[{int(self.fit_score)}] "
            f'<a href="{url}">{title}</a>'
            f"{f' — {company}' if company else ''}</h4>"
            f"<p><strong>Why you fit:</strong></p><ul>{bullets}</ul>"
            f"<p><strong>Rate:</strong> {html.escape(self.suggested_rate)}</p>"
            f"{proof_html}"
            "<p><strong>Paste into the apply form:</strong></p>"
            '<pre style="background:#f6f8fa;padding:10px;border-radius:6px;'
            'white-space:pre-wrap">'
            f"{html.escape(self.cover_note)}</pre></div>"
        )


# --- URL allow-list ------------------------------------------------------------------


def _norm(url: str) -> str:
    return str(url or "").strip().rstrip(_URL_TRIM).rstrip("/").lower()


def _find_urls(text: str) -> list[str]:
    return [u.rstrip(_URL_TRIM) for u in _URL_RE.findall(str(text or ""))]


def _allowed_links(chunks: list[dict[str, Any]], settings: Any) -> list[str]:
    """Every URL a pack is permitted to contain, best-proof-first.

    Three legitimate origins, and no fourth:

    * ``owner_*`` settings — the owner's own site, GitHub, LinkedIn and Calendly.
    * ``owner_github/<source>`` for each retrieved chunk, where ``source`` is the real
      ingested directory name of a portfolio repo, so a URL built from it resolves.
      This is the same construction as :func:`outreach.pitch._project_links`, which
      exists precisely because a prompt that demanded a link without supplying one got
      an invented repo name back.
    * URLs written verbatim in retrieved chunk text — that text came out of the
      owner's own repositories, not off the internet.
    """
    base = (getattr(settings, "owner_github", "") or "").rstrip("/")
    links: list[str] = []
    for chunk in chunks or []:
        source = str(chunk.get("source") or "").strip().strip("/")
        if base and source and "/" not in source and " " not in source:
            links.append(f"{base}/{source}")
        links += _find_urls(chunk.get("text", ""))
    for field_name in ("owner_site", "owner_github", "owner_linkedin", "owner_calendly"):
        value = str(getattr(settings, field_name, "") or "").strip()
        if value:
            links.append(value)
    seen: set[str] = set()
    out: list[str] = []
    for link in links:
        key = _norm(link)
        if key and key not in seen:
            seen.add(key)
            out.append(link)
    return out


def _keep_allowed(urls: list[str], allowed: list[str]) -> list[str]:
    index = {_norm(a): a for a in allowed}
    out: list[str] = []
    for url in urls:
        canonical = index.get(_norm(url))
        if canonical is None:
            logger.warning("apply_pack: dropped non-allow-listed proof link %r", url)
            continue
        if canonical not in out:
            out.append(canonical)
    return out


def _scrub_prose(text: str, allowed: list[str]) -> str:
    """Remove any URL from free text that is not on the allow-list.

    Requirement 2 is about the *pack*, not about one field of it: a fabricated repo
    URL is exactly as dead in the middle of a cover note as it is in ``proof_links``,
    and the cover note is the part that actually gets pasted.
    """
    known = {_norm(a) for a in allowed}
    result = str(text or "")
    for url in _find_urls(result):
        if _norm(url) not in known:
            logger.warning("apply_pack: scrubbed non-allow-listed URL %r from prose", url)
            result = result.replace(url, "")
    # Tidy the punctuation and doubled spaces left where the URL used to be.
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"\(\s*\)|\[\s*\]|<\s*>", "", result)
    return re.sub(r"[ \t]+(?=[.,;:!?\n])", "", result).strip()


# --- text shaping --------------------------------------------------------------------


def _clamp_words(text: str, limit: int) -> str:
    """Trim to ``limit`` words, preserving the newlines the apply form will show."""
    parts = re.split(r"(\s+)", str(text or "").strip())
    words = 0
    kept: list[str] = []
    for part in parts:
        if part.strip():
            words += 1
            if words > limit:
                break
        kept.append(part)
    out = "".join(kept).rstrip()
    if words > limit:
        out = out.rstrip(_URL_TRIM + " -—,") + "…"
    return out


def _word_count(text: str) -> int:
    return len(str(text or "").split())


def _suggested_rate(settings: Any) -> str:
    """Never model-generated. ``standard_rate`` blank means: quote nothing at all."""
    rate = str(getattr(settings, "standard_rate", "") or "").strip()
    if rate:
        return f"{rate} — ballpark only, confirmed against scope on a 15-minute call"
    return "not quoted here — priced against scope on a 15-minute call"


def _shape_bullets(raw: Any, fallback: list[str]) -> list[str]:
    bullets: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = _clamp_words(str(item).strip().lstrip("-•* ").strip(), MAX_BULLET_WORDS)
        if text and text not in bullets:
            bullets.append(text)
    for extra in fallback:
        if len(bullets) >= MIN_BULLETS:
            break
        if extra not in bullets:
            bullets.append(extra)
    return bullets[:MAX_BULLETS]


# --- the deterministic (no model call) pack -------------------------------------------


def _first_sentence(text: str, words: int = MAX_BULLET_WORDS) -> str:
    head = re.split(r"(?<=[.!?])\s", str(text or "").strip(), maxsplit=1)[0]
    return _clamp_words(head, words)


def _template_bullets(chunks: list[dict[str, Any]]) -> list[str]:
    bullets: list[str] = []
    for chunk in chunks or []:
        source = str(chunk.get("source") or "").strip()
        sentence = _first_sentence(chunk.get("text", ""))
        if not source or not sentence:
            continue
        bullet = sentence if source.lower() in sentence.lower() else f"{source}: {sentence}"
        if bullet not in bullets:
            bullets.append(_clamp_words(bullet, MAX_BULLET_WORDS))
        if len(bullets) >= MAX_BULLETS:
            break
    return bullets


def _template_pack(
    lead: dict,
    chunks: list[dict[str, Any]],
    allowed: list[str],
    settings: Any,
) -> ApplyPack:
    """A complete pack with NO model call.

    Used when there is no model available, when the draft fails, and when the run's
    budget is gone. A lead the digest already promised to hand over must arrive with
    something pasteable: a template note the owner edits in 30 seconds beats a bare
    link, which is the status quo this module exists to replace.
    """
    title = str(lead.get("title") or "").strip()
    company = str(lead.get("company") or "").strip()
    bullets = _shape_bullets(_template_bullets(chunks), list(_GENERIC_BULLETS))
    proof = _keep_allowed(allowed[:2], allowed)
    greeting = f"Hi {company} team," if company else "Hi,"
    role = f'your "{title}" listing' if title else "your listing"
    note = (
        f"{greeting}\n\n"
        f"I'm {settings.owner_name}, a freelance DevSecOps / Kubernetes / "
        f"AI-infrastructure engineer, and I'd like to apply for {role}. "
        f"{' '.join(_as_sentence(b) for b in bullets[:2])} "
        "The links below go straight to the code, so the claims above are checkable "
        "before we ever speak.\n\n"
        "Happy to walk through scope on a short call — "
        f"{settings.owner_calendly}\n\n"
        f"{settings.owner_name}\n{settings.owner_site}"
    )
    return ApplyPack(
        lead_url=str(lead.get("url") or ""),
        title=title,
        company=company,
        fit_score=int(lead.get("fit_score") or 0),
        why_you_fit=bullets,
        cover_note=_clamp_words(_scrub_prose(note, allowed), MAX_NOTE_WORDS),
        suggested_rate=_suggested_rate(settings),
        proof_links=proof,
    )


def _as_sentence(text: str) -> str:
    text = str(text or "").strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


# --- the drafted (one model call) pack ------------------------------------------------


def _parse_json(raw: str) -> dict:
    """Pull the JSON object out of a model reply, tolerating prose around it."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model reply")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model reply was not a JSON object")
    return data


def _prompt(lead: dict, chunks: list[dict[str, Any]], allowed: list[str], settings) -> str:
    proof = (
        "\n".join(f"- {c.get('text', '')} [{c.get('source', '')}]" for c in chunks)
        or "(no proof points retrieved)"
    )
    return (
        f"Their listing: {lead.get('title') or '(untitled)'}\n"
        f"Company: {lead.get('company') or 'unknown'}\n"
        f"Job board: {lead.get('source') or 'unknown'}\n"
        f"Listing URL: {lead.get('url') or 'unknown'}\n"
        f"Our fit score for it: {lead.get('fit_score', 0)}/100\n\n"
        f"Retrieved proof points from the engineer's own portfolio:\n{proof}\n\n"
        "Allowed links — copy any URL you use EXACTLY from this list and use no other:\n"
        + "\n".join(f"- {link}" for link in allowed)
        + f"\n\nEngineer name: {settings.owner_name}\n"
        "Write the JSON object now."
    )


def _drafted_pack(
    lead: dict,
    chunks: list[dict[str, Any]],
    allowed: list[str],
    settings: Any,
    model: Any,
) -> ApplyPack:
    """Exactly one ``model.invoke`` call. Raises on anything unusable."""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _prompt(lead, chunks, allowed, settings)},
    ]
    result = model.invoke(messages)
    content = getattr(result, "content", result)
    data = _parse_json(content if isinstance(content, str) else str(content))

    bullets = [
        _scrub_prose(b, allowed) for b in _shape_bullets(data.get("why_you_fit"), [])
    ]
    bullets = _shape_bullets([b for b in bullets if b], _template_bullets(chunks) or list(_GENERIC_BULLETS))
    note = _clamp_words(_scrub_prose(data.get("cover_note", ""), allowed), MAX_NOTE_WORDS)
    if _word_count(note) < 20:
        # A stub note is not a hand-off; the template one is longer and always sane.
        raise ValueError("drafted cover note too short to paste")
    proof = _keep_allowed(
        [str(u) for u in (data.get("proof_links") or []) if str(u).strip()], allowed
    )
    if not proof:
        # The model cited nothing usable (or only invented links). Fall back to real
        # ones rather than shipping a pack whose claims cannot be checked.
        proof = _keep_allowed(allowed[:2], allowed)
    return ApplyPack(
        lead_url=str(lead.get("url") or ""),
        title=str(lead.get("title") or "").strip(),
        company=str(lead.get("company") or "").strip(),
        fit_score=int(lead.get("fit_score") or 0),
        why_you_fit=bullets,
        cover_note=note,
        suggested_rate=_suggested_rate(settings),
        proof_links=proof,
    )


# --- public entry point ---------------------------------------------------------------


def build_apply_packs(
    leads: list[dict],
    *,
    retriever: Any = None,
    chat: Any = None,
    limit: int | None = None,
) -> list[ApplyPack]:
    """Build up to ``limit`` paste-and-submit packs, highest ``fit_score`` first.

    ``leads`` are the dicts collected in ``pipeline.apply_yourself``
    (``title``/``url``/``company``/``source``/``fit_score``). ``limit`` defaults to
    ``get_settings().max_apply_packs_per_run``.

    Exactly one model call per pack, and never more: a lead whose draft fails gets a
    template pack instead of a retry, because a lead we already promised to hand over
    must come back with something pasteable, and a retried failure is one bug billed
    twice. Never raises — a broken pack is logged and the rest are returned.
    """
    settings = get_settings()
    if limit is None:
        limit = int(getattr(settings, "max_apply_packs_per_run", 5) or 0)
    if not leads or limit <= 0:
        return []

    # Callers sort best-fit-first already; re-sorting here means the cap always cuts
    # the *lowest* scores even if a caller hands over an unsorted list.
    chosen = sorted(leads, key=lambda r: int(r.get("fit_score") or 0), reverse=True)[:limit]

    if retriever is None:
        try:
            from rag.retriever import get_retriever  # lazy: keeps this module import-safe

            retriever = get_retriever()
        except Exception:  # pragma: no cover - a missing KB must not lose the packs
            logger.warning("apply_pack: retriever unavailable; packs will cite settings links")

    # One model handle for the whole batch; `chat=None` with no API key configured means
    # there is no model to call, so every pack takes the template path and bills nothing.
    model: Any = None
    if chat is not None or getattr(settings, "anthropic_api_key", ""):
        try:
            model = get_chat(settings.model_opus, chat=chat)
        except Exception as exc:
            logger.warning("apply_pack: no chat model (%s); using template packs", exc)
    else:
        logger.info("apply_pack: no chat model configured; using template packs")

    packs: list[ApplyPack] = []
    for lead in chosen:
        chunks: list[dict[str, Any]] = []
        try:
            if retriever is not None:
                query = " ".join(
                    str(lead.get(k) or "") for k in ("title", "company", "source")
                ).strip()
                chunks = retriever.retrieve(query, _PROOF_CHUNKS) or []
        except Exception as exc:
            logger.warning("apply_pack: retrieval failed for %r (%s)", lead.get("url"), exc)
        allowed = _allowed_links(chunks, settings)

        pack: ApplyPack | None = None
        if model is not None:
            try:
                pack = _drafted_pack(lead, chunks, allowed, settings, model)
            except BudgetExhausted:
                # Spend is gone. Stop calling the model for the remaining leads instead
                # of raising into the run: the packs already built are still delivered,
                # and the rest arrive as templates.
                logger.warning("apply_pack: budget exhausted; remaining packs are templates")
                model = None
            except Exception as exc:
                logger.warning(
                    "apply_pack: draft failed for %r (%s); using template pack",
                    lead.get("url"),
                    exc,
                )
        if pack is None:
            try:
                pack = _template_pack(lead, chunks, allowed, settings)
            except Exception as exc:  # pragma: no cover - defensive; must not lose siblings
                logger.error("apply_pack: could not build any pack for %r (%s)", lead, exc)
                continue
        packs.append(pack)
    return packs
