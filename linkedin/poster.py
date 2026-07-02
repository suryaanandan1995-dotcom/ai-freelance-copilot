"""LinkedIn posting orchestration.

Ties the RAG-grounded content engine to the LinkedIn API with the same safety
posture as the rest of the system:

* **Gated** — nothing publishes unless ``COPILOT_LINKEDIN_AUTO_POST`` is true AND a
  token is configured. ``publish=False`` (the default) only drafts + persists.
* **Rate-limited** — at most ``max_posts_per_day`` published posts per day.
* **Deduped** — the post body is hashed; the same content is never published twice.
* **Auditable** — every draft/publish is written to ``PostRecord`` and shows up in
  the dashboard, exactly like outreach emails.

This never touches other accounts and never scrapes — it publishes original content
to the owner's own feed via the official API, which is ToS-compliant.
"""
from __future__ import annotations

import datetime as _dt
import hashlib

from sqlalchemy import func, select

from config import get_settings
from db.models import PostRecord
from db.session import get_session, init_db


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _published_today(session) -> int:
    start = _dt.datetime.now(_dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.count(PostRecord.id)).where(
        PostRecord.channel == "linkedin",
        PostRecord.status == "published",
        PostRecord.published_at >= start,
    )
    with_count = session.execute(stmt).scalar_one()
    return int(with_count or 0)


def post_to_linkedin(
    kind: str = "post",
    topic: str | None = None,
    publish: bool = False,
    client=None,
    retriever=None,
    chat=None,
) -> dict:
    """Generate a content draft and (optionally) publish it to LinkedIn.

    Returns a stats dict: ``{status, kind, reason?, post_url?, post_urn?, body}``.
    ``status`` is one of ``draft`` (generated, not published), ``published``,
    ``skipped`` (gate off / cap hit / duplicate), or ``failed``.

    ``client``/``retriever``/``chat`` are injectable so tests run fully offline.
    """
    from content.engine import generate  # lazy import keeps this module import-safe

    settings = get_settings()
    init_db()

    draft = generate(kind=kind, topic=topic, retriever=retriever, chat=chat)
    body = (draft.get("body") or "").strip()
    body_hash = _hash(body)

    with get_session() as session:
        # dedupe: same body already recorded?
        existing = session.execute(
            select(PostRecord).where(PostRecord.body_hash == body_hash)
        ).scalar_one_or_none()
        if existing is not None:
            return {
                "status": "skipped",
                "reason": "duplicate-content",
                "kind": draft["kind"],
                "post_url": existing.post_url,
                "body": body,
            }

        record = PostRecord(
            channel="linkedin",
            kind=draft["kind"],
            topic=(topic or "").strip(),
            body=body,
            body_hash=body_hash,
            status="draft",
        )
        session.add(record)
        session.flush()  # assign id

        # --- gates: only publish when explicitly asked AND enabled AND under cap ---
        if not publish:
            return {"status": "draft", "kind": draft["kind"], "body": body}

        if not settings.linkedin_auto_post:
            return {
                "status": "skipped",
                "reason": "linkedin_auto_post disabled",
                "kind": draft["kind"],
                "body": body,
            }
        if not (settings.linkedin_access_token or "").strip():
            return {
                "status": "skipped",
                "reason": "no access token configured",
                "kind": draft["kind"],
                "body": body,
            }
        if _published_today(session) >= settings.max_posts_per_day:
            return {
                "status": "skipped",
                "reason": f"daily cap reached ({settings.max_posts_per_day})",
                "kind": draft["kind"],
                "body": body,
            }

        if client is None:
            from linkedin.client import LinkedInClient

            client = LinkedInClient(settings=settings)

        try:
            result = client.create_post(body)
        except Exception as exc:  # noqa: BLE001 — record failure, never crash the run
            record.status = "failed"
            record.error = str(exc)[:2000]
            return {
                "status": "failed",
                "reason": str(exc),
                "kind": draft["kind"],
                "body": body,
            }

        record.status = "published"
        record.post_urn = result.get("id") or None
        record.post_url = result.get("url") or None
        record.published_at = _dt.datetime.now(_dt.UTC)
        return {
            "status": "published",
            "kind": draft["kind"],
            "post_urn": record.post_urn,
            "post_url": record.post_url,
            "body": body,
        }
