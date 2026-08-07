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

3. **Then the fix for (2) inverted it, and nothing noticed.** Raising
   ``max_leads_per_run`` to 200 was justified by (2) plus the claim that an
   uncontactable lead "costs a regex and a cached DNS lookup, not an Opus call". A
   separate fix then removed the pre-gate ``continue`` so uncontactable leads are
   *scored* (an unscored source cannot be told apart from a badly-scoring one), which
   added ~97 Sonnet qualifications per run. Run 31172835060 (2026-08-07) therefore
   spent $2.004245, stopped on the ceiling at lead ~123 of its 200 allowance, and
   reported ``queued: 8, emailed: 7`` — a number that reads like weak targeting. Two
   settings had come to contradict each other: the lead cap said "consider 200", the
   spend cap could afford ~123, and neither one said so.

These are lints on the *relationships between* settings, not unit tests of any one
caller. A single setting can only be judged wrong against measurement; a pair of
settings can be judged wrong against each other, which is what makes it testable.
"""
from __future__ import annotations

from config import Settings
from costs import _DEFAULT_PRICE, PRICING
from optimizer import optimizer


def _settings() -> Settings:
    """Committed defaults only — never the developer's .env."""
    return Settings(_env_file=None)


# --------------------------------------------------------------------------- #
# the measurement, written down so the next person can re-derive it
# --------------------------------------------------------------------------- #
# Run 31172835060 (2026-08-07, main): cost_usd 2.004245, budget_exhausted True,
# 200 leads considered, 123 new, 122 scored, 1 unscored, queued 8, emailed 7,
# uncontactable_skipped 97 (scored, then drafting suppressed — Sonnet spend only).
#
#   $2.004245 / 122 scored leads = $0.016428 per lead   <- blended, this run's mix
#   200 leads x $0.016428        = $3.29 per full-cap run
_MEASURED_RUN_USD = 2.004245
_MEASURED_LEADS_SCORED = 122
_MEASURED_DRAFTS = 8
# Splitting that total into its two stages: every scored lead pays one qualification,
# and a drafted lead additionally pays research + write. Pricing the draft path at ~5x
# a qualification (Opus output at $25/MTok against Sonnet's $15, over a much longer
# completion) gives  122q + 8*(5q) = 162q = $2.004245  ->  q = $0.012372,
# draft path = $0.061860. Both are order-of-magnitude fences, not precise budgets.
_DRAFT_PATH_MULTIPLE = 5.0
# The scoring model's price when the run above was measured, so the numbers below can
# be re-priced instead of re-measured. See _repricing_factor.
_MEASURED_SCORER_PRICE = (3.0, 15.0)  # claude-sonnet-4-6, $ per MTok (input, output)


def _repricing_factor(cfg: Settings) -> float:
    """Scale the measurement to the models/prices configured *now*.

    This is the mechanism that keeps the assertions below from being pinned to a
    stale sample, the mistake ``test_email_gate_is_within_reach_of_the_scorer``
    documents. A cost-per-lead figure is only meaningful next to the price list it
    was measured against: point ``model_sonnet`` at Haiku, or edit ``costs.PRICING``
    after a price change, and the honest funding requirement moves with it. Deriving
    the requirement from ``PRICING`` means a recalibration in EITHER direction —
    cheaper models or dearer ones — changes what the test demands rather than making
    the test wrong.
    """
    now = PRICING.get(cfg.model_sonnet, _DEFAULT_PRICE)
    return sum(now) / sum(_MEASURED_SCORER_PRICE)


def _usd_per_lead(cfg: Settings) -> float:
    """Blended cost of taking one lead through the run, at current prices."""
    return _MEASURED_RUN_USD / _MEASURED_LEADS_SCORED * _repricing_factor(cfg)


def _usd_per_qualification(cfg: Settings) -> float:
    """Cost of the one call EVERY lead under the cap pays for."""
    denominator = _MEASURED_LEADS_SCORED + _MEASURED_DRAFTS * _DRAFT_PATH_MULTIPLE
    return _MEASURED_RUN_USD / denominator * _repricing_factor(cfg)


def _usd_per_draft_path(cfg: Settings) -> float:
    """Cost of qualification + research + write for one lead."""
    return _usd_per_qualification(cfg) * (1.0 + _DRAFT_PATH_MULTIPLE)


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


def test_email_gate_is_within_reach_of_the_scorer():
    """A threshold above what the scorer can produce sends nothing.

    This assertion was originally written as ``<= 78``, on the belief that 78 was the
    highest score the scorer had ever produced. That was wrong, and wrong in an
    instructive way: 78 was the maximum of the three most recent *aggregates*, the
    only numbers left in the Actions logs. The July run in ``copilot.db`` records 13
    leads from one source scoring **72 to 88** — six at 82+, two at 87/88 — so the
    scorer clears 80 comfortably when it is shown listings with a real title, company
    and description.

    Pinning a ceiling to that stale sample would have made this test the next gate
    that fights the product: it would fail any future recalibration upward. So the
    bound is the domain bound. What keeps the gate honest is the *relationship* tests
    around it, plus the funnel report telling you which sources were never scored.
    """
    fit = _settings().outreach_min_fit
    assert 0 < fit <= 100, "a fit gate outside 0-100 can never fire"


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
    stop and because uncontactable leads never reach the *expensive* call
    (``require_contact_before_draft`` suppresses research+write, though they are still
    qualified). With the pre-draft gate off, every lead under the raised run cap pays
    the Opus path and the arithmetic in the tests below understates the run by ~5x.
    """
    cfg = _settings()
    assert cfg.max_usd_per_run > 0, "no spend ceiling: the run cap is the only limit"
    assert cfg.require_contact_before_draft is True, (
        "with the pre-draft contact gate off, every lead under the raised run cap "
        "costs a research+draft call, so cost scales with the cap"
    )


# --------------------------------------------------------------------------- #
# ...and the spend cap must actually be able to pay for that run
# --------------------------------------------------------------------------- #
def test_spend_ceiling_can_fund_a_full_lead_cap_run():
    """A lead cap the spend cap cannot afford is decorative.

    This is defect (3) in the module docstring, as an invariant. ``max_leads_per_run``
    is a promise about reach; ``max_usd_per_run`` is what pays for it. When the second
    is smaller than ``cap x cost-per-lead``, the run truncates partway down the lead
    list and *no output says so*: the funnel report sees a short, arbitrary sample of
    scores and blames the lead mix. Run 31172835060 stopped at lead ~123 of 200 and
    reported ``queued: 8`` — indistinguishable from bad targeting.

    Note what is asserted and what is not. The bound is derived (cap x measured
    cost-per-lead, re-priced through ``costs.PRICING``), so lowering the lead cap,
    switching to a cheaper scorer, or raising the ceiling all satisfy it, and only a
    *contradiction between two settings* fails it. Hardcoding "must be >= 3.29" would
    have been the same mistake as pinning the fit gate to a stale maximum: it would
    fail the next honest recalibration in either direction.
    """
    cfg = _settings()
    needed = cfg.max_leads_per_run * _usd_per_lead(cfg)
    assert cfg.max_usd_per_run >= needed, (
        f"max_usd_per_run=${cfg.max_usd_per_run:.2f} cannot fund a full "
        f"max_leads_per_run={cfg.max_leads_per_run} run at the measured "
        f"${_usd_per_lead(cfg):.6f}/lead (needs ${needed:.2f}). The run will stop on "
        "the budget partway down the lead list, and the report will blame targeting "
        "for a truncated sample — exactly what run 31172835060 did. Either raise the "
        "ceiling or lower the lead cap, but do not let them disagree."
    )


def test_spend_ceiling_funds_the_worst_mix_the_other_caps_allow():
    """Cost-per-lead is a mix, so fund the mix the settings actually permit.

    $0.016428/lead is blended over a run where only 8 of 122 leads reached the Opus
    draft. A run whose leads happen to be more contactable spends more for the same
    lead count, and the only thing bounding how many drafts a run may pay for is
    ``max_proposals_per_day``. So the funding requirement is
    ``cap x qualification + min(cap, daily draft cap) x draft-path`` — three settings
    that must agree, not two. Without this, a good day is what breaks the run.
    """
    cfg = _settings()
    drafts = min(cfg.max_leads_per_run, cfg.max_proposals_per_day)
    worst = cfg.max_leads_per_run * _usd_per_qualification(cfg) + drafts * _usd_per_draft_path(cfg)
    assert cfg.max_usd_per_run >= worst, (
        f"max_usd_per_run=${cfg.max_usd_per_run:.2f} funds the average mix but not the "
        f"one these settings allow: {cfg.max_leads_per_run} qualifications at "
        f"${_usd_per_qualification(cfg):.6f} plus {drafts} draft paths at "
        f"${_usd_per_draft_path(cfg):.6f} = ${worst:.2f}. A run with unusually "
        "contactable leads would truncate, i.e. the funnel gets worse when the leads "
        "get better."
    )


def test_spend_ceiling_is_still_a_backstop_not_a_blank_cheque():
    """The mirror failure: a ceiling raised until it stops being a ceiling.

    This is an unattended weekday job (``.github/workflows/outreach.yml``, ``0 6 * * 1-5``
    — up to 23 runs/month), so the ceiling is the only thing standing between a retry
    loop or a prompt regression and an overnight bill. "Raise it until the run fits" is
    only half the fix; it must still be a small multiple of what a legitimate full run
    costs. Expressed as a ratio to the derived worst case rather than as a dollar
    figure, so it keeps its meaning if the lead cap or model pricing moves.
    """
    cfg = _settings()
    drafts = min(cfg.max_leads_per_run, cfg.max_proposals_per_day)
    worst = cfg.max_leads_per_run * _usd_per_qualification(cfg) + drafts * _usd_per_draft_path(cfg)
    max_slack = 3.0
    assert cfg.max_usd_per_run <= max_slack * worst, (
        f"max_usd_per_run=${cfg.max_usd_per_run:.2f} is more than {max_slack:g}x the "
        f"${worst:.2f} a legitimate full-cap run costs, so it would no longer stop a "
        "runaway loop before it stopped mattering. An unattended scheduled job needs a "
        "ceiling that binds on the pathological run, not just on the normal one."
    )


def test_monthly_worst_case_spend_is_stated_and_bounded():
    """The number the owner is actually agreeing to is per *month*, not per run.

    The per-run ceiling is the reviewable knob, but the bill is
    ``ceiling x runs/month``. The outreach schedule is weekday-daily, so 23 runs is the
    ceiling on run count for any month. Asserted against a deliberately generous
    budget: this test exists to force the multiplication into view before a per-run
    figure is raised again, not to legislate a spending level.
    """
    cfg = _settings()
    runs_per_month = 23  # weekdays 06:00 UTC, worst-case month
    monthly_worst_case = cfg.max_usd_per_run * runs_per_month
    assert monthly_worst_case <= 150.0, (
        f"max_usd_per_run=${cfg.max_usd_per_run:.2f} x {runs_per_month} scheduled runs "
        f"= ${monthly_worst_case:.0f}/month worst case. That is a bill, not a "
        "guardrail — an unattended side-project pipeline should not be able to reach "
        "it silently. Re-derive the per-run cost before raising this."
    )
