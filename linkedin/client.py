"""LinkedIn API client — publishes text posts to the owner's own feed.

Uses LinkedIn's official Share API (``/v2/ugcPosts``) with a member-authorized
OAuth 2.0 access token carrying the ``w_member_social`` scope. Publishing an
ORIGINAL post to your OWN feed through the official API is ToS-compliant; this is
NOT the third-party account automation that gets people banned.

The access token is short-lived (~60 days). If a ``refresh_token`` + client
credentials are configured, an expired token is refreshed transparently once.

SAFETY: this module can publish, so it is only reachable when
``COPILOT_LINKEDIN_AUTO_POST`` is true (enforced by callers) and always requires a
member-authorized token. It never touches other accounts and never scrapes.
"""
from __future__ import annotations

import json
from typing import Any

import requests

from config import Settings, get_settings

USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
UGC_URL = "https://api.linkedin.com/v2/ugcPosts"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"


class LinkedInError(RuntimeError):
    """Raised on any LinkedIn API failure (auth, network, or rejected post)."""


class LinkedInClient:
    """Thin wrapper over the LinkedIn Share API. ``session`` is injectable for tests."""

    def __init__(self, settings: Settings | None = None, session: Any = None) -> None:
        self.settings = settings or get_settings()
        self._session = session or requests.Session()
        self._token = (self.settings.linkedin_access_token or "").strip()
        self._urn = (self.settings.linkedin_author_urn or "").strip()

    # -- auth -----------------------------------------------------------------
    @property
    def token(self) -> str:
        if not self._token:
            raise LinkedInError("COPILOT_LINKEDIN_ACCESS_TOKEN is not set")
        return self._token

    def _refresh_token(self) -> bool:
        """Mint a fresh access token from the refresh token. Returns True on success."""
        s = self.settings
        if not (s.linkedin_refresh_token and s.linkedin_client_id and s.linkedin_client_secret):
            return False
        r = self._session.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": s.linkedin_refresh_token,
                "client_id": s.linkedin_client_id,
                "client_secret": s.linkedin_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        if r.status_code != 200:
            return False
        tok = r.json().get("access_token")
        if not tok:
            return False
        self._token = tok
        return True

    # -- identity -------------------------------------------------------------
    def author_urn(self) -> str:
        """``urn:li:person:<id>`` — configured value, else derived from the token."""
        if self._urn:
            return self._urn
        data = self._userinfo()
        sub = data.get("sub")
        if not sub:
            raise LinkedInError(f"userinfo missing 'sub': {data}")
        self._urn = f"urn:li:person:{sub}"
        return self._urn

    def _userinfo(self) -> dict:
        r = self._session.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {self.token}"}, timeout=20
        )
        if r.status_code == 401 and self._refresh_token():
            r = self._session.get(
                USERINFO_URL, headers={"Authorization": f"Bearer {self.token}"}, timeout=20
            )
        if r.status_code != 200:
            raise LinkedInError(f"userinfo failed: {r.status_code} {r.text}")
        return r.json()

    def whoami(self) -> dict:
        """Return {name, email, urn} — a read-only token/identity check."""
        data = self._userinfo()
        return {
            "name": data.get("name", ""),
            "email": data.get("email", ""),
            "urn": self.author_urn(),
        }

    # -- publishing -----------------------------------------------------------
    def _post_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def create_post(self, text: str, visibility: str = "PUBLIC") -> dict:
        """Publish a text post to the owner's feed. Returns {id, url, status}.

        ``visibility`` is ``PUBLIC`` (anyone) or ``CONNECTIONS``.
        """
        text = (text or "").strip()
        if not text:
            raise LinkedInError("post text is empty")
        payload = {
            "author": self.author_urn(),
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": visibility},
        }
        body = json.dumps(payload)
        r = self._session.post(UGC_URL, headers=self._post_headers(), data=body, timeout=30)
        if r.status_code == 401 and self._refresh_token():
            r = self._session.post(UGC_URL, headers=self._post_headers(), data=body, timeout=30)
        if r.status_code not in (200, 201):
            raise LinkedInError(f"post failed: {r.status_code} {r.text}")
        post_id = r.headers.get("x-restli-id") or (r.json().get("id", "") if r.text else "")
        url = f"https://www.linkedin.com/feed/update/{post_id}/" if post_id else ""
        return {"id": post_id, "url": url, "status": r.status_code}
