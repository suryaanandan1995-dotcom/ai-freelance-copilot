"""Source registry: wire up the default adapters and fan-out fetching."""
from __future__ import annotations

import logging

from core.schemas import Lead
from sources.base import LeadSource, dedupe
from sources.contra_startup import ContraStartupSource
from sources.hn_freelancer import HNFreelancerSource
from sources.hn_hiring import HNWhoIsHiringSource
from sources.jobicy import JobicySource
from sources.remote_boards import RemoteBoardsSource
from sources.uk_contract import UKContractSource
from sources.working_nomads import WorkingNomadsSource

logger = logging.getLogger(__name__)

# Sources deliberately NOT enabled by default, and why (measured 2026-08-03 over
# 24 production runs). Re-enabling any of these needs the blocker solved first,
# otherwise they burn a fetch + LLM scoring budget every run for zero leads:
#
#   upwork_rss      — the public job RSS endpoint returns HTTP 410 Gone; Upwork
#                     discontinued it. Needs an approved GraphQL API key.
#   reddit_forhire  — 403 Blocked on 48/48 fetches. Reddit blocks unauthenticated
#                     .json from datacenter IPs; needs OAuth credentials.
#
# hn_freelancer stays enabled (its stale-story bug is fixed) but note the HN
# freelancer thread carries ~0-1 relevant posts/month, so expect little from it.


def get_default_sources() -> list[LeadSource]:
    """Instantiate every built-in lead source with default configuration.

    Ordered by measured yield-of-*contactable* leads, not raw volume: hn_hiring
    first because ~46% of its posts publish a direct hiring-manager email, which
    is the only thing that makes a lead actionable for cold outreach.
    """
    return [
        HNWhoIsHiringSource(),
        UKContractSource(),
        RemoteBoardsSource(),
        JobicySource(),
        WorkingNomadsSource(),
        ContraStartupSource(),
        HNFreelancerSource(),
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
