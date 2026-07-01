"""Auto-reply subsystem.

Reads replies to our cold outreach over IMAP and responds autonomously in the
owner's voice. It fully auto-negotiates the conversation with ONE hard exception:
it never commits pricing, scope, timeline, or contractual/legal terms — those are
always deferred to a short cal.com call. Every auto-reply BCCs the owner, and each
thread is capped so the bot can't loop.

Everything is gated behind ``settings.auto_reply`` (default False) and requires
SMTP/IMAP config, so the default configuration is a hard no-op.
"""
