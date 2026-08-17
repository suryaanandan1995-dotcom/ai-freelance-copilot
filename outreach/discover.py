"""Find the contact address a company **published on its own site**.

Why this module exists, measured over the 6 production runs of 2026-08-10..17:

* 1047 new leads; **269** cleared the fit bar of 70 (max score 97); **196** carried
  an email address anywhere in the post — but only **7** were both, and only 6
  emails went out in 10 days. The qualified set and the contactable set are nearly
  disjoint.
* Qualified vs contactable, per source::

      contract_jobs (Adzuna)   79 qualified /   0 contactable
      jobicy                   76 /  14
      remote_boards            76 /   1
      working_nomads           17 /   0
      contra_startup           14 /   0
      hn_hiring                 7 / 181

  So **181 of the 196 addresses come from one source**, ``hn_hiring``, whose posts
  are full-time employment ads with a median fit of 28 — and the boards that carry
  the actual contract work publish no address at all, because they monetise the
  click and route applications through their own forms.

Contactability was therefore defined entirely as "did the post body happen to
contain an address" (:func:`outreach.extract.find_deliverable_email`). There was no
contact-*discovery* step anywhere in the codebase. This module is it: given a lead,
resolve the company's own domain and read the address the company itself publishes
on ``/contact``, ``/about``, ``/careers`` …

The rules, in the order they matter
-----------------------------------

**1. Never guess an address pattern. This is a rule, not a preference.** No
``careers@``, no ``info@``, no ``firstname.lastname@``, no address constructed from
a domain for any reason. Only an address that a fetched page actually published is
ever returned. A guessed mailbox is a guaranteed hard bounce, hard bounces burn the
sending domain, and the sending domain is the one asset cold outreach cannot rebuy.
:data:`outreach.extract._PLACEHOLDER_LOCALS` exists because a *human* published
``first.last@grafana.com`` and we nearly sent to it; inventing the same string
ourselves would be the same defect with less excuse.

**2. One gate, not two.** The fetched page is wrapped in a synthetic
:class:`~core.schemas.Lead` and handed to :func:`outreach.extract.find_contact_email`,
then MX-verified with :func:`outreach.extract.domain_accepts_mail`. No regex, no
local-part list and no prose classifier is copied out of ``extract.py``. That module
took several rounds of live measurement to calibrate (accommodations desks, fraud
inboxes, ``recruiting@`` published as a disability-adjustment address); a second
copy here would drift out of calibration silently and re-introduce exactly those
sends. If the gate rejects everything on a page, the answer is "no contact".

**3. The address must be on the company's domain.** Registrable-suffix match, so
``hello@mail.acme.com`` counts for ``acme.com`` and ``www.`` never matters. Without
this check "discovery" quickly means discovering the job board's own address, an ATS
notification address, or a random partner in the page footer.

**4. Aggregators are not companies** — see :data:`BLOCKED_HOSTS`.

Politeness, because this is someone else's server
-------------------------------------------------
``robots.txt`` is honoured (fetched once per domain, cached in-process; a fetch
failure means proceed, an explicit ``Disallow: /`` aborts the domain), the
User-Agent names the tool and the owner's site, bodies are read to a hard
:data:`MAX_BODY_BYTES` cap so one huge page cannot stall a run, at most
``get_settings().max_pages_per_company`` paths are tried per company and the crawl
stops at the first accepted address. Nothing here raises: every network or parse
failure returns ``None`` and logs, following the ``sources/*.py`` convention, because
an exception in a nice-to-have enrichment step must not be able to end a run.

Worst-case request count for a single discovery
-----------------------------------------------
* Post is on the company's own site (``lead.url`` usable): **1 + budget** requests —
  one ``robots.txt``, then up to ``max_pages_per_company`` (default 4) pages. 5 total.
* Domain derived from ``lead.company``: each of the 4 candidate TLDs costs a
  ``robots.txt`` + a homepage (8), then the accepted domain's budgeted paths (4).
  **12 total**, and the verified homepage is re-read from memory rather than
  re-fetched.

A second lead for the same company costs **0** requests (see :data:`_CONTACT_CACHE`).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from config import get_settings
from core.schemas import Lead
from outreach.extract import domain_accepts_mail, find_contact_email
from sources._text import strip_html

logger = logging.getLogger(__name__)

#: Named so an administrator reading their access log can tell what we are and stop
#: us without guessing. The site is the owner's, and it is the same URL the outreach
#: emails cite as proof of work.
USER_AGENT = (
    "ai-freelance-copilot/1.0 (contact discovery; "
    "+https://suryaanandan1995-dotcom.github.io)"
)

#: Tried in this order; the first accepted address wins. ``/`` is last because a
#: homepage is the least likely place for a published address and the most likely to
#: be a heavy JS shell — but it is free when domain verification already fetched it.
CONTACT_PATHS: tuple[str, ...] = (
    "/contact",
    "/contact-us",
    "/about",
    "/careers",
    "/jobs",
    "/about-us",
    "/team",
    "/",
)

#: Hard cap on bytes read per response (512 KB). One 40 MB "page" on a slow host
#: would otherwise hold a whole pipeline run hostage; no contact address in the wild
#: sits past the first half-megabyte of markup.
MAX_BODY_BYTES = 512 * 1024

#: TLDs tried when the domain has to be derived from the company name. Ordered by
#: how often a real company answers there. Deliberately short: each entry costs two
#: requests on a host that may not be ours to poke.
_DERIVED_TLDS: tuple[str, ...] = (".com", ".io", ".ai", ".co")


# --- hosts that are never the company ------------------------------------------------
#
# Treating any of these as "the company domain" would make us discover *their* address
# and cold-pitch a job board, an ATS vendor, or a stranger who happens to share a
# shared-hosting suffix. Grouped by what each group is:
BLOCKED_HOSTS: frozenset[str] = frozenset(
    {
        # Applicant tracking systems. A listing lives at boards.greenhouse.io/acme,
        # so the host belongs to the ATS vendor and the address on it is theirs.
        "greenhouse.io",
        "lever.co",
        "workable.com",
        "ashbyhq.com",
        "smartrecruiters.com",
        "bamboohr.com",
        "jobvite.com",
        "myworkdayjobs.com",
        "workday.com",
        "breezy.hr",
        "recruitee.com",
        "teamtailor.com",
        "jazzhr.com",
        "applytojob.com",
        "icims.com",
        "taleo.net",
        # Job boards and aggregators — the sources this pipeline reads. Their whole
        # business is owning the click, so their address is a sales/support desk.
        "linkedin.com",
        "indeed.com",
        "adzuna.com",
        "adzuna.co.uk",
        "jobicy.com",
        "weworkremotely.com",
        "remoteok.com",
        "remoteok.io",
        "remotive.com",
        "workingnomads.com",
        "workingnomads.co",
        "nodesk.co",
        "contra.com",
        "glassdoor.com",
        "glassdoor.co.uk",
        "ziprecruiter.com",
        "monster.com",
        "dice.com",
        "wellfound.com",
        "angel.co",
        "upwork.com",
        "fiverr.com",
        "freelancer.com",
        # Shared publishing/hosting suffixes: the registrable domain belongs to the
        # platform, so a "domain match" against it would match every other tenant.
        "github.io",
        "github.com",
        "gitlab.io",
        "notion.site",
        "webflow.io",
        "wixsite.com",
        "netlify.app",
        "vercel.app",
        "herokuapp.com",
        "wordpress.com",
        "blogspot.com",
        "medium.com",
        "substack.com",
        "docs.google.com",
        # Social / general-purpose platforms.
        "facebook.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "youtube.com",
        "reddit.com",
        "news.ycombinator.com",
        "google.com",
        # Free mailbox providers. A gmail.com address is never "the company domain",
        # and a company domain is never one of these.
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "ymail.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "icloud.com",
        "me.com",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "mail.ru",
    }
)

#: Two-label public suffixes, so ``acme.co.uk`` is one company and ``other.co.uk`` is
#: another. A curated subset rather than the full Public Suffix List, because the PSL
#: is a dependency and this repo's rule is that a new dependency needs a reason. The
#: cost of a miss is a *rejected* address (we decline to email), never a wrong send.
_MULTI_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
        "com.au", "net.au", "org.au", "co.nz", "co.za", "com.br", "com.mx",
        "co.jp", "or.jp", "co.in", "com.sg", "com.hk", "co.kr", "com.tr",
        "co.il", "com.cn", "com.ar", "com.co", "co.id", "com.my", "com.ph",
    }
)

#: Legal suffixes stripped before a company name becomes a domain guess. Repeatedly,
#: so "Acme Robotics Pty Ltd" reduces to "acmerobotics".
_LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "inc", "inc.", "incorporated", "ltd", "ltd.", "limited", "llc", "l.l.c",
        "llp", "gmbh", "ug", "bv", "b.v", "nv", "pty", "sa", "s.a", "ab", "oy",
        "oyj", "as", "a.s", "aps", "corp", "corp.", "corporation", "co", "co.",
        "company", "plc", "srl", "s.r.l", "spa", "kg", "ag", "kk", "pte",
    }
)

#: Domain-parking / for-sale / placeholder markers. A parking page for ``acme.com``
#: usually *contains* the string "acme", so "does the page mention the company" is not
#: enough on its own — a wrong domain means emailing a stranger, which is strictly
#: worse than emailing nobody.
_PARKING_MARKERS: tuple[str, ...] = (
    "domain is for sale",
    "domain for sale",
    "buy this domain",
    "this domain may be for sale",
    "the domain name",
    "parked domain",
    "domain parking",
    "is parked",
    "sedo.com",
    "godaddy.com/domainsearch",
    "hugedomains",
    "afternic",
    "dan.com",
    "under construction",
    "coming soon",
    "site not published",
    "default web page",
    "index of /",
)

# --- HTML → text -------------------------------------------------------------------
#
# No new dependency: drop <script>/<style> blocks, surface mailto: targets, then reuse
# sources._text.strip_html (tags first, entities second — the order is load-bearing
# there and documented in that module).
_SCRIPT_RE = re.compile(r"(?is)<(script|style|template|noscript|svg)\b.*?</\1\s*>")

#: ``mailto:`` hrefs are the single richest signal on a contact page — very often the
#: address appears **only** inside the attribute, with visible text reading "Email us"
#: — so it must survive stripping. Surfacing it *inside* the tag would be useless: the
#: tag stripper deletes everything up to the next ``>``, taking the address with it.
#: So the whole tag is replaced by the bare address, in place, which also preserves the
#: surrounding prose that ``extract``'s hiring-cue and do-not-contact windows read.
_MAILTO_TAG_RE = re.compile(r"(?is)<[a-z][^>]*?mailto:([^\"'>?\s]+)[^>]*>")
_MAILTO_BARE_RE = re.compile(r"(?i)mailto:([^\"'>?\s]+)")
_WS_RE = re.compile(r"\s+")
_ALNUM_STRIP_RE = re.compile(r"[^a-z0-9]+")
_TEXTY_TYPES = ("text/", "application/xhtml", "application/xml", "application/json")

#: How many times the gate is re-asked after rejecting an off-domain address. See
#: :func:`_accept`: a page can publish the company's address *and* a partner's, and
#: ``find_contact_email`` returns exactly one winner.
_MAX_GATE_ROUNDS = 3


@dataclass(frozen=True)
class DiscoveredContact:
    """An address a company published, and the page it was published on."""

    email: str
    #: The company domain the address was found on (what the match was made against).
    domain: str
    #: The exact page URL, after redirects, so the owner can audit any address before
    #: it is used. An address with no auditable source is not worth having.
    source_url: str


@dataclass(frozen=True)
class _Page:
    url: str
    text: str


@dataclass(frozen=True)
class _Robots:
    """The parsed subset of ``robots.txt`` this crawler honours."""

    #: ``Disallow: /`` for our user-agent group: the domain is off-limits entirely.
    #: Deliberately unconditional — a ``Disallow: /`` with an ``Allow:`` carve-out
    #: still aborts. Such carve-outs are written for search engines (a sitemap, a
    #: canonical landing page), and we are not one; reading the invitation as being
    #: addressed to us is exactly the liberty a stranger's ``robots.txt`` is there to
    #: withhold. Narrower ``Disallow`` prefixes DO honour ``Allow`` (longest match).
    abort: bool = False
    #: ``(field, value)`` pairs from the group that applies to us, in file order.
    rules: tuple[tuple[str, str], ...] = ()

    def disallowed(self, path: str) -> bool:
        return _robots_blocks(self.rules, path)


# --- in-process caches ---------------------------------------------------------------
#
# Two leads from the same company must cost one crawl per run: 175 leads a run against
# a long tail of repeat employers, and every avoided request is a request we don't make
# on somebody else's server.
_ROBOTS_CACHE: dict[str, _Robots] = {}
#: company-name slug -> resolved domain (or None). Negative results are cached too, so
#: a company whose domain cannot be found is probed once, not once per lead.
_DOMAIN_CACHE: dict[str, str | None] = {}
#: domain -> result. ``None`` is cached: "this company publishes no usable address" is
#: an answer, and re-crawling to re-derive it is the rude version of asking twice.
_CONTACT_CACHE: dict[str, DiscoveredContact | None] = {}


def clear_caches() -> None:
    """Drop every in-process cache. For tests, and for a long-lived process."""
    _ROBOTS_CACHE.clear()
    _DOMAIN_CACHE.clear()
    _CONTACT_CACHE.clear()


# --- host helpers --------------------------------------------------------------------


def _normalise_host(host: str | None) -> str:
    """Lowercase, no port, no trailing dot, no ``www.``."""
    out = (host or "").strip().lower().rstrip(".")
    if "@" in out:  # userinfo in a URL
        out = out.rsplit("@", 1)[1]
    out = out.split(":", 1)[0]
    if out.startswith("www."):
        out = out[4:]
    return out


def _host_of(url: str | None) -> str:
    try:
        return _normalise_host(urlsplit(url or "").hostname or "")
    except Exception:  # pragma: no cover - urlsplit is tolerant, but never raise
        return ""


def _looks_like_domain(host: str) -> bool:
    """A routable-looking name, not an IP, ``localhost`` or a bare label."""
    if not host or "." not in host or host.endswith("."):
        return False
    labels = host.split(".")
    if any(not label for label in labels):
        return False
    if all(label.isdigit() for label in labels):  # an IP literal
        return False
    tld = labels[-1]
    return len(tld) >= 2 and tld.isalpha()


def _registrable(host: str) -> str:
    """``mail.acme.co.uk`` -> ``acme.co.uk``; ``jobs.acme.com`` -> ``acme.com``."""
    labels = _normalise_host(host).split(".")
    if len(labels) < 3:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _is_blocked_host(host: str) -> bool:
    """True if ``host`` is (or sits under) a host that is never a company's own."""
    h = _normalise_host(host)
    if not h:
        return True
    for blocked in BLOCKED_HOSTS:
        if h == blocked or h.endswith("." + blocked):
            return True
    return False


def _same_company(address_domain: str, company_domain: str) -> bool:
    """True if an address's domain belongs to the company we resolved.

    Registrable-suffix match, so ``mail.acme.com`` and ``www.acme.com`` both count for
    ``acme.com`` while ``partner.io`` never does. This is requirement 3 and it is the
    check that stops "discovery" from returning the job board's own address.
    """
    a, c = _normalise_host(address_domain), _normalise_host(company_domain)
    if not a or not c:
        return False
    if a == c or a.endswith("." + c) or c.endswith("." + a):
        return True
    return _registrable(a) == _registrable(c)


# --- company name -> domain candidate ------------------------------------------------


def _company_slug(company: str | None) -> str:
    """``"Acme Robotics Pty Ltd"`` -> ``"acmerobotics"``, or ``""`` if unusable."""
    name = (company or "").strip().lower()
    if not name:
        return ""
    # A name that is already a hostname ("acme.com") keeps its label, not its TLD.
    if _looks_like_domain(name) and " " not in name:
        name = _normalise_host(name).rsplit(".", 1)[0].replace(".", " ")
    tokens = [t for t in re.split(r"[^a-z0-9&+]+", name) if t]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    slug = _ALNUM_STRIP_RE.sub("", "".join(tokens))
    # Two characters is not a company name, it is a coin flip on somebody's domain.
    return slug if len(slug) >= 3 else ""


def _mentions_company(text: str, slug: str, company: str | None) -> bool:
    """True if a homepage plausibly belongs to the company we derived it from.

    Guards against parking pages and squatters. Checked in this order because a
    parking page for ``acme.com`` normally *does* contain "acme": the parking markers
    veto first, then the name has to appear.

    The name has to appear somewhere **other than the domain itself**. Measured while
    writing the tests: a squatter page reading "Bob's Plumbing Supplies …
    bob@acmerobotics.com" passed, because the address contains the domain contains the
    slug — the check was verifying that a page mentions its own URL, which every page
    does. Occurrences of ``<slug>.<tld>`` are therefore removed before matching. The
    conservative cost, stated: a real company that never writes its own name in prose,
    only its domain, is rejected. That is the right side to fail on — a wrong domain
    means cold-emailing a stranger, which is worse than emailing nobody.
    """
    lowered = text.lower()
    if any(marker in lowered for marker in _PARKING_MARKERS):
        return False
    if slug:
        lowered = re.sub(rf"{re.escape(slug)}\.[a-z]{{2,}}(?:\.[a-z]{{2,}})?", " ", lowered)
    compressed = _ALNUM_STRIP_RE.sub("", lowered)
    if not compressed:
        return False
    if slug and slug in compressed:
        return True
    # Fallback for names the slug mangles ("Acme & Sons" -> "acmesons"): every
    # substantial word of the name has to be on the page.
    words = [w for w in re.split(r"[^a-z0-9]+", (company or "").lower()) if len(w) >= 4]
    return bool(words) and all(w in compressed for w in words)


# --- HTML → text ---------------------------------------------------------------------


def _html_to_text(raw: str) -> str:
    """Plain text with ``mailto:`` targets surfaced, whitespace collapsed."""
    text = _SCRIPT_RE.sub(" ", raw)
    text = _MAILTO_TAG_RE.sub(r" \1 ", text)
    text = _MAILTO_BARE_RE.sub(r" \1 ", text)
    text = strip_html(text)  # tags, then entities — see sources._text.strip_html
    # Collapsed because extract's cue/block windows are measured in characters, and a
    # 400-newline gap between "email us at" and the address would push one out of the
    # other's window purely because of markup indentation.
    return _WS_RE.sub(" ", text).strip()


# --- fetching ------------------------------------------------------------------------


def _get(client: httpx.Client, url: str, timeout: float) -> tuple[str, str] | None:
    """``(final_url, body_text)`` for a 200 text response, else ``None``. Never raises."""
    try:
        with client.stream(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"},
            timeout=timeout,
            follow_redirects=True,
        ) as resp:
            if resp.status_code != 200:
                logger.debug("discover: %s returned %s", url, resp.status_code)
                return None
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype and not any(t in ctype for t in _TEXTY_TYPES):
                logger.debug("discover: %s is %s, not text", url, ctype)
                return None
            final_url = str(resp.url)
            encoding = resp.charset_encoding or "utf-8"
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_BODY_BYTES:
                    logger.debug("discover: %s truncated at %d bytes", url, MAX_BODY_BYTES)
                    break
        body = b"".join(chunks)[:MAX_BODY_BYTES]
        try:
            return final_url, body.decode(encoding, errors="replace")
        except LookupError:  # a charset label we don't have a codec for
            return final_url, body.decode("utf-8", errors="replace")
    except Exception as exc:  # network, TLS, redirect loop, decode — all the same here
        logger.debug("discover: fetch failed for %s: %s: %s", url, type(exc).__name__, exc)
        return None


def _fetch_page(client: httpx.Client, url: str, timeout: float) -> _Page | None:
    got = _get(client, url, timeout)
    if got is None:
        return None
    final_url, raw = got
    # follow_redirects can land us on an aggregator (a dead careers page redirected to
    # a LinkedIn company profile is common). An address read there is not the company's.
    if _is_blocked_host(_host_of(final_url)):
        logger.debug("discover: %s redirected to a non-company host %s", url, final_url)
        return None
    try:
        return _Page(url=final_url, text=_html_to_text(raw))
    except Exception as exc:  # pragma: no cover - regex/decode defence
        logger.warning("discover: could not parse %s: %s", url, exc)
        return None


# --- robots.txt ----------------------------------------------------------------------


def _parse_robots(text: str) -> tuple[tuple[str, str], ...]:
    """The ``(field, value)`` rules of the group that applies to us.

    Consecutive ``User-agent`` lines form one group. Our own product token wins over
    ``*`` when the file names us; otherwise ``*`` applies. Groups aimed at other
    crawlers are ignored, which is the whole point of the field.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    agents: list[str] = []
    in_rules = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if in_rules:  # a new group starts after a rule line
                agents = []
                in_rules = False
            agents.append(value.lower())
        elif field in ("disallow", "allow"):
            in_rules = True
            for agent in agents or ["*"]:
                groups.setdefault(agent, []).append((field, value))
    for agent in ("ai-freelance-copilot", "*"):
        if agent in groups:
            return tuple(groups[agent])
    return ()


def _rule_matches(pattern: str, path: str) -> int:
    """Length of ``pattern`` if it covers ``path``, else -1.

    A deliberately conservative subset of the de-facto standard: prefix match, with
    ``*`` honoured only as "match up to here" and ``$`` ignored. Over-matching means we
    skip a page we were allowed to read (costs one lead); under-matching means we fetch
    a page we were asked not to (costs the owner's standing with a stranger).
    """
    prefix = pattern.split("*", 1)[0]
    if not prefix:
        return 0 if "*" in pattern else -1
    return len(prefix) if path.startswith(prefix) else -1


def _robots_blocks(rules: tuple[tuple[str, str], ...], path: str) -> bool:
    """Longest-match wins between ``Allow`` and ``Disallow``, as crawlers agree."""
    best_disallow = best_allow = -1
    for field, value in rules:
        if not value:  # "Disallow:" with an empty value means allow everything
            continue
        length = _rule_matches(value, path)
        if length < 0:
            continue
        if field == "disallow":
            best_disallow = max(best_disallow, length)
        else:
            best_allow = max(best_allow, length)
    return best_disallow > best_allow


def _robots_for(domain: str, client: httpx.Client, timeout: float) -> _Robots:
    """Fetch and parse ``robots.txt`` once per domain.

    A fetch failure (404, 500, timeout, TLS) means **proceed**: the overwhelming
    majority of small company sites have no ``robots.txt`` at all, and treating a
    missing file as a prohibition would switch discovery off for most of the market.
    An explicit ``Disallow: /`` for us means abort the domain.
    """
    if domain in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[domain]
    robots = _Robots()
    got = _get(client, f"https://{domain}/robots.txt", timeout)
    if got is not None:
        try:
            rules = _parse_robots(got[1][:MAX_BODY_BYTES])
            robots = _Robots(abort=_robots_blocks(rules, "/"), rules=rules)
        except Exception as exc:  # pragma: no cover - parse defence
            logger.warning("discover: unreadable robots.txt for %s: %s", domain, exc)
            robots = _Robots()
    _ROBOTS_CACHE[domain] = robots
    return robots


# --- the gate (reused, never reimplemented) ------------------------------------------


def _synthetic_lead(lead: Lead, page_url: str, text: str) -> Lead:
    """Wrap a fetched page as a Lead so ``extract``'s gate can judge it unchanged.

    ``raw`` stays empty on purpose: the gate searches ``description`` then ``raw``, and
    the only thing under judgement here is the page we fetched.
    """
    return Lead(
        source="discover",
        external_id=page_url,
        title=lead.title,
        description=text,
        url=page_url,
    )


def _redact(text: str, email: str) -> str:
    """Blank one literal address so the gate can be asked for its next-best pick."""
    return re.sub(re.escape(email), " ", text, flags=re.IGNORECASE)


def _accept(page: _Page, domain: str, lead: Lead) -> DiscoveredContact | None:
    """The gate's verdict on one page, restricted to the company's own domain.

    ``find_contact_email`` returns a single winner, so a page that publishes both
    ``hello@acme.com`` and a partner's ``sales@vendor.io`` could hand back the partner
    and lose the real address. Rather than re-implement the ranking (requirement 2),
    the rejected literal is blanked and the *same* gate is asked again, at most
    :data:`_MAX_GATE_ROUNDS` times.
    """
    text = page.text
    for _ in range(_MAX_GATE_ROUNDS):
        email = find_contact_email(_synthetic_lead(lead, page.url, text))
        if not email:
            return None
        address_domain = email.partition("@")[2]
        if not _same_company(address_domain, domain):
            logger.debug("discover: %s is not on %s, ignoring", email, domain)
            text = _redact(text, email)
            continue
        if not domain_accepts_mail(address_domain):
            # A discovered address has never been read by a human, so the MX check is
            # not optional here the way it is for a hand-posted one.
            logger.debug("discover: %s publishes no MX", address_domain)
            return None
        return DiscoveredContact(email=email, domain=domain, source_url=page.url)
    return None


# --- resolution + crawl --------------------------------------------------------------


def _verify_candidate(
    candidate: str, slug: str, company: str | None, client: httpx.Client, timeout: float
) -> _Page | None:
    """The candidate's homepage, if it answers 200 and looks like the company's.

    ``robots.txt`` is read before the homepage, not after: politeness is not something
    to check once we have already taken what we came for.
    """
    robots = _robots_for(candidate, client, timeout)
    if robots.abort or robots.disallowed("/"):
        logger.debug("discover: robots.txt forbids %s/", candidate)
        return None
    page = _fetch_page(client, f"https://{candidate}/", timeout)
    if page is None:
        return None
    if not _mentions_company(page.text, slug, company):
        logger.debug("discover: %s never mentions %r, not the company", candidate, company)
        return None
    return page


def _resolve_domain(
    lead: Lead, client: httpx.Client, timeout: float
) -> tuple[str | None, _Page | None]:
    """The company's domain, and its homepage if verification already fetched it.

    Order, and why:

    (a) ``lead.url``'s host, when it is not blocklisted — the post is on the company's
        own site, which is the strongest evidence available and costs no request;
    (b) otherwise derive from ``lead.company``: strip legal suffixes and punctuation,
        then try ``.com``, ``.io``, ``.ai``, ``.co``, accepting a candidate only if its
        homepage answers 200 **and** plausibly mentions the company. A derived domain
        that is never verified is how you email a stranger, which is worse than not
        emailing at all.
    """
    host = _host_of(lead.url)
    if _looks_like_domain(host):
        if not _is_blocked_host(host):
            return host, None
        logger.debug("discover: %s is an aggregator, deriving from the company name", host)

    slug = _company_slug(lead.company)
    if not slug:
        return None, None
    key = f"name:{slug}"
    if key in _DOMAIN_CACHE:
        return _DOMAIN_CACHE[key], None

    for tld in _DERIVED_TLDS:
        candidate = f"{slug}{tld}"
        if _is_blocked_host(candidate):
            continue
        page = _verify_candidate(candidate, slug, lead.company, client, timeout)
        if page is not None:
            _DOMAIN_CACHE[key] = candidate
            return candidate, page
    _DOMAIN_CACHE[key] = None
    return None, None


def _crawl(
    domain: str,
    lead: Lead,
    client: httpx.Client,
    timeout: float,
    budget: int,
    homepage: _Page | None,
) -> DiscoveredContact | None:
    """Try up to ``budget`` contact paths on ``domain``, stopping at the first address."""
    robots = _robots_for(domain, client, timeout)
    if robots.abort:
        logger.debug("discover: robots.txt disallows all of %s", domain)
        return None

    # Already in hand from domain verification, so it costs nothing and does not spend
    # the budget. A footer mailto on the homepage is a common, perfectly auditable hit.
    if homepage is not None:
        found = _accept(homepage, domain, lead)
        if found is not None:
            return found

    tried = 0
    for path in CONTACT_PATHS:
        if tried >= budget:
            break
        if homepage is not None and path == "/":
            continue
        if robots.disallowed(path):
            # Skipping costs no request, so it does not spend the budget either.
            logger.debug("discover: robots.txt disallows %s%s", domain, path)
            continue
        tried += 1
        page = _fetch_page(client, f"https://{domain}{path}", timeout)
        if page is None:
            continue
        found = _accept(page, domain, lead)
        if found is not None:
            return found
    return None


def _discover(lead: Lead, client: httpx.Client) -> DiscoveredContact | None:
    settings = get_settings()
    if not getattr(settings, "discover_contacts", True):
        return None
    timeout = float(getattr(settings, "discover_timeout_seconds", 8.0) or 8.0)
    budget = max(1, int(getattr(settings, "max_pages_per_company", 4) or 4))

    domain, homepage = _resolve_domain(lead, client, timeout)
    if not domain:
        logger.debug("discover: no company domain for %r / %r", lead.company, lead.url)
        return None
    if domain in _CONTACT_CACHE:
        return _CONTACT_CACHE[domain]

    # DNS before HTTP: if the company domain publishes no MX at all, no address on it
    # can be sent to, and crawling up to five pages of a stranger's site to learn that
    # is rude as well as pointless. Forfeited edge case, stated: a company whose mail
    # lives only on a subdomain that publishes MX while the apex does not.
    if not domain_accepts_mail(domain):
        logger.debug("discover: %s publishes no MX, not crawling", domain)
        _CONTACT_CACHE[domain] = None
        return None

    result = _crawl(domain, lead, client, timeout, budget, homepage)
    _CONTACT_CACHE[domain] = result
    return result


def discover_contact(
    lead: Lead, *, client: httpx.Client | None = None
) -> DiscoveredContact | None:
    """The contact address ``lead``'s company publishes on its own site, or ``None``.

    Never raises and never guesses: a returned address was read verbatim off the page
    named by :attr:`DiscoveredContact.source_url`, passed
    :func:`outreach.extract.find_contact_email`, sits on the company's own domain, and
    has MX records. ``None`` means "no auditable address", which is a perfectly good
    answer — the alternative is a bounce, and bounces are charged to the sending domain.

    ``client`` is injectable so tests can supply an ``httpx.MockTransport`` and so a
    caller can share one connection pool across a run's leads.
    """
    try:
        if client is not None:
            return _discover(lead, client)
        timeout = float(getattr(get_settings(), "discover_timeout_seconds", 8.0) or 8.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as owned:
            return _discover(lead, owned)
    except Exception as exc:
        # Requirement 7: an enrichment step must not be able to end a pipeline run.
        logger.warning(
            "discover: giving up on %r: %s: %s", lead.dedupe_key, type(exc).__name__, exc
        )
        return None


__all__ = [
    "BLOCKED_HOSTS",
    "CONTACT_PATHS",
    "MAX_BODY_BYTES",
    "USER_AGENT",
    "DiscoveredContact",
    "clear_caches",
    "discover_contact",
]
