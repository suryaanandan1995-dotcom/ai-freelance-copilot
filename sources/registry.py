"""Source registry: wire up the default adapters and fan-out fetching."""
from __future__ import annotations

import logging

from config import get_settings
from core.schemas import Lead
from sources.base import LeadSource, dedupe
from sources.contra_startup import ContraStartupSource
from sources.contract_jobs import ContractJobsSource
from sources.hn_freelancer import HNFreelancerSource
from sources.hn_hiring import HNWhoIsHiringSource
from sources.jobicy import JobicySource
from sources.reddit_forhire import RedditForHireSource
from sources.remote_boards import RemoteBoardsSource
from sources.working_nomads import WorkingNomadsSource

logger = logging.getLogger(__name__)

# Sources NOT enabled by default, and why (measured 2026-08-03 over 24 production
# runs). Enabling either needs the blocker solved first, otherwise they burn a fetch
# + LLM scoring budget every run for zero leads:
#
#   upwork_rss      — PERMANENT for now: the public job RSS endpoint returns HTTP 410
#                     Gone; Upwork discontinued it. Needs an approved GraphQL API key,
#                     which is an application, not a config value.
#   reddit_forhire  — CONDITIONAL, no longer permanent. Unauthenticated
#                     www.reddit.com/*.json was 403 Blocked on 48/48 fetches (Reddit
#                     blocks datacenter IPs, which is where this runs). App-only OAuth
#                     lifts that and is free: create a "script" app at
#                     reddit.com/prefs/apps and set COPILOT_REDDIT_CLIENT_ID +
#                     COPILOT_REDDIT_CLIENT_SECRET. The source then enables itself
#                     below. Absent EITHER value it stays out, because without a token
#                     every run would spend two fetches to be refused.
#
# hn_freelancer stays enabled (its stale-story bug is fixed) but note the HN
# freelancer thread carries ~0-1 relevant posts/month, so expect little from it.


def get_default_sources() -> list[LeadSource]:
    """Instantiate every built-in lead source with default configuration.

    Ordered by measured yield-of-*contactable* leads, not raw volume: hn_hiring
    first because ~46% of its posts publish a direct hiring-manager email, which
    is the only thing that makes a lead actionable for cold outreach.

    ``reddit_forhire`` goes second whenever OAuth credentials exist. 6 runs
    (2026-08-10..17) measured the real blocker: 269 leads cleared the fit bar, 196
    carried an address, and only **7 were both** — 181 of the 196 addresses were
    hn_hiring full-time employment posts (median fit 28), while the 231 qualified
    job-board leads carry no address at all. r/forhire ``[Hiring]`` posts are the
    one high-volume place that overlap lives: a client, contract work, address in
    the body. It ranks behind hn_hiring only because hn_hiring's contactable rate
    is measured in production and this one's is not yet — first run that beats it
    should take the top slot.
    """
    sources: list[LeadSource] = [HNWhoIsHiringSource()]

    settings = get_settings()
    if (settings.reddit_client_id or "").strip() and (
        settings.reddit_client_secret or ""
    ).strip():
        sources.append(RedditForHireSource())
    else:
        logger.info(
            "reddit_forhire disabled: set COPILOT_REDDIT_CLIENT_ID and "
            "COPILOT_REDDIT_CLIENT_SECRET (free 'script' app at reddit.com/prefs/apps)"
        )

    sources += [
        ContractJobsSource(),
        RemoteBoardsSource(),
        JobicySource(),
        WorkingNomadsSource(),
        ContraStartupSource(),
        HNFreelancerSource(),
    ]
    return sources


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
