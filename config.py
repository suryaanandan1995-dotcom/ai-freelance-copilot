"""Central configuration (pydantic-settings).

Defaults are SAFE and OFFLINE: SQLite DB, dry-run on, sending disabled. Nothing
is ever submitted to a platform automatically — `allow_send` exists only to let
the approval dashboard mark items as sent by a human.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- RAG ---
    portfolio_repos_path: str = ".."  # where the user's repos live (for KB ingest)
    rag_store_path: str = "data/portfolio_kb.json"

    # --- pipeline policy ---
    min_fit_score: int = 70          # leads below this are dropped
    max_leads_per_run: int = 50
    # Skip research+drafting for leads with no deliverable contact when the goal is
    # auto-email. 18 of 25 drafted proposals were thrown away at the contact step
    # after being paid for at Opus prices; checking first costs a regex + a cached
    # DNS lookup. Set False to keep drafting everything for human submission.
    require_contact_before_draft: bool = True
    # Require the contact domain to publish MX/A records before sending. Protects
    # sender reputation (the one asset cold outreach cannot rebuy). Disable only
    # for offline tests/dev, where fixture domains like "acme.io" don't resolve.
    verify_contact_domain: bool = True

    # --- run-health alerting (see monitor/health.py) ---
    # Every one of 24 production runs reported "success" while sending nothing,
    # because "sent 0 emails" was never an error condition. These thresholds turn
    # a silently idle funnel into a failed run that emails the owner.
    alert_after_zero_email_runs: int = 3   # consecutive runs with emailed == 0
    alert_after_zero_queue_runs: int = 5   # consecutive runs with queued == 0
    min_contactable_per_run: int = 1       # below this, the top of funnel is broken
    max_proposals_per_day: int = 15  # anti-spam guard
    max_usd_per_run: float = 2.0     # hard Claude-spend cap per pipeline run

    # --- SAFETY (do not flip without understanding platform ToS) ---
    dry_run: bool = True
    allow_send: bool = False  # auto-send is a ToS violation on Upwork/LinkedIn

    # --- auto cold-email outreach (the only channel safe to fully automate) ---
    # Sending email from your own address is NOT a platform ToS violation. Emails
    # go ONLY to leads that publicly posted a contact address looking to hire
    # (B2B legitimate interest), are rate-limited, deduped, and carry an opt-out.
    auto_email: bool = False        # master gate — nothing sends unless this is True AND SMTP is set
    max_emails_per_day: int = 20    # cap (reply quality + domain reputation + legality); keep sane to protect deliverability
    outreach_min_fit: int = 80      # only email strong-fit leads
    opt_out_mailbox: str = ""       # where "unsubscribe" replies go (defaults to owner_email)

    # --- auto-reply (autonomous handling of prospect replies) ---
    # Reads replies via IMAP and responds autonomously. HARD RULES enforced in the
    # prompt: never quotes firm price/scope/timeline (defers to a cal.com call),
    # never makes contractual/legal commitments; always BCCs the owner; capped per
    # thread so it can't loop. Gated off by default.
    auto_reply: bool = False
    imap_host: str = "imap.gmail.com"
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


def get_settings() -> Settings:
    return Settings()
