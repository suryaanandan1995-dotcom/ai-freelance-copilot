"""Pipeline orchestration: discover -> qualify -> research -> draft -> queue.

This is the integration core. It fans out to every lead source, runs each fresh
lead through the LangGraph agent pipeline under a hard per-run Claude-spend cap,
and persists *drafted* proposals for a human to review and submit. It NEVER
submits anything to any platform — that is a deliberate ToS-safety decision.

Everything here runs fully offline when a ``FakeChat`` + fake retriever +
in-memory sources are injected (see the test suite).
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from config import get_settings
from costs import BudgetExhausted, CostTracker
from db.models import LeadRecord, LeadStatus, ProposalRecord, ProposalStatus
from db.session import get_session, init_db
from observability import metrics

logger = logging.getLogger(__name__)


def _today_start() -> _dt.datetime:
    now = _dt.datetime.now(_dt.UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _proposals_today(session: Any) -> int:
    """Count proposals created since UTC midnight (anti-spam day cap)."""
    return (
        session.query(ProposalRecord)
        .filter(ProposalRecord.created_at >= _today_start())
        .count()
    )


def _emails_today(session: Any) -> int:
    """Count every outbound email sent since UTC midnight (shared daily cap).

    Delegates to ``outreach.quota`` so cold emails and follow-ups are counted against
    one budget. Counting only this channel's sends made the cap per-channel, which is
    not what protects a sending domain.
    """
    from outreach.quota import emails_sent_today

    return emails_sent_today(session)


def _maybe_email_lead(
    *,
    lead: Any,
    scored_state: dict,
    research_state: dict,
    fit_score: int,
    lead_db_id: int | None,
    settings: Any,
    retriever: Any,
    chat: Any,
    email_override: str | None = None,
) -> str:
    """Attempt one cold email for a freshly queued lead.

    Returns ``"sent"`` on a successful send, otherwise a short skip-reason string
    (e.g. ``"no_email"``, ``"low_fit"``, ``"duplicate"``, ``"suppressed"``,
    ``"daily_cap"``, ``"send_failed"``). All guards (gate, fit, dedupe,
    suppression, daily cap) are enforced here. Never raises for control flow.

    ``email_override`` is an address that did NOT come from the post body — today only
    from ``outreach/discover.py``, which read it off the company's own site. It bypasses
    extraction because there is nothing in the lead to extract; it bypasses NOTHING else.
    Every remaining guard (fit, suppression, duplicate, daily cap) still applies, and
    deliberately so: a discovered address is the one kind this system invents rather than
    reads, so it is the last one that should get a shortcut around the suppression list.
    """
    from db.models import OutreachRecord
    from outreach.extract import find_deliverable_email
    from outreach.pitch import draft_email
    from outreach.sender import send_outreach
    from outreach.suppression import is_suppressed

    email = (email_override or "").strip().lower() or find_deliverable_email(lead)
    if not email:
        return "no_email"
    if fit_score < settings.outreach_min_fit:
        return "low_fit"
    if is_suppressed(email):
        return "suppressed"

    with get_session() as session:
        already = (
            session.query(OutreachRecord.id)
            .filter(OutreachRecord.email == email)
            .first()
        )
        if already is not None:
            return "duplicate"
        # The ramped ceiling, not the raw setting: ``max_emails_per_day`` is where volume
        # may end up, and ``effective_cap`` is what today has earned. Applied on BOTH send
        # paths — the follow-up runner already routes through it — because a ramp one
        # channel can walk around is the per-channel cap bug again, and that one let a
        # configured 20 send 40.
        from outreach.quota import effective_cap

        if _emails_today(session) >= effective_cap(session, settings.max_emails_per_day):
            return "daily_cap"

    from core.schemas import CompanyResearch, ScoredLead

    scored = ScoredLead(**scored_state) if scored_state else None
    if scored is None:
        return "no_score"
    research = CompanyResearch(**research_state) if research_state else CompanyResearch()

    draft = draft_email(scored, research, retriever=retriever, chat=chat)
    sent = send_outreach(email, draft["subject"], draft["body"])

    with get_session() as session:
        session.add(
            OutreachRecord(
                lead_id=lead_db_id,
                email=email,
                subject=draft["subject"],
                status="sent" if sent else "failed",
            )
        )
    return "sent" if sent else "send_failed"


def _domain_is_evidence(lead: Any, domain: str) -> bool:
    """True when ``domain`` came from the post's own host rather than from a guess.

    ``outreach.discover`` resolves a company domain two ways and the returned
    ``DiscoveredContact`` does not distinguish them, so the caller re-derives it: a
    discovered domain that matches the registrable domain of ``lead.url`` means the
    listing was published on that site, which is evidence. Anything else was derived from
    the company NAME, and a name is not an identifier — see
    ``settings.discover_send_to_guessed_domains``.

    The classification lives here, next to the send decision, rather than inside
    discovery: discovery's job is to find candidates, and only the caller knows whether it
    is about to mail one. Errors resolve to False, because "we could not tell where this
    address came from" must not read as "the post vouched for it".
    """
    if not domain:
        return False
    try:
        from outreach.discover import _host_of, _registrable

        host = _host_of(getattr(lead, "url", "") or "")
        if not host:
            return False
        return _registrable(host) == _registrable(domain)
    except Exception as exc:  # pragma: no cover - import/parse guard
        logger.warning("could not classify discovered domain %s: %s", domain, exc)
        return False


def _contact_overlap_note(rows: dict[str, dict] | None, threshold: int) -> str:
    """Name the sources on each side of the qualified/contactable split, or ''.

    ``blocked_no_contact`` established THAT the leads clearing the bar are unreachable.
    It cannot say why, and the why is not a property of the run — it is a property of
    which sources are in the mix. Deriving it took a manual pull of six runs' logs; this
    makes it a line in every digest instead.

    Measured over 2026-08-10..17 (6 runs, 1047 new leads), qualified vs contactable per
    source: contract_jobs 79/0, jobicy 76/14, remote_boards 76/1, working_nomads 17/0,
    contra_startup 14/0, hn_hiring 7/181. Two populations, barely intersecting: 181 of
    the 196 addresses came from ``hn_hiring``, whose posts are full-time employment ads
    with a median fit of 28, while 231 of the 269 qualified leads came from job boards
    that publish no address because they monetise the click. The per-run overlap was
    1, 4, 0, 0, 0, 2 — so the entire send volume rode on roughly one lead a day, and on
    the three zero days it was a coin flip over a set of size one.

    That distinction matters because the two halves have different levers, and the
    aggregate hides both: widening queries cannot make a board publish an address, and
    lowering the threshold only admits more addressless board leads. The fix is either a
    source that carries both (``reddit_forhire``) or discovery of the address ourselves
    (``outreach/discover.py``) — neither of which is suggested by "leads are clearing
    the bar" or by a bare unreachable count.
    """
    if not rows:
        return ""
    # A source "qualifies but cannot be reached" only counts at 3+ passing leads: at one
    # or two, no contactable is ordinary luck rather than a property of the feed.
    #
    # The test is a RATIO, not ``contactable == 0``. Exact zero missed the clearest case
    # in the measured window: ``remote_boards`` put 76 leads over the bar and supplied
    # exactly ONE address, which is the same structural finding as Adzuna's 79-and-none
    # but fell outside a strict-zero test on the strength of a single lucky post. So the
    # note reports the real address count instead of implying none — a reader who is
    # being told which half of the funnel is unreachable is owed the actual number.
    mute: list[tuple[int, str]] = []
    mute_contacts = 0
    for name, row in rows.items():
        passed = int(row.get("passed") or 0)
        contactable = int(row.get("contactable") or 0)
        if passed >= 3 and contactable <= max(1, passed // 20):
            mute.append((passed, name))
            mute_contacts += contactable
    # The mirror image: a source supplying real address volume whose leads almost never
    # clear the bar. Its p50 is the evidence that this is the wrong KIND of work rather
    # than a scoring bug, so it is quoted.
    talkers: list[tuple[int, str, object]] = []
    for name, row in rows.items():
        contactable = int(row.get("contactable") or 0)
        passed = int(row.get("passed") or 0)
        if contactable >= 5 and passed <= max(1, contactable // 10):
            talkers.append((contactable, name, row.get("p50")))
    if not (mute and talkers):
        return ""
    mute.sort(reverse=True)
    talkers.sort(reverse=True)
    mute_total = sum(p for p, _ in mute)
    mute_names = ", ".join(n for _, n in mute[:3])
    c, t_name, t_p50 = talkers[0]
    p50_txt = f", median fit {t_p50}" if t_p50 is not None else ""
    return (
        f" The two halves are different sources: {mute_total} qualified lead(s) came from "
        f"{mute_names}, which supplied {mute_contacts} address(es) between them, while "
        f"{t_name} supplied {c} address(es) that almost never clear "
        f"{threshold}{p50_txt}. Neither a wider query nor a lower bar can close that gap "
        "— only a source carrying both, or discovering the address."
    )


def _fit_summary(
    scores: list[int],
    threshold: int,
    unscored: int = 0,
    budget_exhausted: bool = False,
    blocked_no_contact: int = 0,
    source_rows: dict[str, dict] | None = None,
) -> dict:
    """Summarise the run's fit-score distribution and name the likely bottleneck.

    ``dropped: 34`` on its own is unactionable. The two causes need opposite fixes:

    * scores clustered just *below* the threshold  -> the threshold is too strict;
    * scores clustered far below it                -> the sources are off-ICP.

    ``near_miss`` (within 10 points of the threshold) is the number that decides
    which one it is, so it is computed here rather than left to be eyeballed.

    ``unscored`` is how many new leads never reached the model. It exists because the
    verdict "the lead mix is off-ICP; fix targeting" was being printed over a sample
    that excluded the *best-targeted source in the mix*: leads with no contact address
    were dropped before qualification, and the day-rate contract feed — the one built
    for this ICP, the one carrying an actual day rate — contributed zero scores to
    three consecutive runs. A confident verdict computed over a censored sample sends
    you to fix targeting that is already correct, which is worse than no verdict.

    ``blocked_no_contact`` is how many of the leads that CLEARED the bar carry no email
    address. It exists for the same reason ``budget_exhausted`` does, and it is the same
    bug a second time: ``passed`` was treated as success, so any single passing lead
    printed ``bottleneck: none``. Run 31364376553 scored a max of 97, put 47 leads over
    the bar, emailed **one**, and reported "none — leads are clearing the bar". True about
    the scores and false about the run: 46 of those 47 had no address, which is the whole
    reason only one was actionable. Clearing the bar is not the same as being reachable,
    and "none" tells the reader there is nothing to unblock.

    ``budget_exhausted`` is the run-level fact that the spend cap stopped the loop. It
    is threaded in because this function is the ONLY place that names a bottleneck, and
    without it a run that died on money reported ``bottleneck: none — leads are clearing
    the bar``: literally true about the scores (41 of 123 cleared 70) and false about the
    run, which stopped at $2.0042 against its cap with a lead it never reached. "none"
    tells the reader there is nothing to unblock, on the one run where the binding
    constraint was known and fixable in one setting.
    """
    if not scores:
        # Nothing was scored AND the cap was hit: say which, because "no leads scored"
        # reads as a sourcing/gating problem when the cause was spend.
        if budget_exhausted:
            return {
                "n": 0,
                "unscored": unscored,
                "bottleneck": (
                    "budget: the run stopped on its spend cap before any lead was "
                    "scored — raise max_usd_per_run or cut per-lead spend"
                ),
            }
        return {"n": 0, "unscored": unscored, "bottleneck": "no leads scored"}

    ordered = sorted(scores)
    n = len(ordered)

    def pct(p: float) -> int:
        return ordered[min(n - 1, max(0, int(round(p * (n - 1)))))]

    passed = sum(1 for s in ordered if s >= threshold)
    near_miss = sum(1 for s in ordered if threshold - 10 <= s < threshold)

    if budget_exhausted:
        # PRECEDENCE, deliberately: budget outranks every distribution verdict below —
        # including the censored-sample branch — because all four of those describe the
        # SHAPE of the scores, and on a truncated run that shape is a prefix of what the
        # money bought, not a sample of the lead mix. None of them can name the thing
        # that actually stopped the run. It is placed ahead of ``passed`` because that is
        # the exact line this fixes: run 31172835060 scored 41/123 over the bar and
        # reported "none — leads are clearing the bar" after stopping at $2.0042 on its
        # cap with one new lead it never reached. "none" tells the reader there is
        # nothing to unblock, on the one run whose blocker was known and was one setting.
        #
        # It does NOT trade specificity away: this message carries the same
        # scored-of-new counts the sample branch reports, says the same thing about the
        # distribution not being evidence, and additionally names the lever. Anything
        # less specific would be a regression on the censored-sample fix above.
        bottleneck = (
            f"budget: the run stopped on its spend cap after scoring {n} of "
            f"{n + unscored} new leads, so this distribution is a prefix, not a sample "
            f"({passed}/{n} cleared {threshold} before the stop). Raise max_usd_per_run "
            "or cut per-lead spend before reading targeting from it."
        )
    elif passed and blocked_no_contact >= max(1, passed // 2):
        # Ranked ABOVE the "none" branch for the same reason budget is ranked above the
        # distribution verdicts: it names the constraint that actually bound the run.
        # Gated at half of ``passed`` so a run with a couple of unreachable leads still
        # reports "none" — the point is to catch the case where being unreachable is the
        # rule, not to replace a clean verdict with a caveat.
        reachable = passed - blocked_no_contact
        bottleneck = (
            f"contacts: {blocked_no_contact} of {passed} leads that cleared {threshold} "
            f"carry no email address, so automation could act on {reachable}. They are "
            "listed under APPLY YOURSELF — this is a channel limit, not a targeting "
            "problem, so changing queries or the threshold will not raise the send count."
            + _contact_overlap_note(source_rows, threshold)
        )
    elif passed:
        bottleneck = "none — leads are clearing the bar"
    elif near_miss >= max(2, n // 5):
        bottleneck = (
            f"threshold: {near_miss}/{n} scored within 10 points of {threshold}. "
            "Lowering min_fit_score is likely to help more than changing sources."
        )
    elif unscored >= n:
        # More leads were withheld from the model than were shown to it. Whatever this
        # distribution says about targeting, it is not evidence about the lead mix.
        bottleneck = (
            f"sample: only {n} of {n + unscored} new leads were scored, so this "
            "distribution is not evidence about targeting. Check which sources show "
            "'email-blocked' or 'unscored' before changing queries or the threshold."
        )
    else:
        bottleneck = (
            f"sources: 0/{n} cleared {threshold} and only {near_miss} came close. "
            "The lead mix is off-ICP; fix targeting, not the threshold."
        )

    return {
        "n": n,
        "unscored": unscored,
        "threshold": threshold,
        "min": ordered[0],
        "p50": pct(0.5),
        "p90": pct(0.9),
        "max": ordered[-1],
        "passed": passed,
        "near_miss": near_miss,
        "bottleneck": bottleneck,
    }


def _interleave_by_source(leads: list) -> list:
    """Round-robin leads across their sources, preserving each source's own order.

    ``fetch_all`` concatenates sources in registry order, so truncating to the run cap
    took a prefix — i.e. everything from the first source and nothing from the rest. With
    a per-source limit equal to the run cap (both default 50), the first source alone
    filled it: six of seven sources were reported ``dead: fetched nothing`` on a live run
    when the truth was that the cap consumed the budget before they were reached.

    That is worse than the missing report it replaced: a wrong verdict gets acted on.
    Interleaving makes the cap sample every source instead of exhausting the first.
    """
    by_source: dict[str, list] = {}
    for lead in leads:
        by_source.setdefault(lead.source, []).append(lead)
    out: list = []
    queues = list(by_source.values())
    index = 0
    while len(out) < len(leads):
        for queue in queues:
            if index < len(queue):
                out.append(queue[index])
        index += 1
    return out


def _per_source_summary(rows: dict[str, dict], threshold: int) -> dict:
    """Attribute the funnel to each source, so targeting can be fixed with evidence.

    ``_fit_summary`` can say "the lead mix is off-ICP" but not WHICH sources produced
    the off-ICP mix. With 7 sources enabled that leaves the actual fix — drop the dead
    ones, expand the productive one — as guesswork, and a run that fetches 46 leads and
    qualifies 0 looks equally like "all sources are mediocre" and "six are useless and
    one is good". Those need opposite responses.

    A source that yields leads but never a *contactable* one is a distinct failure from
    one that yields nothing: the first is costing LLM scoring spend every run for leads
    that can never be emailed, and it is invisible in the totals.
    """
    out: dict[str, dict] = {}
    for name, row in sorted(rows.items()):
        scores = sorted(row.get("scores") or [])
        fetched = row.get("fetched", 0)
        # ``considered`` defaults to ``fetched`` so callers that don't track it (and the
        # summary's own unit tests) keep behaving as before rather than reading 0 and
        # reporting every source starved.
        considered = row.get("considered", fetched)
        entry = {
            # Absent (rather than 0) when the adapter doesn't report it, so a source
            # that never counted cannot be mistaken for one that read an empty feed.
            **({"scanned": row["scanned"]} if row.get("scanned") is not None else {}),
            "fetched": fetched,
            "considered": considered,
            "new": row.get("new", 0),
            "contactable": row.get("contactable", 0),
            # Kept beside ``contactable`` and never folded into it: "this feed publishes
            # addresses" and "we found one anyway" are different facts about the source,
            # and only the first is an argument for fetching more from it.
            "discovered": row.get("discovered", 0),
            "queued": row.get("queued", 0),
            "scored": len(scores),
        }
        if scores:
            entry["p50"] = scores[len(scores) // 2]
            entry["max"] = scores[-1]
            entry["passed"] = sum(1 for s in scores if s >= threshold)
        # Name the per-source verdict rather than leaving it to be inferred from
        # five numbers. Ordered most- to least-severe.
        error = row.get("error")
        if error:
            # An adapter that reported *why* it failed outranks every count below:
            # "broken" and "dead" need opposite responses (fix the caller vs. retire
            # the source), and the counts alone cannot tell them apart.
            entry["error"] = error
            entry["verdict"] = f"broken: {error}"
        elif not fetched:
            # "Returned nothing" has two opposite causes and one used to answer for both.
            # ``dead`` ranks near the top of the digest under "retire what never
            # produces"; if the adapter in fact read a full feed and its own filters
            # rejected every row, retiring the source is the wrong move — the filters or
            # the keyword list are the lever. Only a source that reached its upstream and
            # genuinely found nothing there is dead.
            scanned = entry.get("scanned")
            if scanned:
                entry["verdict"] = (
                    f"filtered-out: read {scanned} listing(s), none matched the "
                    f"skill keywords — widen the keywords, not the sources"
                )
            else:
                entry["verdict"] = "dead: fetched nothing"
        elif not considered:
            # The source worked; the run cap spent its whole budget elsewhere. Blaming
            # the source here would send you to fix a source that has nothing wrong.
            entry["verdict"] = (
                f"starved: fetched {fetched}, none considered — raise max_leads_per_run"
            )
        elif not entry["new"]:
            entry["verdict"] = "stale: every lead already seen"
        elif not scores:
            entry["verdict"] = "unscored: pre-gated before reaching the model"
        elif not entry["contactable"]:
            # Checked AFTER the score, and worded to name the channel rather than the
            # source. It used to be checked first, which made it unfalsifiable: leads
            # with no contact were never scored, so ``scores`` was always empty, so this
            # branch always won and said "scoring them is wasted spend" about a source
            # the code had refused to score. Adzuna sells the click, so its listings
            # carry no address by design — that makes the feed email-blocked, not bad.
            # It is where the $208k-$249k Forward Deployed Engineer role came from.
            best = f"best score {entry['max']}" if entry.get("max") is not None else "scored"
            entry["verdict"] = (
                f"email-blocked: {entry['new']} new leads, none with a contact "
                f"({best}) — human-submit channel only"
            )
        elif not entry.get("passed"):
            entry["verdict"] = f"off-ICP: best score {entry['max']} < {threshold}"
        elif not entry["queued"]:
            # Clearing the fit bar is NOT output. A lead can score 82 and still never be
            # queued: no deliverable contact, the per-day proposal cap, or the run hitting
            # its spend cap mid-loop. ``passed`` was being printed as the "productive"
            # number, so run 31172835060 read ``productive: 8 cleared 70`` for
            # remote_boards — 22 new leads, 1 contactable, **0 queued** — in text
            # identical to hn_hiring, which queued all 8 of its 8. Two sources, one
            # verdict, opposite correct responses (widen the winner vs. find out why the
            # other delivers nothing). This is the mirror of the email-blocked fix above:
            # that one under-credited a good source, this one over-credited a barren one.
            # Both are the same defect — a verdict asserting an outcome the counts don't
            # show — and over-crediting is the worse direction, because a source reported
            # productive is never investigated.
            #
            # The cause is a run-level fact this function cannot see, so name the measured
            # evidence and point at the candidates rather than picking one.
            entry["verdict"] = (
                f"no-output: 0 queued, though {entry['passed']} of {entry['scored']} "
                f"scores cleared {threshold} ({entry['contactable']}/{entry['new']} "
                "contactable) — clearing the bar is not output; check contacts, "
                "max_proposals_per_day and the run budget"
            )
        else:
            # Every number is labelled with what it measures, and the OUTPUT count leads.
            # "productive: 8 cleared 70" put a scores-above-bar count where a reader takes
            # the headline number to be leads delivered; here 8-queued and 8-cleared can
            # no longer be read as each other even when they happen to be equal.
            entry["verdict"] = (
                f"productive: {entry['queued']} queued, "
                f"{entry['passed']} of {entry['scored']} scores cleared {threshold}"
            )
        out[name] = entry
    return out


def run_pipeline(
    limit: int | None = None,
    sources: list | None = None,
    retriever: Any = None,
    chat: Any = None,
    notify: bool = False,
    auto_email: bool = False,
) -> dict:
    """Run one end-to-end pipeline pass and return run statistics.

    Returns ``{fetched, new, queued, dropped, skipped, cost_usd, budget_exhausted,
    emailed, emailed_skipped}``.

    When ``auto_email`` is True, each freshly queued lead that exposes a public
    contact email AND clears ``outreach_min_fit`` is sent a single short cold
    intro email — deduped against ``OutreachRecord`` (never emailed twice),
    suppression-list aware, and capped at ``max_emails_per_day``. This is the
    ONLY auto-send path; Upwork/LinkedIn submission stays human-only. The actual
    send is itself gated by ``settings.auto_email`` + SMTP config in the sender,
    so passing ``auto_email=True`` here is safe by default.
    """
    settings = get_settings()
    init_db()

    tracker = CostTracker(budget_usd=settings.max_usd_per_run)
    # Install the tracker so the metered LLM wrapper meters + budget-gates every call.
    from agents.llm import set_cost_tracker

    set_cost_tracker(tracker)

    fetched = new = queued = dropped = skipped = 0
    emailed = 0
    emailed_skipped: dict[str, int] = {}
    budget_exhausted = False
    queued_leads: list[dict] = []
    # Contactability is measured for EVERY new lead, before any LLM spend, because
    # it is the funnel's real bottleneck: 18 of 25 drafted proposals were discarded
    # at the contact step after being fully researched and written at Opus prices.
    contactable = 0
    uncontactable_skipped = 0
    # Qualified leads that have no email, kept so the digest can hand them to the owner
    # to apply by hand. Measured 2026-08-07: contract_jobs (the day-rate feed, the one
    # aimed squarely at paid contract work) fetched 36 leads scoring up to 78 and
    # produced ZERO outreach, because those listings carry an apply form and no address.
    # They were scored at real cost and then discarded without ever being shown to
    # anyone — the automation's dead end was silently also the owner's dead end, when
    # applying by hand takes a minute. auto_submit stays off permanently (bot-submitting
    # to job forms breaks platform ToS), so a human hand-off is the ONLY route these
    # leads have, which is exactly why they must be surfaced rather than dropped.
    apply_yourself: list[dict] = []
    # Addresses found on the company's own site for leads whose POST carried none, and
    # how many leads we spent lookups on. Counted separately from ``contactable`` on
    # purpose: ``contactable`` means "the listing published an address", and that is the
    # measurement the whole qualified-vs-reachable diagnosis rests on — 196 contactable
    # against 269 qualified with an overlap of 7. Folding discovered addresses into it
    # would make the split it exposes disappear from the report that exposed it, while
    # the underlying feeds stayed exactly as unreachable as before.
    discovered = 0
    discovery_attempts = 0
    # Addresses found on a domain GUESSED from the company name. Shown to the owner, not
    # mailed (see settings.discover_send_to_guessed_domains). A one-line hand-off — the
    # address and the page it came from — is enough for a human to accept or bin in two
    # seconds, and it is the only way the guessed path ever earns the right to send.
    proposed_contacts: list[dict] = []
    # Every fit score seen this run. Without this, "dropped: 34" is unactionable:
    # a run cannot tell you whether 34 leads scored 68 (threshold too strict) or 12
    # (sources off-ICP), which are opposite fixes. Recorded so min_fit_score can be
    # tuned from evidence instead of guessed.
    fit_scores: list[int] = []
    # Per-source funnel attribution. Keyed by lead.source; see _per_source_summary.
    by_source: dict[str, dict] = {}

    def _src(name: str) -> dict:
        return by_source.setdefault(
            name,
            {
                "fetched": 0,
                "considered": 0,
                "new": 0,
                "contactable": 0,
                "discovered": 0,
                "queued": 0,
                "scores": [],
            },
        )

    def _skip_email(reason: str) -> None:
        emailed_skipped[reason] = emailed_skipped.get(reason, 0) + 1

    try:
        from agents.graph import run_lead
        from sources.registry import fetch_all, get_default_sources

        srcs = sources if sources is not None else get_default_sources()
        per_source = max(1, (limit or settings.max_leads_per_run))

        # Seed a row for every ENABLED source, including ones that yield nothing.
        # Attribution built only from returned leads would omit a dead source entirely,
        # and an absent row reads as "not a problem" — the exact failure this reporting
        # exists to expose. A source is enabled, so it must appear in the report.
        for source in srcs:
            _src(getattr(source, "name", source.__class__.__name__))

        leads = fetch_all(srcs, per_source_limit=per_source)

        # Sources that record why they came back empty get to say so. Adapters swallow
        # transport errors and return [] by contract (they must never abort a run), which
        # made "the API rejected our request" look exactly like "nothing matched" —
        # uk_contract sent an invalid filter name for its entire life and every run
        # reported it as ``dead: fetched nothing``, pointing at queries instead of code.
        for source in srcs:
            err = getattr(source, "last_error", None)
            if err:
                _src(getattr(source, "name", source.__class__.__name__))["error"] = err

        # How many candidates each adapter actually READ, before its own keyword/side
        # filters. Without it "the feed gave us nothing" and "the feed gave us plenty and
        # our filters rejected all of it" were the same verdict — and the digest sorts
        # that verdict under "retire what never produces", so it advised deleting working
        # sources. hn_freelancer took an afternoon to clear by hand for exactly this.
        for source in srcs:
            scanned = getattr(source, "scanned", None)
            if scanned is not None:
                _src(getattr(source, "name", source.__class__.__name__))["scanned"] = scanned

        # ``fetched`` is counted BEFORE the run cap, so it means "what the source
        # returned" and nothing else. Counting it after made the number a function of
        # registry order: a source could be labelled ``dead: fetched nothing`` because
        # the cap was already full when its turn came. A verdict that blames a source
        # for the caller's cap is worse than no verdict, because it gets acted on.
        for lead in leads:
            _src(lead.source)["fetched"] += 1

        cap = limit if limit is not None else settings.max_leads_per_run
        if cap is not None and len(leads) > cap:
            # Interleave BEFORE truncating. fetch_all concatenates in registry order, so
            # a prefix is "all of source #1, none of the rest" — which reported six of
            # seven sources dead on a live run. See _interleave_by_source.
            leads = _interleave_by_source(leads)[:cap]

        fetched = len(leads)
        metrics.inc("leads_fetched_total", fetched)

        # What actually survived the cap, per source. A source that fetched leads but
        # had none considered is starved by the cap, not dead — the opposite fix.
        for lead in leads:
            _src(lead.source)["considered"] += 1

        for lead in leads:
            # Dedupe against the DB by (source, external_id).
            with get_session() as session:
                exists = (
                    session.query(LeadRecord.id)
                    .filter(
                        LeadRecord.source == lead.source,
                        LeadRecord.external_id == lead.external_id,
                    )
                    .first()
                )
            if exists is not None:
                skipped += 1
                continue

            new += 1
            _src(lead.source)["new"] += 1

            # --- cheap gates BEFORE any LLM spend ---------------------------
            # Extracting a contact costs one regex pass + one cached DNS lookup;
            # researching and drafting costs Opus tokens. Doing them in that order
            # is the difference between paying to discover a lead is unsendable and
            # discovering it for free. Only applies when auto-emailing is the goal —
            # with auto_email off, drafts for human submission are still valuable.
            has_contact = False
            # Why the address is missing, not just that it is. ``no_email_pregate`` read
            # 851 of 1047 leads across 2026-08-10..17 — a number no decision can be made
            # from, because "the post published no address" (fix: discover it, or change
            # sources) and "we ranked an address whose domain refuses mail" (fix: the
            # ranking, or the MX gate) are opposite problems sharing one counter.
            contact_reason = "unknown"
            try:
                from outreach.extract import find_deliverable_email_with_reason

                found_email, contact_reason = find_deliverable_email_with_reason(lead)
                has_contact = found_email is not None
            except Exception as exc:  # extraction must never break the loop
                logger.warning("contact pre-check failed for %s: %s", lead.url, exc)
            draft_allowed = True
            if has_contact:
                contactable += 1
                _src(lead.source)["contactable"] += 1
            elif auto_email and settings.require_contact_before_draft:
                # NOT ``continue``. Skipping the whole lead here skipped *qualification*
                # too, which is the cheap Sonnet call — to save the expensive Opus ones
                # that ``route_after_qualify`` already gates on fit score. Saving the
                # cheap stage to protect the expensive stage is a category error, and it
                # cost far more than it saved: a source whose leads are all uncontactable
                # produced no scores at all, so ``_per_source_summary`` reported
                # "unreachable: scoring them is wasted spend" — a verdict that was true
                # only because the code had made it true. The run that surfaced a
                # $208k-$249k Forward Deployed Engineer role recorded it as evidence to
                # retire the feed that found it.
                #
                # Score it, draft nothing, and let the funnel report say which it was.
                uncontactable_skipped += 1
                # Reason-coded, one key per lever. The ``no_email_`` prefix is kept so the
                # digest still groups them together and an old run's logs stay comparable.
                _skip_email(f"no_email_{contact_reason}")
                draft_allowed = False

            try:
                state = run_lead(
                    lead, retriever=retriever, chat=chat, draft_allowed=draft_allowed
                )
            except BudgetExhausted:
                budget_exhausted = True
                logger.warning("budget exhausted at $%.4f — stopping run", tracker.usd())
                break

            scored = state.get("scored") or {}
            fit_score = int(scored.get("fit_score", 0))
            if scored:
                fit_scores.append(fit_score)
                _src(lead.source)["scores"].append(fit_score)
                metrics.observe("fit_score", fit_score)
                if fit_score >= settings.min_fit_score:
                    metrics.inc("leads_qualified_total")

            # --- second chance: find the address ourselves -------------------
            # Only for leads that ALREADY cleared the bar, which is why this cannot live
            # in the pre-gate above: discovery costs up to a dozen HTTP requests against
            # a stranger's website, and 778 of the 1047 leads in the measured window were
            # never worth one. Here the population is the 269 that qualified, of which 262
            # had nowhere to send anything — the largest single loss in the funnel, and the
            # only one where the missing thing is a fact about the world rather than a
            # decision we made.
            email_override: str | None = None
            if (
                not has_contact
                and fit_score >= settings.min_fit_score
                and getattr(settings, "discover_contacts", False)
                and discovery_attempts
                < getattr(settings, "max_contact_discoveries_per_run", 0)
            ):
                discovery_attempts += 1
                try:
                    from outreach.discover import discover_contact

                    hit = discover_contact(lead)
                except Exception as exc:  # discovery must never break the lead loop
                    logger.warning("contact discovery failed for %s: %s", lead.url, exc)
                    hit = None
                if hit is not None and not _domain_is_evidence(lead, hit.domain):
                    # The domain was GUESSED from the company name, not taken from the
                    # post's own host. See settings.discover_send_to_guessed_domains: the
                    # first live execution of that path returned a stranger's address, so
                    # the address is handed to the owner instead of mailed. Recorded as a
                    # discovery either way — it is a real find, and hiding it would make
                    # the guessed path unevaluable, which is the reason it stays off.
                    proposed_contacts.append(
                        {
                            "email": hit.email,
                            "domain": hit.domain,
                            "source_url": hit.source_url,
                            "title": lead.title,
                            "company": lead.company or "",
                            "lead_url": lead.url,
                            "fit_score": fit_score,
                        }
                    )
                    if not getattr(settings, "discover_send_to_guessed_domains", False):
                        logger.info(
                            "proposing (not sending) %s for %s — domain guessed from the"
                            " company name, not from %s",
                            hit.email,
                            lead.company,
                            lead.url,
                        )
                        hit = None
                if hit is not None:
                    email_override = hit.email
                    has_contact = True
                    discovered += 1
                    _src(lead.source)["discovered"] += 1
                    logger.info(
                        "discovered %s for %s via %s", hit.email, lead.company, hit.source_url
                    )
                    # The lead was scored with ``draft_allowed=False``, so there is no
                    # proposal to send — it has to be re-run now that there is somewhere
                    # to send one. That repeats the qualify call, which is the cheap
                    # Sonnet stage; the alternative is threading a resume point through
                    # the graph to save cents on at most ``max_contact_discoveries_per_run``
                    # leads a day, and a second entry point into the graph is a far more
                    # expensive thing to own than a duplicated cheap call.
                    try:
                        state = run_lead(
                            lead, retriever=retriever, chat=chat, draft_allowed=True
                        )
                    except BudgetExhausted:
                        budget_exhausted = True
                        logger.warning(
                            "budget exhausted at $%.4f — stopping run", tracker.usd()
                        )
                        break
                    rescored = state.get("scored") or {}
                    if rescored:
                        # The score is REPLACED, not appended. ``fit_scores`` is one row
                        # per lead and it is the sample the min_fit_score decision is made
                        # from, so a lead counted twice would drag that median toward
                        # whatever happens to get discovered.
                        fit_score = int(rescored.get("fit_score", fit_score))

            # A qualified lead with no email is not a dead lead, it is a lead the owner
            # applies to by hand. Captured BEFORE the disposition check below, because an
            # uncontactable lead never reaches "queue" — that is precisely why these were
            # invisible. Discovery runs first, so a lead only lands here once automation
            # has genuinely run out of routes.
            if not has_contact and fit_score >= settings.min_fit_score:
                apply_yourself.append(
                    {
                        "title": lead.title,
                        "url": lead.url,
                        "company": lead.company or "",
                        "source": lead.source,
                        "fit_score": fit_score,
                        # Carried so the digest can say WHY it is being handed over. An
                        # "apply by hand" list with no reason invites the reader to assume
                        # the sources are bad, when 3 of the 4 reasons are ours to fix.
                        "contact_reason": contact_reason,
                        # For the apply pack: the model needs the post to write the note.
                        "description": (lead.description or "")[:4000],
                    }
                )

            if state.get("disposition") != "queue":
                dropped += 1
                continue

            # Enforce the per-day proposal cap before persisting another draft.
            with get_session() as session:
                if _proposals_today(session) >= settings.max_proposals_per_day:
                    logger.info("max_proposals_per_day reached — stop queuing")
                    dropped += 1
                    break

            proposal = state.get("proposal") or {}
            verdict = state.get("verdict") or {}

            with get_session() as session:
                record = LeadRecord(
                    source=lead.source,
                    external_id=lead.external_id,
                    title=lead.title,
                    description=lead.description or "",
                    url=lead.url or "",
                    company=lead.company,
                    budget=lead.budget,
                    tags=list(lead.tags or []),
                    posted_at=lead.posted_at,
                    fit_score=fit_score,
                    status=LeadStatus.drafted,
                )
                record.proposals.append(
                    ProposalRecord(
                        body=proposal.get("body", ""),
                        suggested_rate=proposal.get("suggested_rate", "") or "",
                        cited_projects=list(proposal.get("cited_projects", []) or []),
                        status=ProposalStatus.draft,
                    )
                )
                session.add(record)
                session.flush()
                lead_db_id = record.id

            queued += 1
            _src(lead.source)["queued"] += 1
            metrics.inc("proposals_drafted_total")
            metrics.observe("proposal_quality", int(verdict.get("quality_score", 0)))
            queued_leads.append(
                {"id": lead_db_id, "title": lead.title, "fit_score": fit_score}
            )

            # --- auto cold-email outreach (email-only; never platform submit) ---
            if auto_email:
                try:
                    result = _maybe_email_lead(
                        lead=lead,
                        scored_state=state.get("scored") or {},
                        research_state=state.get("research") or {},
                        fit_score=fit_score,
                        lead_db_id=lead_db_id,
                        settings=settings,
                        retriever=retriever,
                        chat=chat,
                        email_override=email_override,
                    )
                    if result == "sent":
                        emailed += 1
                    else:
                        _skip_email(result)
                except Exception as exc:  # email must never break the lead loop
                    logger.warning("auto-email failed for lead %s: %s", lead_db_id, exc)
                    _skip_email("error")
    finally:
        set_cost_tracker(None)

    stats = {
        "fetched": fetched,
        "new": new,
        # `contactable` is the leading indicator for this whole system: no amount of
        # proposal quality matters if nothing is reachable. Surfaced as a top-level
        # stat (not buried in emailed_skipped) so the weekly review can act on it.
        "contactable": contactable,
        # Both numbers, always. ``discovered`` alone cannot be read: 0 of 0 attempts means
        # nothing qualified without an address (good), 0 of 40 means discovery ran and
        # found nothing (a broken selector, a blocked user-agent, an ATS-hosted world) —
        # and a zero with no denominator has already sent this project down the wrong
        # lever more than once.
        "discovered": discovered,
        "discovery_attempts": discovery_attempts,
        "proposed_contacts": sorted(
            proposed_contacts, key=lambda r: r.get("fit_score", 0), reverse=True
        ),
        "uncontactable_skipped": uncontactable_skipped,
        "queued": queued,
        "dropped": dropped,
        "skipped": skipped,
        "emailed": emailed,
        "emailed_skipped": emailed_skipped,
        "cost_usd": tracker.usd(),
        "budget_exhausted": budget_exhausted,
        # Highest-scoring first: the digest shows the top few, so ordering here decides
        # which ones the owner actually sees.
        "apply_yourself": sorted(
            apply_yourself, key=lambda r: r.get("fit_score", 0), reverse=True
        ),
    }
    # Derived, not counted: any new lead that produced no score, whatever the reason
    # (budget exhausted mid-run, a graph error, a future pre-gate). Counting it at each
    # skip site would mean a new skip path silently omits itself, which is how the
    # censored-sample bug got in.
    # ``budget_exhausted`` is passed, not re-derived: the bottleneck line is the only
    # place the run says what stopped it, and it read "none — leads are clearing the bar"
    # on a run that broke off against its spend cap. The flag was already in ``stats``
    # two lines up; the summary just could not see it.
    # ``blocked_no_contact`` is the apply-yourself list, which by construction is exactly
    # "cleared the bar, has no address". Passed in rather than re-derived so the headline
    # bottleneck and that section can never disagree about the same run.
    # Computed BEFORE the fit summary because the contacts bottleneck now names the
    # sources on each side of the split, and those counts live in these rows.
    stats["by_source"] = _per_source_summary(by_source, settings.min_fit_score)
    stats["fit"] = _fit_summary(
        fit_scores,
        settings.min_fit_score,
        unscored=max(0, new - len(fit_scores)),
        budget_exhausted=budget_exhausted,
        blocked_no_contact=len(apply_yourself),
        source_rows=stats["by_source"],
    )

    # --- paste-and-submit packs for the hand-off ---------------------------------
    # Built here, after the stats are assembled, because the input is the SORTED
    # apply-yourself list: the cap has to cut the lowest scores, and only this list is
    # ordered. Costs ~$0.013 a pack against a ~$2.30 run, and it is the difference
    # between handing the owner 262 links and handing over work that was already paid
    # for in a form they can submit. Never raises by contract; wrapped anyway, because
    # nothing about a digest extra may cost a run its stats.
    stats["apply_packs"] = []
    if getattr(settings, "apply_packs", False) and stats["apply_yourself"]:
        # The tracker is RE-INSTALLED for this, and ``cost_usd`` re-read afterwards. The
        # ``finally`` above uninstalls it, so building packs here without this would be a
        # model call that no budget gates and no run reports: the same shape as every
        # other guard in this project that reported success while not doing its job, only
        # this one costs money per run forever. Same tracker instance, so the packs spend
        # the REMAINING budget rather than getting a fresh $5.00.
        set_cost_tracker(tracker)
        try:
            from outreach.apply_pack import build_apply_packs

            packs = build_apply_packs(
                stats["apply_yourself"],
                retriever=retriever,
                chat=chat,
                limit=getattr(settings, "max_apply_packs_per_run", 5),
            )
            stats["apply_packs"] = [
                {"text": pack.to_text(), "html": pack.to_html()} for pack in packs
            ]
        except BudgetExhausted:
            # The run's spend ceiling, hit while writing packs. Not an error: the leads
            # are still in apply_yourself and still get their links.
            stats["budget_exhausted"] = True
            logger.warning("apply packs skipped: run budget exhausted")
        except Exception as exc:  # pragma: no cover - build_apply_packs never raises
            logger.warning("apply pack build failed: %s", exc)
        finally:
            set_cost_tracker(None)
            stats["cost_usd"] = tracker.usd()

    if notify:
        try:
            from interfaces.notify import send_digest

            top = sorted(queued_leads, key=lambda d: d["fit_score"], reverse=True)
            send_digest(stats, top)
        except Exception as exc:  # notification must never break a run
            logger.warning("digest notification failed: %s", exc)

    return stats


def pipeline_stats() -> dict:
    """Lead counts grouped by status (used by the MCP server, dashboard, CLI)."""
    init_db()
    counts: dict[str, int] = {status.value: 0 for status in LeadStatus}
    total = 0
    proposals = 0
    with get_session() as session:
        for lead in session.query(LeadRecord).all():
            counts[lead.status.value] = counts.get(lead.status.value, 0) + 1
            total += 1
        proposals = session.query(ProposalRecord).count()
    return {"total_leads": total, "total_proposals": proposals, "by_status": counts}


def top_queued(n: int = 5) -> list[dict]:
    """Return the highest-fit drafted leads awaiting human review."""
    init_db()
    out: list[dict] = []
    with get_session() as session:
        rows = (
            session.query(LeadRecord)
            .filter(LeadRecord.status == LeadStatus.drafted)
            .order_by(LeadRecord.fit_score.desc(), LeadRecord.id.desc())
            .limit(max(0, n))
            .all()
        )
        for lead in rows:
            out.append(
                {
                    "id": lead.id,
                    "title": lead.title,
                    "fit_score": lead.fit_score,
                    "company": lead.company,
                    "url": lead.url,
                    "source": lead.source,
                }
            )
    return out
