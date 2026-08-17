"""Extract a real contact email from a lead.

Most email-reachable leads come from Hacker News "Who is hiring?" comments where
posters publish a direct address ("email jobs@acme.com"). We regex-scan the
lead's description and any string values in ``lead.raw``, lowercase + validate,
and reject obvious non-contact addresses (noreply, error/asset domains, example
placeholders).

**"The first good address wins" was wrong, and it shipped.** Job descriptions from
ATS-backed boards run 3,000-15,000 chars and carry a legal/compliance footer that
appears *long before* any hiring address. With only bounce-type locals rejected,
every one of these live-measured addresses passed the gate and was eligible to be
cold-emailed:

* ``accessibilitysupport@nbcuni.com``      — disability accommodations
* ``candidate_accommodations@upstart.com`` — disability accommodations
* ``security@harness.io``                  — vulnerability disclosure
* ``hr@launchdarkly.com``                  — employee relations / ATS routing

The single lead the last production run marked ``productive`` and queued for
auto-email resolved to one of the accommodations inboxes. Cold-pitching freelance
services to a disability-accommodations mailbox is the worst possible first
impression, and it spends the one asset this system cannot rebuy — sender
reputation (see the note above :func:`find_deliverable_email`).

Two changes, in the order they matter:

1. :data:`_NON_HIRING_TOKENS` / :data:`_NON_HIRING_SUBSTRINGS` reject institutional
   mailboxes outright. The mirror failure is a gate so strict the market looks
   empty, so ``jobs@``, ``careers@``, ``hiring@``, ``recruiting@``, ``talent@``,
   ``hello@``, ``info@``, ``contact@`` and ordinary personal names all still pass.
2. :func:`_rank_candidates` prefers a hiring-cued address over the earliest one, so
   an ``accessibility@`` footer no longer outranks the ``careers@`` line below it.
"""
from __future__ import annotations

import re
from typing import Any

from core.schemas import Lead

# Reasonable email matcher (not RFC-perfect, but good for scraped free text).
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}",
)

# Obfuscated forms people use to dodge scrapers, e.g.
#   "name [at] domain [dot] com", "name (at) domain (dot) com".
# We de-obfuscate the whole text first, then run the normal matcher over it.
_AT_RE = re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.IGNORECASE)
_DOT_RE = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.IGNORECASE)
# Bare " at "/" dot " forms, only when they clearly sit between address parts.
_AT_WORD_RE = re.compile(r"(?<=[\w])\s+at\s+(?=[\w])", re.IGNORECASE)
_DOT_WORD_RE = re.compile(r"(?<=[\w])\s+dot\s+(?=[\w])", re.IGNORECASE)


def _deobfuscate(text: str) -> str:
    """Normalize common ``x [at] y [dot] z`` obfuscations into ``x@y.z``."""
    out = _AT_RE.sub("@", text)
    out = _DOT_RE.sub(".", out)
    # Only touch bare-word forms when a bracketed/at marker was present, to
    # avoid mangling ordinary prose like "meet at dot com office".
    if "@" in out and "@" not in text:
        out = _DOT_WORD_RE.sub(".", out)
    elif _AT_WORD_RE.search(text) and (
        _DOT_RE.search(text) or " dot " in text.lower()
    ):
        out = _AT_WORD_RE.sub("@", out)
        out = _DOT_WORD_RE.sub(".", out)
    return out

# Local-parts that are never a person you should cold-email.
_BAD_LOCAL_PREFIXES = (
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "notifications",
)

# --- institutional mailboxes -----------------------------------------------------
#
# Non-hiring departments that a long JD's boilerplate footer publishes. Matched as
# whole tokens of the local-part (split on ``. _ - +`` and digits), NOT as
# substrings, because the over-strict version of this gate is the failure this repo
# keeps repeating — a filter that rejects real people makes the market look empty.
#
# The tradeoff that buys, stated explicitly: a person named **Salesi** or **Legaspi**
# still passes (their token is "salesi"/"legaspi", not "sales"/"legal"), while a
# genuine ``sales.team@`` or ``legal-notices@`` is still caught because those split
# into tokens. The residual hole — a company using the exact single token ``sales``
# as its only inbox — costs us one lead; the mirror mistake costs a real person a
# cold pitch to their accommodations desk. We take the cheap loss.
_NON_HIRING_TOKENS = frozenset(
    {
        # accessibility / accommodations — the address the last production run
        # actually queued. Never, under any circumstances, a sales target.
        "accessibility",
        "accommodation",
        "accommodations",
        "ada",
        # security & data protection
        "security",
        "infosec",
        "abuse",
        "privacy",
        "dpo",
        "gdpr",
        # legal / compliance
        "legal",
        "compliance",
        "ethics",
        "copyright",
        "dmca",
        "trademark",
        # comms / IR — a human reads these, but not for procurement
        "press",
        "pressoffice",
        "media",
        "investor",
        "investors",
        "ir",
        # inbound funnels that route away from a decision-maker
        "support",
        "helpdesk",
        "webmaster",
        "hostmaster",
        "billing",
        "invoice",
        "invoices",
        "accounts",
        "accounting",
        "payables",
        "sales",
        "marketing",
        "unsubscribe",
        "optout",
        # HR/employee-relations desks: these handle *existing* employees and ATS
        # routing, not vendor conversations. hr@launchdarkly.com was measured live.
        "hr",
        "humanresources",
        "people",
        "peopleops",
        "benefits",
        "payroll",
    }
)

# Glued-together institutional locals with no separator to tokenize on — e.g. the
# measured ``accessibilitysupport@nbcuni.com``. Only words long and specific enough
# that no human name can contain them belong here; anything shorter goes in
# _NON_HIRING_TOKENS so it is boundary-matched instead.
_NON_HIRING_SUBSTRINGS = (
    "accessibility",
    "accommodation",
    "compliance",
    "unsubscribe",
    "webmaster",
    "dataprotection",
    "dataprivacy",
    "investorrelations",
    "humanresources",
)

# Local-parts that ARE the hiring contact. Prefix-matched against each token so
# plurals and compounds count ("careers", "talentacquisition", "recruiting").
# These outrank everything else during selection and must never be rejected.
_HIRING_STEMS = (
    "job",
    "career",
    "hiring",
    "hire",
    "apply",
    "applicant",
    "recruit",
    "talent",
    "staffing",
)

# Words in the surrounding prose that mark an address as the one to write to.
# Used only to break ties between otherwise-acceptable candidates.
_HIRING_CUE_RE = re.compile(
    r"\b(?:e-?mail|apply|applications?|send\s+(?:your|us|me|a)|resume|résumé|cv|"
    r"contact\s+us|reach\s+out|reach\s+me|get\s+in\s+touch|write\s+to|"
    r"hiring|we'?re\s+looking|drop\s+(?:us|me))\b",
    re.IGNORECASE,
)

#: Characters either side of a match that count as "surrounding text". Wide enough
#: to span "Interested? Send your CV to <addr>", narrow enough that a compliance
#: footer 2,000 chars up the page cannot lend its cue to an unrelated address.
_CUE_WINDOW = 160

# --- the surrounding prose, not the local-part -----------------------------------
#
# :data:`_NON_HIRING_TOKENS` asks "does this mailbox *look* institutional", and that
# question has a ceiling: it cannot see what the sentence containing the address says
# the address is *for*. Measured across ~430 live listings, of ~31 addresses the gate
# ACCEPTED only 6 were genuine hiring contacts and just 2 of those sat on engineering
# roles. The other ~25 were accommodations desks, fraud-report inboxes and legal/ATS
# routing. So the gate's real defect was the opposite of the one it was built for: it
# is too LOOSE, not too strict.
#
# Worse, :func:`_is_hiring_local` returning True short-circuits every other check, so
# a hiring-flavoured local-part is *guaranteed* to pass no matter what the prose says.
# Three verbatim examples from the fetch on 2026-08-07:
#
#   recruiting@ppg.com          "If you need an adjustment due to a disability,
#                                please email recruiting@ppg.com."
#   applicationassistance@      "Alternative methods of applying ... for individuals
#     sailpoint.com              unable to submit an application because of a
#                                disability. Contact <addr> ... NOTE: Any unsolicited
#                                resumes sent by candidates or agencies to this email"
#   isamoylova@mirantis.com     "You also have the right to appeal any decisions made
#                                by ADMT by sending your request to <addr>"
#
# The Mirantis one is why this has to be a prose check and not a longer reject list:
# the local-part is an ordinary human name, so no amount of local-part cleverness can
# ever catch it. Only the sentence gives it away.
#
# **Why phrases and not topic words.** The tempting implementation matches
# ``disability``/``privacy``/``fraud`` near the address. That would be the over-strict
# mirror this module's header warns about, and it would fire almost everywhere: every
# US job ad carries an EEO footer reading "without regard to race, color, religion,
# sex, ... disability status, protected veteran status", so a bare ``disability``
# keyword would disqualify the legitimate ``careers@`` address of most listings and
# make the market look empty. What actually marks an address as off-limits is prose
# that *routes you to it for a non-hiring purpose* — "if you require an accommodation,
# contact X", "report it to X". So every pattern below contains a routing verb or an
# explicit prohibition, never a bare subject word.
#
# The patterns are split into two tiers because they are not equally decisive.
# :data:`_HARD_BLOCK_RE` names the address's purpose or names the address itself
# ("unsolicited resumes sent to this email"), and nothing can override it.
# :data:`_SOFT_BLOCK_RE` is a *general* prohibition aimed at somebody else — "no
# agencies" is addressed to recruitment firms, and a listing can say it in its footer
# while still printing "Apply: careers@…" three words from the address. Blocking those
# outright would be over-strict in precisely our target market: UK/EU contract ads say
# "no agencies" constantly, so a hard block there would cost real leads for a warning
# that was never aimed at a freelancer applying directly.
_HARD_BLOCK_RE = re.compile(
    # accommodations / accessibility desks — the largest measured group (14 of ~31)
    r"(?:need|require|request(?:ing)?|needing)\s+(?:an?\s+)?"
    r"(?:reasonable\s+)?(?:accommodation|accomodation|adjustment|assistance|aid)"
    r"|reasonable\s+accommodation"
    r"|(?:due\s+to|because\s+of|owing\s+to|on\s+account\s+of)\s+(?:a\s+)?disabilit"
    r"|unable\s+to\s+(?:submit|complete|apply|access|use)"
    r"|alternative\s+(?:method|means|format)"
    r"|assistance\s+(?:with|in)\s+(?:the\s+)?(?:applic|complet)"
    r"|accessible\s+format"
    # anti-recruitment-fraud warnings. The address in these is where you report a
    # scam, and 4 measured domains appeared ONLY inside such a warning.
    r"|fraudulent|fraud(?:ulent)?\s+(?:job|offer|recruit|email|activit)"
    r"|(?:job|recruitment|hiring|employment)\s+scam"
    r"|scam(?:s|mers?)?\b"
    r"|phishing|impersonat"
    r"|we\s+(?:will\s+)?(?:never|do\s+not|don'?t)\s+(?:ask|request|require|charge)"
    r"|report\s+(?:it|this|them|any\s+such|suspicious)"
    r"|verify\s+the\s+(?:legitimacy|authenticity)"
    # "unsolicited resumes sent to this email will not be honoured" — an explicit
    # prohibition attached to THIS address, measured verbatim on SailPoint.
    r"|unsolicited\s+(?:resum|r[eé]sum|cv|applicat|candidat)"
    # data-protection / ADMT / privacy routing (the Mirantis case)
    r"|(?:personal|candidate)\s+data"
    r"|data\s+protection\s+(?:law|regulation|officer|request)"
    r"|right\s+to\s+(?:appeal|object|erasure|access|be\s+forgotten)"
    r"|automated\s+decision"
    r"|\bADMT\b"
    r"|opt(?:ing)?[-\s]?out"
    r"|(?:exercise|submit)\s+your\s+(?:rights|request)"
    # employee-only / internal routing
    r"|current\s+employees\s+(?:should|must|please)"
    r"|internal\s+(?:applicants?|candidates?|transfer)",
    re.IGNORECASE,
)

#: General prohibitions aimed at agencies rather than at this address. Blocking only
#: when no hiring cue sits closer — see :func:`_is_do_not_contact`.
_SOFT_BLOCK_RE = re.compile(
    r"no\s+agenc(?:y|ies)"
    r"|agenc(?:y|ies)\s+(?:need\s+not|please\s+do\s+not|will\s+not|are\s+not)"
    r"|(?:staffing|recruiting|recruitment|search)\s+(?:agenc|firm|vendor|partner)"
    r"|third[-\s]?part(?:y|ies)\s+(?:agenc|recruit|vendor)"
    r"|direct\s+applicants?\s+only",
    re.IGNORECASE,
)

#: HTML entities that survive :func:`~sources._text.strip_html` on some feeds and
#: would otherwise split a phrase the classifier is trying to match. Measured:
#: "please email&nbsp;recruiting@ppg.com".
_ENTITY_RE = re.compile(r"&(?:nbsp|amp|#\d{1,4}|#x[0-9a-fA-F]{1,4});")

#: Prose window for :data:`_DO_NOT_CONTACT_RE`. Wider *before* the address than
#: after, because the disqualifying sentence overwhelmingly precedes it ("If you
#: require an accommodation, contact <addr>"), but not unbounded: the whole reason
#: this is a window and not a whole-document scan is that a fraud warning in a footer
#: must not disqualify a hiring address 3,000 chars above it. The trailing side still
#: has to be real, because "…to this email" prohibitions come after (SailPoint).
_BLOCK_WINDOW_BEFORE = 220
_BLOCK_WINDOW_AFTER = 120


def _is_do_not_contact(text: str, start: int, end: int) -> bool:
    """True if the prose around an address routes it to a non-hiring purpose.

    Overrides :func:`_is_hiring_local`, deliberately: ``recruiting@ppg.com`` is a
    hiring-stemmed local-part published as a disability-adjustment desk, and the
    local-part winning outright is the specific line that let ~25 of ~31 accepted
    addresses through.

    A hard block is unconditional. A soft block (an agency prohibition, which is
    aimed at recruitment firms and not at us) yields to an explicit hiring cue in the
    same window — "Apply: careers@acme.com. No agencies." is an apply address with a
    footer, not an agency inbox.
    """
    raw = text[max(0, start - _BLOCK_WINDOW_BEFORE) : end + _BLOCK_WINDOW_AFTER]
    window = _ENTITY_RE.sub(" ", raw)
    if _HARD_BLOCK_RE.search(window):
        return True
    if _SOFT_BLOCK_RE.search(window):
        return not _HIRING_CUE_RE.search(window)
    return False

_TOKEN_SPLIT_RE = re.compile(r"[^a-z]+")

#: Local-parts that are a **template for** an address rather than an address.
#:
#: Measured live: a Grafana Labs hiring manager wrote "get in touch with me directly
#: via <linkedin> or ``first.last@grafana.com``" — meaning "work out my name and use
#: this form". The domain is real, so it publishes MX and passed the deliverability
#: check; the mailbox does not exist. Sending there is a guaranteed hard bounce, and
#: hard bounces are what cost sender reputation, the one asset this system cannot
#: rebuy — so this is a worse outcome than extracting nothing at all.
#:
#: Matched against the WHOLE local-part, not per token, because these are only
#: placeholders in their exact template form. ``firstname``/``lastname`` as separate
#: tokens is a real convention (``john.smith@``), and a person can be named Initial.
_PLACEHOLDER_LOCALS = frozenset(
    {
        "first.last",
        "firstname.lastname",
        "first_last",
        "firstname_lastname",
        "firstlast",
        "firstnamelastname",
        "f.last",
        "flast",
        "first.l",
        "name.surname",
        "your.name",
        "yourname",
        "name",
        "email",
        "address",
        "user",
        "username",
        "someone",
        "somebody",
        "anyone",
        "me",
        "you",
    }
)

# Domains/substrings that are placeholders, infra, or asset hosts — not contacts.
_BAD_DOMAIN_SUBSTRINGS = (
    "@example.",
    "@sentry.",
    "@email.example",
    "@test.",
    "@localhost",
    "@domain.",
    "@yourcompany.",
    "@company.com",
)

# Image/asset file extensions sometimes captured as "name@2x.png" etc.
_ASSET_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".css",
    ".js",
    ".ico",
)


def _local_tokens(local: str) -> list[str]:
    """Split a local-part into alphabetic words: ``candidate_accommodations`` →
    ``["candidate", "accommodations"]``.

    Tokenizing is what keeps the gate from eating real people: matching
    ``_NON_HIRING_TOKENS`` against these words means "salesi" never equals "sales".
    """
    return [t for t in _TOKEN_SPLIT_RE.split(local.lower()) if t]


def _is_hiring_local(local: str) -> bool:
    """True if any token of the local-part is a hiring word (``jobs``, ``careers``)."""
    return any(
        token.startswith(_HIRING_STEMS) for token in _local_tokens(local)
    )


def _is_non_hiring_local(local: str) -> bool:
    """True for institutional mailboxes that must never receive a cold pitch.

    A hiring word anywhere in the local-part wins outright: ``jobs-support@`` and
    ``careers.accessibility@`` are hiring inboxes that happen to name a department,
    and dropping them would be the over-strict failure this gate exists to avoid.
    """
    if _is_hiring_local(local):
        return False
    tokens = _local_tokens(local)
    if any(token in _NON_HIRING_TOKENS for token in tokens):
        return True
    # No separator to tokenize on, e.g. "accessibilitysupport@nbcuni.com" (measured).
    return any(sub in local.lower() for sub in _NON_HIRING_SUBSTRINGS)


def _is_good_email(email: str) -> bool:
    email = email.lower()
    if "@" not in email or "." not in email.split("@", 1)[1]:
        return False
    local, _, domain = email.partition("@")
    if any(local.startswith(p) for p in _BAD_LOCAL_PREFIXES):
        return False
    if _is_non_hiring_local(local):
        return False
    if local in _PLACEHOLDER_LOCALS:
        # A template, not a mailbox — and on a real domain, so the MX check passes
        # and the bounce is guaranteed. See _PLACEHOLDER_LOCALS.
        return False
    if any(sub in email for sub in _BAD_DOMAIN_SUBSTRINGS):
        return False
    if email.endswith(_ASSET_EXTENSIONS):
        return False
    # A pure asset filename like "logo@2x.png" — domain segment is a file.
    if any(domain.endswith(ext) for ext in _ASSET_EXTENSIONS):
        return False
    return True


def _iter_raw_strings(value: Any) -> list[str]:
    """Flatten string values out of a (possibly nested) raw dict/list."""
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            found.extend(_iter_raw_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            found.extend(_iter_raw_strings(v))
    return found


def _cue_score(text: str, start: int, end: int) -> int:
    """1 if a hiring cue sits within :data:`_CUE_WINDOW` chars of the match, else 0."""
    window = text[max(0, start - _CUE_WINDOW) : end + _CUE_WINDOW]
    return 1 if _HIRING_CUE_RE.search(window) else 0


# --- why there is no address, not just that there isn't one ----------------------
#
# ``pipeline`` recorded a single skip reason, ``no_email_pregate``, for every lead it
# could not write to. Across the 6 production runs of 2026-08-10..17 that counter read
# **851 of 1047 leads** — 81% of everything fetched — and it was unactionable, because
# it summed three outcomes whose fixes point in opposite directions:
#
#   * the post published no address at all → the lever is a different LEAD SOURCE
#     (Adzuna/ATS listings carry an apply form and nothing else, measured 2026-08-07);
#   * an address was there and OUR OWN GATE refused it → the lever is this module, and
#     the pool is recoverable today at zero acquisition cost;
#   * an address was there, we accepted it, and its domain publishes no MX/A record →
#     the lever is NOTHING. That address is undeliverable no matter what we change.
#
# Without the split we cannot tell whether hundreds of the 851 are recoverable or none
# of them are, and those two worlds justify completely different work. This is the same
# missing-denominator bug this project has now fixed twice at higher layers —
# ``last_error`` separated "the API rejected us" from "nothing matched", and
# ``LeadSource.scanned`` separated "nothing matched" from "the feed was empty". This is
# one level further down: inside a single lead.
#
#: Reason strings, ordered **most recoverable / most specific first**. Two things read
#: this order and they must not disagree, so the rank table below is derived from the
#: tuple rather than written out a second time (six stages each writing their own
#: comparison is a bug class this codebase has paid for).
#:
#: Why THIS order when several candidate addresses in one post each fail differently:
#: report the reason belonging to the BEST candidate, i.e. the one that got furthest
#: through the gate, because that is the only reason that names a lever we could pull.
#: A post carrying both ``support@acme.com`` and ``jobs@no-mx.example`` is NOT a
#: "we have no address" lead and it is NOT a "our gate is too strict" lead — the gate
#: did its job and picked the hiring address; the address is simply dead. Reporting the
#: weaker ``rejected_non_hiring`` there would send someone to loosen a gate that was
#: already right, and reporting ``no_address_in_post`` would send someone to buy a new
#: lead source we do not need. ``domain_refused_mail`` is first because it is the one
#: verdict that closes the lead permanently; ``no_address_in_post`` is last because it
#: is the least informative thing we can say about a post that plainly had an address.
REASON_FOUND = "found"
REASON_DOMAIN_REFUSED_MAIL = "domain_refused_mail"
REASON_REJECTED_DO_NOT_CONTACT = "rejected_do_not_contact"
REASON_REJECTED_NON_HIRING = "rejected_non_hiring"
REASON_NO_ADDRESS_IN_POST = "no_address_in_post"

#: Every reason a lead can be *unreachable* for, most recoverable first. Exported so a
#: report can pre-seed all four keys at zero: an absent key reads as "this never
#: happened" when it usually means "nobody counted", which is the exact failure the
#: 851 figure above is an instance of.
CONTACT_REASONS: tuple[str, ...] = (
    REASON_DOMAIN_REFUSED_MAIL,
    REASON_REJECTED_DO_NOT_CONTACT,
    REASON_REJECTED_NON_HIRING,
    REASON_NO_ADDRESS_IN_POST,
)

#: Higher wins. Derived from :data:`CONTACT_REASONS` so the ordering is stated once.
_REASON_RANK: dict[str, int] = {
    reason: len(CONTACT_REASONS) - i for i, reason in enumerate(CONTACT_REASONS)
}


def _preferred_reason(a: str, b: str) -> str:
    """The more recoverable of two failure reasons — see :data:`CONTACT_REASONS`.

    Used to fold many candidate addresses (and many text blocks) down to the single
    verdict a report can act on. Unknown strings rank 0 so a future reason added to
    only one of the two lists degrades to "the known one wins" instead of crashing.
    """
    return a if _REASON_RANK.get(a, 0) >= _REASON_RANK.get(b, 0) else b


def _best_email_in_with_reason(text: str) -> tuple[str | None, str]:
    """:func:`_best_email_in`, plus WHY when it returns ``None``.

    This is the **single** implementation of the address decision; every other entry
    point in this module is a wrapper that throws part of the answer away. Keeping one
    copy is not tidiness: the gate is five interacting checks (bounce locals,
    institutional locals, placeholder templates, bad domains, do-not-contact prose) and
    a second code path that re-asked any of them would drift from this one silently,
    which is precisely how a report ends up confidently blaming the wrong lever.

    Reason semantics, in the order the checks run per candidate:

    * ``rejected_non_hiring`` — the address failed :func:`_is_good_email`. That covers
      the institutional locals this reason is named for (``support@``, ``privacy@``)
      and also the bounce locals, placeholder templates and asset/example domains,
      all of which are "the string looked like an address but is not a person you can
      pitch". They share a lever (this module's reject lists), so they share a key.
    * ``rejected_do_not_contact`` — the address passed :func:`_is_good_email` but the
      surrounding prose routes it elsewhere (accommodations desk, fraud report, data
      request). Checked second because it needs the match's POSITION, and ranked above
      the previous one because it is the more specific statement about a better address.
    * ``no_address_in_post`` — the regex matched nothing at all in this block.

    ``domain_refused_mail`` is deliberately NOT produced here: deliverability is a
    property of the winning address, decided once in
    :func:`find_deliverable_email_with_reason`, and this function is also used on paths
    (``find_contact_email``) where no DNS check happens at all.

    Ranking of the *acceptable* candidates, highest first — a *later* hiring address
    beats an *earlier* generic one,
    which is the whole point: a compliance footer at char 400 must not beat the
    ``careers@`` line at char 6,000.

    1. the local-part is a hiring word (``jobs``, ``careers``, ``recruiting``);
    2. otherwise, prose near the address invites contact ("email", "send your CV");
    3. otherwise, first found — the pre-existing behaviour, and still exactly what
       happens when there is only one candidate.

    Addresses whose surrounding prose disqualifies them are **dropped, not demoted**
    (see :func:`_is_do_not_contact`). Demotion would be useless in the measured cases:
    an accommodations desk is usually the *only* address in the listing, so ranking it
    last still selects it. "No contact" is the correct answer for those leads.
    """
    # Scan the raw text first (catches "email: x@y.com", "contact: x@y.com"), then a
    # de-obfuscated copy ("name [at] domain [dot] com").
    variants = [text]
    deob = _deobfuscate(text)
    if deob != text:
        variants.append(deob)

    best: tuple[int, int, str] | None = None
    # Starts at the weakest verdict and only ever climbs, via _preferred_reason: with
    # zero regex matches this is the honest answer, and any candidate at all overrides
    # it. Never assigned directly after this line, so no branch can accidentally
    # downgrade a specific verdict back to "no address".
    reason = REASON_NO_ADDRESS_IN_POST
    for variant in variants:
        for order, match in enumerate(_EMAIL_RE.finditer(variant)):
            email = match.group(0).lower().strip(".,;:<>()[]\"'")
            if not _is_good_email(email):
                reason = _preferred_reason(reason, REASON_REJECTED_NON_HIRING)
                continue
            if _is_do_not_contact(variant, match.start(), match.end()):
                # The prose says this mailbox is for accommodations / fraud reports /
                # data requests / agencies. Checked here rather than in
                # _is_good_email because it needs the address's POSITION in the text,
                # which is the only thing that distinguishes "the careers@ line" from
                # "the careers@ in the fraud warning".
                reason = _preferred_reason(reason, REASON_REJECTED_DO_NOT_CONTACT)
                continue
            local, _, _domain = email.partition("@")
            rank = 2 if _is_hiring_local(local) else _cue_score(
                variant, match.start(), match.end()
            )
            # Negative order makes earlier matches win ties without a second sort key.
            scored = (rank, -order, email)
            if best is None or scored > best:
                best = scored
        # The raw variant already yielded a winner; the de-obfuscated copy is a
        # fallback for text that hid its address, not a second opinion. Note the
        # rejection reasons from the raw pass are deliberately KEPT when we fall
        # through to the de-obfuscated one: "support@ was there and we refused it" is
        # still true of the post whichever spelling of the text we read it in.
        if best is not None:
            break
    if best is not None:
        return best[2], REASON_FOUND
    return None, reason


def _best_email_in(text: str) -> str | None:
    """Best acceptable address in one block of text, or ``None``.

    Thin wrapper that discards the reason. It stays a separate name because callers
    (and one behavioural test pinning the placeholder-vs-real ranking) only ever cared
    about the address; see :func:`_best_email_in_with_reason` for the ranking rules and
    the reason vocabulary.
    """
    return _best_email_in_with_reason(text)[0]


def _contact_email_with_reason(lead: Lead) -> tuple[str | None, str]:
    """Scan every text block of a lead: ``(address, reason)``.

    Description before ``raw``: the human-written body is where a poster puts the
    address they want used. Within a block, see
    :func:`_best_email_in_with_reason` for why "best" replaced "first".

    The first block that yields an address still wins outright — unchanged — so the
    only new behaviour is the reason. Across blocks the reasons FOLD rather than
    last-one-wins, because a lead's raw payload routinely contributes a dozen blocks of
    boilerplate after the description: taking the final block's verdict would report
    ``no_address_in_post`` for a post whose description clearly printed ``support@``,
    which is the wrong lever (buy a new source) for a lead we already reach.
    """
    haystacks: list[str] = [lead.description or ""]
    haystacks.extend(_iter_raw_strings(lead.raw or {}))

    reason = REASON_NO_ADDRESS_IN_POST
    for text in haystacks:
        email, why = _best_email_in_with_reason(text)
        if email:
            return email, REASON_FOUND
        reason = _preferred_reason(reason, why)
    return None, reason


def find_contact_email(lead: Lead) -> str | None:
    """Return the best valid contact email found in the lead, else ``None``.

    Thin wrapper over :func:`_contact_email_with_reason`; the signature and the return
    value are byte-for-byte what they were before the reason plumbing existed, which is
    what lets ~40 existing assertions across four test modules stand unchanged.
    """
    return _contact_email_with_reason(lead)[0]


# --- deliverability verification ------------------------------------------------
#
# Measured context: of 25 fully-researched, fully-drafted proposals, 18 died at
# the contact step ("no_email"). Emailing a domain that cannot receive mail burns
# sender reputation, which is the one asset a cold-outreach system cannot rebuy —
# so an address is only considered sendable if its domain publishes MX (or A)
# records. This is a DNS lookup, not an SMTP probe: no connection is made to the
# recipient's mail server, so it is invisible to them and costs nothing.

_MX_CACHE: dict[str, bool] = {}


def domain_accepts_mail(domain: str) -> bool:
    """True if ``domain`` publishes MX (or fallback A) records.

    Cached per-process. Fails **open** on resolver errors: a DNS hiccup should not
    silently discard a real lead, since a bad send merely bounces while a wrongly
    discarded lead is invisible.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return False
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]

    ok = True
    try:  # dnspython is optional — degrade to a plain A-record check without it.
        import dns.resolver  # type: ignore[import-untyped]

        try:
            answers = dns.resolver.resolve(domain, "MX")
            ok = bool(list(answers))
        except Exception:
            try:
                dns.resolver.resolve(domain, "A")
                ok = True
            except Exception:
                ok = False
    except ImportError:
        import socket

        try:
            socket.getaddrinfo(domain, None)
            ok = True
        except Exception:
            ok = False
    except Exception:  # pragma: no cover - resolver misconfiguration: fail open
        ok = True

    _MX_CACHE[domain] = ok
    return ok


def find_deliverable_email_with_reason(lead: Lead) -> tuple[str | None, str]:
    """``(address, reason)`` — the sendable address, or ``None`` and why not.

    The reason is one of :data:`REASON_FOUND` or the four members of
    :data:`CONTACT_REASONS`, and the strings are stable because they are report keys:
    they replace the single ``no_email_pregate`` counter that read 851 of 1047 leads
    over the 6 production runs of 2026-08-10..17 while naming no lever at all.

    Deliverability is decided here and nowhere else, and it is decided LAST, on the one
    address that already won ranking. Two consequences worth stating:

    * ``domain_refused_mail`` outranks every other reason (see :data:`CONTACT_REASONS`)
      but it can only be reached by an address that passed the whole gate, so the
      ordering is not a tie-break so much as a description of how far the lead got.
    * we do not fall back to the runner-up when the winner's domain is dead. That is
      the pre-existing behaviour of this function and it is preserved on purpose — the
      winner is the hiring address and a runner-up is by construction the generic or
      uncued one, so "send to the second-best address on a domain we could not verify"
      trades sender reputation, the one asset this system cannot rebuy, for a lead.
      The reason string is what makes the trade visible: if ``domain_refused_mail``
      turns out to be a large slice of the 851, that measurement is the argument for
      revisiting it, which is exactly what the split exists to enable.

    The DNS check is skipped when ``COPILOT_VERIFY_CONTACT_DOMAIN=false`` so that the
    test suite (and offline dev) stays hermetic — fixture domains like ``acme.io``
    don't resolve, and a unit test must not depend on a resolver. With verification off
    the reason is ``found``, never ``domain_refused_mail``: the escape hatch means "this
    machine cannot answer the deliverability question", and inventing a verdict for an
    unasked question is how a report ends up confidently wrong.
    """
    email, reason = _contact_email_with_reason(lead)
    if not email:
        return None, reason
    try:
        from config import get_settings

        if not get_settings().verify_contact_domain:
            return email, REASON_FOUND
    except Exception:  # pragma: no cover - config unavailable: stay strict
        pass
    _, _, domain = email.partition("@")
    if not domain_accepts_mail(domain):
        return None, REASON_DOMAIN_REFUSED_MAIL
    return email, REASON_FOUND


def find_deliverable_email(lead: Lead) -> str | None:
    """Like :func:`find_contact_email`, but also require a mail-accepting domain.

    Thin wrapper that discards the reason, so every existing caller (``pipeline`` calls
    this twice) keeps its exact pre-refactor behaviour. New callers that report on why
    a lead was unreachable should use :func:`find_deliverable_email_with_reason`.
    """
    return find_deliverable_email_with_reason(lead)[0]
