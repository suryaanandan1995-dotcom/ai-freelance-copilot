"""The numeric gates must be clearable, and must agree with each other.

Two production defects motivate this file, both of the same family as the rest of
this repo's history: a gate that cannot pass for the reason it exists.

1. **``outreach_min_fit`` was 80 and had never been cleared once.** Measured maxima
   across live runs: 72 (run 31033943812), 78 (30988060139), 52 (30909649401). The
   pipeline therefore drafted a proposal, paid Opus prices for it, recorded
   ``queued: 1`` — and then skipped the send as ``low_fit``. Twenty-four runs
   reported "success" while the outreach channel was closed by a constant.

2. **The run cap, not cost, was the binding constraint.** That same run fetched 186
   leads from seven sources and considered 50. It spent $0.13 against a $2.00
   ceiling — 15x headroom — and the best feed (contract_jobs) got 9 of the 50 slots.

These are lints on the *relationships between* settings, not unit tests of any one
caller. A single setting can only be judged wrong against measurement; a pair of
settings can be judged wrong against each other, which is what makes it testable.
"""
from __future__ import annotations

from config import Settings
from optimizer import optimizer


def _settings() -> Settings:
    """Committed defaults only — never the developer's .env."""
    return Settings(_env_file=None)


# --------------------------------------------------------------------------- #
# the email gate must be reachable
# --------------------------------------------------------------------------- #
def test_email_gate_is_not_stricter_than_the_draft_gate():
    """The defect, stated as an invariant.

    ``min_fit_score`` decides what gets drafted; ``outreach_min_fit`` decides what
    gets sent. Setting the second above the first creates a band of leads that are
    always drafted and never sent — the most expensive possible outcome, since the
    draft is the Opus call and the send is free. A lead good enough to write to is
    good enough to write to.
    """
    cfg = _settings()
    assert cfg.outreach_min_fit <= cfg.min_fit_score, (
        f"outreach_min_fit={cfg.outreach_min_fit} exceeds min_fit_score="
        f"{cfg.min_fit_score}: every lead scoring in between is drafted at Opus "
        "prices and then discarded as low_fit, which is what made 24 consecutive "
        "runs report success while emailing nobody."
    )


def test_email_gate_is_within_reach_of_measured_scores():
    """A threshold above every score the scorer has ever produced sends nothing.

    The three highest fit scores observed in production are 78, 72 and 52. A gate at
    80 is unclearable *given this scorer*; if the scorer is later recalibrated to
    produce higher scores, raise this ceiling in the same commit and say why.
    """
    highest_ever_observed = 78
    assert _settings().outreach_min_fit <= highest_ever_observed, (
        "No production run has ever scored a lead above 78, so a gate above that "
        "cannot fire. Either lower the gate or recalibrate the scorer — but do not "
        "leave a send path that is closed by arithmetic."
    )


def test_email_gate_still_excludes_the_bulk_of_leads():
    """The mirror failure: a gate lowered until it stops being a gate.

    Sender reputation is the one asset cold outreach cannot rebuy, so the point of
    this threshold is to exclude weak fits. Measured distribution: p50 28, p90 58.
    Anything at or below 58 would email the ordinary listing.
    """
    p90_fit = 58
    assert _settings().outreach_min_fit > p90_fit, (
        "outreach_min_fit has fallen to where it would email the p90 lead. The gate "
        "exists to protect deliverability; disabling it is not the same as fixing it."
    )


def test_email_gate_agrees_with_the_optimizer_window():
    """Otherwise the configured value and the value actually used differ, silently.

    ``optimizer._clamp_fit`` bounds the self-tuner to a safe window. A default
    outside that window is clamped on the way in, so ``config.py`` would document a
    threshold the running system never uses — the same class of defect as a value in
    ``.env`` that never reaches ``os.environ``.
    """
    fit = _settings().outreach_min_fit
    assert optimizer._FIT_MIN <= fit <= optimizer._FIT_MAX, (
        f"outreach_min_fit={fit} is outside the optimizer window "
        f"[{optimizer._FIT_MIN}, {optimizer._FIT_MAX}] and would be silently clamped."
    )
    assert optimizer._clamp_fit(fit) == fit


# --------------------------------------------------------------------------- #
# the run cap must not be the binding constraint before spend is
# --------------------------------------------------------------------------- #
def test_run_cap_leaves_room_for_every_source_to_contribute():
    """A cap below (sources x per-source yield) truncates the tail of the source list.

    Leads are interleaved by source before truncation (see ``_interleave_by_source``),
    so a small cap does not starve one source outright — it thins all of them. With
    seven sources fetching up to 50 each, a cap of 50 discarded 136 of 186 leads and
    left the best feed with 9 slots. The cap must be sized so that reach, not the
    cap, is what limits the funnel; ``max_usd_per_run`` is the real safety backstop.
    """
    cfg = _settings()
    plausible_sources = 7
    per_source_yield = 20
    assert cfg.max_leads_per_run >= plausible_sources * per_source_yield, (
        f"max_leads_per_run={cfg.max_leads_per_run} is smaller than the volume "
        "seven sources routinely return, so most fetched leads are thrown away "
        "before scoring while the spend ceiling sits unused."
    )


def test_spend_cap_is_what_bounds_a_run():
    """The cap that should bind is the one denominated in money.

    Raising ``max_leads_per_run`` is only safe because ``max_usd_per_run`` is a hard
    stop and because uncontactable leads are dropped *before* the expensive call
    (``require_contact_before_draft``). If either of those is off, the run cap is
    load-bearing again and this file's advice becomes wrong.
    """
    cfg = _settings()
    assert cfg.max_usd_per_run > 0, "no spend ceiling: the run cap is the only limit"
    assert cfg.require_contact_before_draft is True, (
        "with the pre-draft contact gate off, every lead under the raised run cap "
        "costs a research+draft call, so cost scales with the cap"
    )
