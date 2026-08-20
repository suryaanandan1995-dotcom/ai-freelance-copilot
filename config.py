"""Central configuration (pydantic-settings).

Defaults are SAFE and OFFLINE: SQLite DB, dry-run on, sending disabled. Nothing
is ever submitted to a platform automatically — `allow_send` exists only to let
the approval dashboard mark items as sent by a human.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

#: SMTP relays that only SEND. They expose no IMAP server, so an IMAP host cannot be
#: derived from them — replies to mail sent through a relay arrive in the mailbox that
#: owns the From address, which only the operator knows. See
#: :meth:`Settings.resolved_imap_host`.
_SEND_ONLY_SMTP_HOSTS = frozenset(
    {
        "smtp.sendgrid.net",
        "smtp.mailgun.org",
        "smtp.eu.mailgun.org",
        "smtp-relay.brevo.com",
        "smtp.brevo.com",
        "smtp.sendinblue.com",
        "smtp.resend.com",
        "smtp.postmarkapp.com",
        "email-smtp.us-east-1.amazonaws.com",
        "smtp.mailersend.net",
        "smtp.sparkpostmail.com",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COPILOT_", env_file=".env", extra="ignore")

    # --- storage ---
    database_url: str = "sqlite:///copilot.db"

    # --- Claude API ---
    anthropic_api_key: str = ""
    model_opus: str = "claude-opus-4-8"      # drafting / hard reasoning
    model_sonnet: str = "claude-sonnet-4-6"  # cheap scoring / triage

    # --- notifications (Telegram is blocked in India -> email primary, WhatsApp optional) ---
    notify_channel: str = "email"  # "email" | "whatsapp" | "none"
    dashboard_base_url: str = "http://localhost:8000"  # used for links in digests

    # email / SMTP (primary)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""           # defaults to owner_email if empty
    notify_email_to: str = ""     # defaults to owner_email if empty

    # WhatsApp Business Cloud API (optional)
    whatsapp_token: str = ""
    whatsapp_phone_id: str = ""   # WhatsApp Business phone-number ID
    whatsapp_to: str = ""         # recipient in international format, e.g. 9190XXXXXXXX

    # --- lead sources ---
    # Adzuna (free tier, https://developer.adzuna.com) backs the contract_jobs source,
    # the primary UK day-rate contract feed. Declared here rather than read straight
    # from os.environ: pydantic-settings loads .env into THIS object and never into
    # os.environ, so a source reading os.environ.get("COPILOT_ADZUNA_APP_ID") sees
    # nothing when the key is set in .env — and then reports itself "DISABLED", which
    # reads as "you never configured it" rather than "your config is being ignored".
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""

    # Comma-separated RSS feed overrides for two sources, same reason as above: these
    # were read from os.environ, so a value set in .env did nothing. It matters most for
    # upwork_feeds, which has NO built-in default — an ignored override leaves that
    # source with zero feeds, i.e. silently switched off. Blank means "use the module
    # defaults" for startup_feeds, and "no feeds" for upwork_feeds.
    startup_feeds: str = ""
    upwork_feeds: str = ""

    # --- RAG ---
    portfolio_repos_path: str = ".."  # where the user's repos live (for KB ingest)
    rag_store_path: str = "data/portfolio_kb.json"

    # --- pipeline policy ---
    min_fit_score: int = 70          # leads below this are dropped
    # Sized from measurement, not guessed. Run 31033943812 fetched 186 leads across
    # seven sources and considered only 50 of them — the cap, not cost, was the binding
    # constraint: that run spent $0.13 against a $2.00 ceiling, 15x headroom. The
    # newly-multi-region contract source got 9 of the 50 slots despite being the
    # highest-quality feed.
    #
    # CORRECTION (run 31172835060, 2026-08-07): this comment used to add that an
    # uncontactable lead "costs a regex and a cached DNS lookup, not an Opus call", so a
    # higher cap bought reach rather than spend. That stopped being true when the
    # pre-gate `continue` was removed and uncontactable leads started being *scored*
    # (deliberately — see require_contact_before_draft). 97 of that run's 122 scored
    # leads were suppressed after qualification, per-lead cost went $0.0026 -> $0.016428,
    # and the run hit its $2.00 ceiling at lead ~123 of 200. A cap of 200 is only real
    # if the spend ceiling can fund 200 leads; see max_usd_per_run, now $5.00, and the
    # relationship lint in tests/test_thresholds.py.
    max_leads_per_run: int = 200
    # Skip research+drafting for leads with no deliverable contact when the goal is
    # auto-email. 18 of 25 drafted proposals were thrown away at the contact step
    # after being paid for at Opus prices; checking first costs a regex + a cached
    # DNS lookup. Set False to keep drafting everything for human submission.
    require_contact_before_draft: bool = True
    # Require the contact domain to publish MX/A records before sending. Protects
    # sender reputation (the one asset cold outreach cannot rebuy). Disable only
    # for offline tests/dev, where fixture domains like "acme.io" don't resolve.
    verify_contact_domain: bool = True

    # --- contact discovery (see outreach/discover.py) ---
    # Measured over the 6 production runs of 2026-08-10..17: 269 leads cleared the fit
    # bar and 196 carried an email, but only 7 were BOTH. The two sets are nearly
    # disjoint — 181 of the 196 addresses came from hn_hiring (median fit 28, full-time
    # employment posts) while 231 of the 269 qualified leads came from job boards that
    # publish no address at all, because boards monetise the click. Contactability was
    # entirely "did the post body happen to contain an address", so the good half of the
    # funnel had no route. Discovery visits the *company's own site* and reads the
    # address it publishes.
    discover_contacts: bool = True
    # Only ever for leads that already cleared the bar: discovery is HTTP, not LLM, but
    # it is still someone else's server. A qualified-only trigger also means the fetch
    # count tracks the useful half of the funnel (~40/run) and not the 175 raw leads.
    max_contact_discoveries_per_run: int = 40
    # Pages tried per company, in `outreach.discover.CONTACT_PATHS` order. 4 covers
    # /contact, /contact-us, /about, /careers on every site tested; more is crawling.
    max_pages_per_company: int = 4
    discover_timeout_seconds: float = 8.0
    # Whether a GUESSED domain may be emailed, as opposed to only proposed to the owner.
    #
    # Discovery has two paths. When the post lives on the company's own site, the domain
    # is evidence and mail goes to the site that published the listing. When the post
    # lives on a job board (which is the whole 262-lead population this exists for), the
    # domain is *derived from the company name* — "Acme Corp" -> acme.com -> .io -> .ai —
    # and accepted if the homepage answers and mentions the company.
    #
    # That second path was measured the first time it ever executed, by accident, from a
    # unit test: it resolved a fixture's "Acme Corp" to the real acme.com, read the page,
    # and returned frobozz07@mail.acme.com. A stranger, on the first try. Generic company
    # names ("Apex", "Nova", "Summit") all have a .com owner who is not the client, and
    # every one of them looks exactly like a hit to the mention check.
    #
    # Cold-emailing the wrong company is the one failure in this project that fixing the
    # code afterwards cannot undo — a spam complaint burns the sending domain, and it
    # takes the follow-up sequence and every already-contacted prospect with it. So the
    # guessed half is surfaced in the digest for the owner to glance at, and not sent, and
    # THAT is what produces the evidence to turn this on: a few weeks of proposed
    # addresses a human can check for free beats an argument about the heuristic.
    discover_send_to_guessed_domains: bool = False

    # --- apply-yourself packs (see outreach/apply_pack.py) ---
    # The 262 qualified-but-unreachable leads of that same window already reach the
    # owner as a list of links (PR #19). A link is not a hand-off: applying still means
    # re-reading the post and writing the pitch from scratch, 262 times. A pack turns
    # each into a paste-and-submit. Capped because these are Opus drafts against a
    # $5.00/run ceiling: 5 packs is ~$0.08 and covers the top of a sorted list.
    apply_packs: bool = True
    max_apply_packs_per_run: int = 5

    # --- Reddit OAuth (see sources/reddit_forhire.py) ---
    # r/forhire "[Hiring]" posts are the one high-volume source where a *client* posts
    # *contract* work *with* a contact address — precisely the overlap the funnel lacks.
    # The adapter has been disabled since 2026-08-03 because unauthenticated .json 403s
    # from datacenter IPs (48/48 fetches). App-only OAuth fixes that and is free. The
    # source auto-enables when both values are set; absent them it stays off rather
    # than burning a fetch per run to be refused.
    reddit_client_id: str = ""
    reddit_client_secret: str = ""

    # --- run-health alerting (checks in monitor/funnel.py, run by monitor/doctor.py) ---
    # Every one of 24 production runs reported "success" while sending nothing,
    # because "sent 0 emails" was never an error condition. These thresholds turn
    # a silently idle funnel into a failed run that emails the owner.
    alert_after_zero_email_runs: int = 3   # consecutive runs with emailed == 0
    alert_after_zero_queue_runs: int = 5   # consecutive runs with queued == 0
    min_contactable_per_run: int = 1       # below this, the top of funnel is broken
    # Emails sent with zero inbound EVER seen before we suspect the IMAP reader rather
    # than a quiet market. 25 because a 0% reply rate over 25 cold emails is unlucky
    # but ordinary, while 0 inbound over 25 is better explained by a reader that
    # returns [] on every error. Set high enough that it cannot cry wolf early.
    alert_after_sent_without_inbound: int = 25
    max_proposals_per_day: int = 15  # anti-spam guard
    # Hard Claude-spend cap per pipeline run. Raised 2.00 -> 5.00, sized from the run
    # that hit the old value: run 31172835060 (2026-08-07) spent $2.004245, considered
    # 200 leads, scored 122 of 123 new ones and then stopped — ``budget_exhausted:
    # True`` with a lead it never reached. So the cap in *money* was binding while the
    # cap in *leads* said 200: `max_leads_per_run` was decorative for the last 78 leads,
    # and the run's visible output (queued 8, emailed 7) reads like a targeting problem.
    #
    # Arithmetic:
    #   $2.004245 / 122 scored leads      = $0.016428 per lead
    #   200 leads x $0.016428             = $3.29 to finish a full-cap run
    # Headroom on top of that, because $3.29 is this run's *mix*, not the worst one:
    # only 8 of 122 leads (6.6%) reached the Opus draft, while 25 of 122 (20.5%) were
    # contactable. Solving 122q + 8d = $2.004245 for a draft path (Sonnet research +
    # Opus write) costing ~5x a Sonnet qualification gives q≈$0.012, d≈$0.06; a run
    # where the whole contactable share drafts is 200q + 41d ≈ $5.0. Hence 5.00 —
    # 1.5x the measured full-run cost, not a round number. (`max_proposals_per_day`
    # = 15 independently caps drafts at ~16/run, so the realistic worst case is ~$3.5.)
    #
    # Why per-lead cost jumped: run 31033943812 spent $0.13 over 50 considered leads =
    # $0.0026 each, because uncontactable leads hit a `continue` before any model call.
    # That pre-gate is gone on purpose (a source that is never scored cannot be told
    # apart from one that scores badly), so 97 of the 122 leads in the run above were
    # scored-then-suppressed — Sonnet spend with no draft. $0.016428 / $0.0026 = 6.3x.
    # Any comment claiming an uncontactable lead "costs a regex and a cached DNS
    # lookup" predates that change; it now costs a qualification.
    #
    # Worst case the owner is agreeing to: $5.00 per run; the schedule is weekdays
    # 06:00 UTC (.github/workflows/outreach.yml), i.e. at most 23 runs/month, so
    # <= $115/month worst case and ~$72/month at the measured $3.29. Still a real
    # backstop: 2.5x the largest run ever observed, so a retry loop or a prompt
    # regression stops the run instead of billing all night. Do not remove it.
    max_usd_per_run: float = 5.0

    # --- SAFETY (do not flip without understanding platform ToS) ---
    dry_run: bool = True
    allow_send: bool = False  # auto-send is a ToS violation on Upwork/LinkedIn

    # --- auto cold-email outreach (the only channel safe to fully automate) ---
    # Sending email from your own address is NOT a platform ToS violation. Emails
    # go ONLY to leads that publicly posted a contact address looking to hire
    # (B2B legitimate interest), are rate-limited, deduped, and carry an opt-out.
    auto_email: bool = False        # master gate — nothing sends unless this is True AND SMTP is set
    max_emails_per_day: int = 20    # cap (reply quality + domain reputation + legality); keep sane to protect deliverability
    # Only email strong-fit leads — but a bar nothing clears sends nothing, and this
    # one had **never been cleared once**. Measured maxima across live runs:
    # 72 (31033943812), 78 (30988060139), 52 (30909649401). Every run that queued a
    # draft then skipped the send as ``low_fit``, so the system reported "success"
    # while the outreach channel was closed by a constant. That is this project's
    # signature defect (see the gates-must-not-fight-the-product pattern in the
    # README): a check that cannot pass for the reason it exists.
    #
    # 70 aligns it with ``min_fit_score``: a lead good enough to draft is good enough
    # to email. Deliberately NOT lower — the point of the gate is to protect sender
    # reputation, so it must still exclude the p50 (28) and p90 (58) bulk.
    outreach_min_fit: int = 70
    opt_out_mailbox: str = ""       # where "unsubscribe" replies go (defaults to owner_email)

    # --- auto-reply (autonomous handling of prospect replies) ---
    # Reads replies via IMAP and responds autonomously. HARD RULES enforced in the
    # prompt: never quotes firm price/scope/timeline (defers to a cal.com call),
    # never makes contractual/legal commitments; always BCCs the owner; capped per
    # thread so it can't loop. Gated off by default.
    auto_reply: bool = False
    # Reading the inbox is a SEPARATE concern from answering it, and must not share a
    # gate with it. Detecting a reply is what sets ``OutreachRecord.replied``, which is
    # what stops the follow-up sequence and what the optimizer measures. Gating the read
    # on the send flag means that with auto_reply off (the default) nobody is ever marked
    # replied: prospects who answered keep getting nudged, and reply_rate reads 0.0
    # forever — indistinguishable from a pitch nobody wants. Detection is safe to leave
    # on because it sends nothing; it only needs IMAP credentials.
    reply_detection: bool = True
    # Detect booked calls from cal.com's confirmation emails and send the owner a
    # briefing (who booked, why they probably booked, how to run the 15 minutes).
    #
    # This is the inbox-side replacement for ``POST /webhooks/cal``, which is correct,
    # tested, and has never fired once: a webhook needs a publicly reachable dashboard
    # and hosting one was declined. So ``call_booked_at`` was never stamped, every KPI
    # report showed **0 calls booked**, and on 2026-08-20 a real booking sat unremarked
    # in the inbox while the pipeline reported nothing had happened. Reads mail and
    # emails the owner; sends nothing to prospects, so it is safe on by default.
    detect_calls: bool = True
    # Blank means "derive it from smtp_host" — see :meth:`resolved_imap_host`. It is
    # deliberately NOT defaulted to a concrete provider: this field used to read
    # ``imap.gmail.com`` while the login credentials come from the *SMTP* settings, so
    # any non-Gmail SMTP provider silently decoupled reading from sending. That failure
    # is invisible (``fetch_replies`` swallows IMAP errors and returns ``[]``), and its
    # consequence is nudging people who already replied. A default that has to match a
    # secret nobody re-checks is a default that will eventually be wrong.
    imap_host: str = ""
    imap_port: int = 993
    max_replies_per_thread: int = 6   # safety stop against reply loops
    standard_rate: str = ""           # optional; blank = always defer pricing to the call

    # --- follow-ups (spaced nudges when a prospect doesn't reply) ---
    max_followups: int = 2            # touches after the first email
    followup_after_days: int = 3      # min days of silence before the next follow-up

    # --- alerting ---
    alert_email: str = ""             # where run-failure alerts go (defaults to owner_email)

    # --- dashboard auth (protect the UI before exposing it on any public URL) ---
    # HTTP Basic over HTTPS. If dashboard_password is blank, auth is DISABLED (fine
    # for local / SSH-tunnel use) — you MUST set a password before public hosting.
    dashboard_user: str = "admin"
    dashboard_password: str = ""

    # --- cal.com booking webhook (completes the funnel: emailed -> replied -> call booked -> won) ---
    cal_webhook_secret: str = ""      # HMAC secret from cal.com; blank disables signature checking

    # --- autonomous self-optimizer (tunes its own STRATEGY, never its source code) ---
    # Rotates pitch/subject variants + thresholds, measures reply rate, auto-reverts a
    # change that hurts. Never edits Python source and never touches safety invariants
    # (no auto-submit, opt-out, caps, pricing->call are all off-limits to the optimizer).
    self_optimize: bool = False       # gate
    optimize_min_samples: int = 20    # need this many contacted-with-known-outcome leads before tuning
    optimize_revert_drop: float = 0.05  # revert a trial if reply rate falls this much vs baseline

    # --- LinkedIn auto-posting (OAuth w_member_social; publishes content-engine drafts) ---
    # This is DISTINCT from the auto-submit ban. Publishing an ORIGINAL post to your
    # OWN feed through LinkedIn's official API with a member-authorized OAuth token is
    # ToS-compliant. (The ban is on scraping / bot-submitting proposals to *other*
    # people's job posts.) Gated off by default; requires an access token carrying the
    # w_member_social scope. Content still comes from the RAG-grounded content engine.
    linkedin_auto_post: bool = False        # master gate for auto-publishing to LinkedIn
    linkedin_access_token: str = ""         # OAuth 2.0 access token (w_member_social)
    linkedin_refresh_token: str = ""        # used to mint a fresh access token when it expires
    linkedin_client_id: str = ""            # app Client ID (for token refresh)
    linkedin_client_secret: str = ""        # app Client Secret (for token refresh)
    linkedin_author_urn: str = ""           # urn:li:person:<id>; auto-derived from the token if blank
    max_posts_per_day: int = 1              # anti-spam / feed-fatigue guard

    # --- identity (used in proposals/signature) ---
    owner_name: str = "Surya A"
    owner_email: str = "suryaanandan1995@gmail.com"
    owner_linkedin: str = "https://www.linkedin.com/in/surya-devsecops/"
    owner_site: str = "https://suryaanandan1995-dotcom.github.io"
    owner_calendly: str = "https://cal.com/surya-devsecops/15min"
    # GitHub account the portfolio repos live under. Used to build a REAL per-project
    # URL for the project a pitch cites: the prompt demands "a named project with no
    # link is an unverifiable claim", but the only link it was given was the portfolio
    # root, so the model supplied its own — and invented a repo that 404s. See
    # outreach.pitch._project_links.
    owner_github: str = "https://github.com/suryaanandan1995-dotcom"

    def resolved_imap_host(self) -> str:
        """The IMAP host to read replies from, derived from ``smtp_host`` when unset.

        IMAP logs in with the SMTP username and password, so the two hosts must belong
        to the same provider. Keeping ``imap_host`` as an independent setting with a
        Gmail default meant the pair could disagree with nothing to notice: point
        ``COPILOT_SMTP_HOST`` at any other provider and reads go to Gmail with
        credentials it will refuse, ``fetch_replies`` swallows the error and returns
        ``[]``, and every prospect who answered keeps getting follow-up nudges.

        Deriving beats defaulting because the transform is mechanical for every
        provider that offers both protocols: the submission host differs from the mail
        host only in the leading ``smtp`` label (``smtp.gmail.com`` /
        ``imap.gmail.com``, ``smtp.office365.com`` / ``outlook.office365.com`` is the
        one exception worth naming, ``smtp.zoho.eu`` / ``imap.zoho.eu``).

        An explicit ``imap_host`` always wins, so a provider that breaks the pattern
        stays configurable. Returns ``""`` when SMTP is unconfigured — the caller
        treats that as "not set up", which is correct, rather than as a host to try.
        """
        explicit = (self.imap_host or "").strip()
        if explicit:
            return explicit
        host = (self.smtp_host or "").strip().lower()
        if not host:
            return ""
        # Send-only relays have NO IMAP service, so there is no host to derive: mail
        # sent through them is replied to in whatever mailbox the From address belongs
        # to. Deriving "imap.sendgrid.net" would produce a nonexistent host and report
        # a login failure, when the real instruction is "set COPILOT_IMAP_HOST to your
        # own mailbox provider". Returning "" makes the checks say exactly that.
        # Amazon SES is regional (email-smtp.<region>.amazonaws.com), so it is matched
        # by shape rather than enumerated.
        if host in _SEND_ONLY_SMTP_HOSTS or (
            host.startswith("email-smtp.") and host.endswith(".amazonaws.com")
        ):
            return ""
        # Office 365 is the one common provider whose IMAP host is not the SMTP host
        # with the label swapped, so it is named rather than derived.
        if host in {"smtp.office365.com", "smtp-mail.outlook.com"}:
            return "outlook.office365.com"
        labels = host.split(".")
        if labels and labels[0] in {"smtp", "smtps", "mail", "send", "submission"}:
            return ".".join(["imap"] + labels[1:])
        return host


def get_settings() -> Settings:
    return Settings()
