"""Source registry: wire up the default adapters and fan-out fetching."""
from __future__ import annotations

import logging

from core.schemas import Lead
from sources.base import LeadSource, dedupe
from sources.contra_startup import ContraStartupSource
from sources.hn_hiring import HNWhoIsHiringSource
from sources.remote_boards import RemoteBoardsSource
from sources.upwork_rss import UpworkRSSSource

logger = logging.getLogger(__name__)


def get_default_sources() -> list[LeadSource]:
    """Instantiate every built-in lead source with default configuration."""
    return [
        UpworkRSSSource(),
        RemoteBoardsSource(),
        ContraStartupSource(),
        HNWhoIsHiringSource(),
    ]


def fetch_all(sources: list[LeadSource], per_source_limit: int = 25) -> list[Lead]:
    """Fetch from each source, concatenate, and dedupe.

    A failing source never aborts the run — its error is logged and skipped.
    """
    all_leads: list[Lead] = []
    for source in sources:
        try:
            all_leads.extend(source.fetch(limit=per_source_limit))
        except Exception as exc:  # adapters shouldn't raise, but be defensive
            logger.warning("source %s raised during fetch: %s", source.name, exc)
    return dedupe(all_leads)
