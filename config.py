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
    max_proposals_per_day: int = 15  # anti-spam guard
    max_usd_per_run: float = 2.0     # hard Claude-spend cap per pipeline run

    # --- SAFETY (do not flip without understanding platform ToS) ---
    dry_run: bool = True
    allow_send: bool = False  # auto-send is a ToS violation on Upwork/LinkedIn

    # --- identity (used in proposals/signature) ---
    owner_name: str = "Surya A"
    owner_email: str = "suryaanandan1995@gmail.com"
    owner_linkedin: str = "https://www.linkedin.com/in/surya-devsecops/"
    owner_site: str = "https://suryaanandan1995-dotcom.github.io"
    owner_calendly: str = "https://cal.com/surya-devsecops/15min"


def get_settings() -> Settings:
    return Settings()
