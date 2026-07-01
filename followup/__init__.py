"""Automated follow-up sequences.

Spaced, polite nudges to prospects who were cold-emailed but never replied. The
whole subsystem is a NO-OP unless ``settings.auto_email`` is True — it shares the
same master gate, SMTP transport, daily cap, and suppression list as the initial
outreach, so nothing sends under the safe default config.
"""
