"""LinkedIn auto-posting.

Publishes RAG-grounded content-engine drafts to the owner's OWN LinkedIn feed via
the official API with a member-authorized OAuth token (``w_member_social``). This is
ToS-compliant, unlike scraping or bot-submitting to other accounts. Gated off by
default (``COPILOT_LINKEDIN_AUTO_POST``).
"""
from linkedin.client import LinkedInClient, LinkedInError

__all__ = ["LinkedInClient", "LinkedInError"]
