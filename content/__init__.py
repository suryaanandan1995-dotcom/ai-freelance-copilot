"""Inbound content engine: turn portfolio proof into marketing drafts.

Drafts ONLY. A human posts everything by hand — automated posting to LinkedIn,
Fiverr or Upwork is a Terms-of-Service / account-ban risk, the same safety
policy the rest of this system follows for proposal submission.
"""
from __future__ import annotations

from .engine import generate

__all__ = ["generate"]
