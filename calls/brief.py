"""Turn a detected booking into the email the owner actually needs.

The requirement, in the owner's words: *"if any call is booked, email me that this call
is booked and what is the purpose of that and how to handle it… I use mails, check out
only daily."* So this is not a notification — a notification is a second thing to go and
research. It is a briefing: who, why they probably booked, and a minute-by-minute plan,
complete enough to be the only thing read before joining.

The honest part matters more than the helpful part. For an inbound booking from a
personal Gmail there is genuinely no way to know the purpose, and a briefing that
invented one would send the owner in confidently wrong — worse than sending them in
knowing they must ask. So the purpose section states what is *known*, what it *implies*,
and where the guess ends.
"""
from __future__ import annotations

from calls import parse


def _outreach_purpose(lead, pitch: str) -> list[str]:
    """We cold-emailed this person: the purpose is the job in their own post."""
    lines = [
        "WHY THEY BOOKED — known, not guessed",
        "  You cold-emailed them. They booked instead of replying, which is the",
        "  strongest signal this pipeline produces: no objections to answer, they",
        "  just want to talk.",
        "",
    ]
    if lead is not None:
        lines += [
            f"  Their post : {lead.title or '(no title)'}",
            f"  Company    : {lead.company or '(not named)'}",
            f"  Source     : {lead.source}",
        ]
        if lead.url:
            lines.append(f"  Read it    : {lead.url}")
        lines.append("")
        if lead.description:
            snippet = " ".join(lead.description.split())[:700]
            lines += ["  What they asked for, in their words:", f"    {snippet}", ""]
    if pitch:
        lines += [
            "  WHAT YOU CLAIMED — an agent wrote this and sent it without you reading it.",
            "  Read it before you join; contradicting your own email in the first two",
            "  minutes is the fastest way to lose a warm lead.",
            "",
        ]
        lines += [f"    {line}" for line in pitch.splitlines()]
        lines.append("")
    return lines


def _inbound_purpose(booking: dict, latest_post) -> list[str]:
    """No cold email went to this address, so the purpose is genuinely unknown."""
    address = booking.get("invitee_email", "")
    lines = [
        "WHY THEY BOOKED — unknown. Do not guess on the call, ask.",
        "  This address was never cold-emailed, so the booking is INBOUND: they found",
        "  the cal.com link themselves. It is published in your LinkedIn posts, on the",
        "  GitHub portfolio site, and in cold emails — so LinkedIn or GitHub.",
        "",
    ]
    if parse.is_freemail(address):
        lines += [
            "  It is a personal mailbox, not a company domain, so there is no company to",
            "  research before the call. That narrows the realistic possibilities to four,",
            "  and one question separates them:",
            "",
            "    1. A prospect — a founder or lead who has an LLM/platform problem now.",
            "    2. A recruiter or agency sourcing for a client.",
            "    3. A peer who liked a post and wants to talk shop or get advice.",
            "    4. Someone selling you something.",
            "",
            "  Ask this in the first 60 seconds, before pitching anything:",
            '    "Before I talk about me — what made you book? Is there something you\'re',
            '     trying to ship right now, or were you curious about one of the posts?"',
            "",
            "  1 and 2 are worth your full 15 minutes. 3 is worth being generous with for",
            "  five and then offering to continue by email. 4, end it politely and early.",
            "",
        ]
    else:
        domain = address.rpartition("@")[2]
        lines += [
            f"  It is a company address ({domain}), so spend two minutes on {domain} and",
            "  their LinkedIn before you join — knowing what they sell is most of the",
            "  preparation. Still open by asking what made them book: an inbound booking",
            "  with no email thread behind it has no agenda you can assume.",
            "",
        ]
    if latest_post is not None and latest_post.body:
        first = " ".join(latest_post.body.split())[:300]
        when = (
            latest_post.published_at.strftime("%Y-%m-%d")
            if latest_post.published_at
            else "recently"
        )
        lines += [
            f"  Most likely referrer — your LinkedIn post of {when}:",
            f"    {first}…",
            "  Assume that is the hook until they say otherwise. If they mention it, you",
            "  already know exactly which claim caught them.",
            "",
        ]
    return lines


def _plan(booking: dict, origin: str) -> list[str]:
    """Fifteen minutes, allocated. Same shape either way; the opening differs."""
    name = booking.get("invitee_name") or "them"
    first_name = name.split()[0] if name else "there"
    if origin == "outreach":
        opening = (
            f'    "{first_name}, thanks for booking. Before I pitch anything — what\'s'
            f' the\n     problem that made you post in the first place?"'
        )
    else:
        opening = (
            f'    "{first_name}, thanks for booking. So I use the time well — what made'
            f' you\n     reach out? Is there something you\'re trying to ship right now?"'
        )
    return [
        "HOW TO HANDLE IT — 15 minutes",
        "",
        "  0:00  Open with a question, not a pitch:",
        opening,
        "        Then stop talking. Let them describe the problem for two minutes.",
        "",
        "  2:00  Two proof stories, chosen AFTER hearing the problem — not before.",
        "        Each one: what was broken, what you built, what it changed. Numbers",
        "        beat adjectives (deploy time down ~50%, deploy frequency up ~75%).",
        "",
        "  7:00  Say the awkward thing yourself, first — rate, location, availability,",
        "        whatever it is. Raising it reads as judgement; being caught on it reads",
        "        as wasted time.",
        "",
        " 10:00  Close the SMALL thing. Never try to close a contract in 15 minutes:",
        '    "Give me one real problem, fixed scope, a few days, paid. You see how I',
        '     work on your actual data instead of trusting my CV."',
        "        A paid discovery slice is the shortest path to money and the easiest",
        "        yes they can give you.",
        "",
        " 13:00  Agree the next step out loud, with a date, and say you'll email a",
        "        one-page summary within the hour. Then actually send it — the summary",
        "        is what gets forwarded to whoever signs.",
        "",
        "  Do NOT: quote a firm price for undefined scope, agree to unpaid 'test",
        "  tasks' beyond a short conversation, or accept an NDA on the call.",
    ]


def build_brief(
    *,
    booking: dict,
    origin: str,
    lead=None,
    pitch: str = "",
    latest_post=None,
) -> tuple[str, str]:
    """``(subject, body)`` for the owner's briefing email."""
    name = booking.get("invitee_name") or booking.get("invitee_email") or "someone"
    when = booking.get("when_text") or "(time not parsed — see the cal.com email)"
    subject = f"CALL BOOKED — {name} — {when}"

    lines = [
        f"A call was booked{' by a lead you emailed' if origin == 'outreach' else ''}.",
        "",
        "WHO",
        f"  {name} <{booking.get('invitee_email') or 'address not parsed'}>",
        (
            "  In the outreach ledger — this is a lead the pipeline contacted."
            if origin == "outreach"
            else "  NOT in the outreach ledger — inbound, not from a cold email."
        ),
        "",
        f"WHEN  {when}",
    ]
    if booking.get("join_url"):
        lines.append(f"JOIN  {booking['join_url']}")
    lines += ["", *(
        _outreach_purpose(lead, pitch)
        if origin == "outreach"
        else _inbound_purpose(booking, latest_post)
    ), *_plan(booking, origin)]
    lines += [
        "",
        "—",
        "Detected from the cal.com confirmation email in your inbox. This briefing is",
        "sent once per booking; a cancellation or reschedule gets its own email.",
    ]
    return subject, "\n".join(lines)


def build_cancellation(booking: dict) -> tuple[str, str]:
    """A cancellation needs no plan — only to arrive before the owner joins a dead call."""
    name = booking.get("invitee_name") or booking.get("invitee_email") or "someone"
    when = booking.get("when_text") or "(time not parsed)"
    return (
        f"CALL CANCELLED — {name} — {when}",
        "\n".join(
            [
                f"{name} <{booking.get('invitee_email') or '?'}> cancelled.",
                f"Was: {when}",
                "",
                "Nothing to prepare. Worth one line by email if they were a real lead —",
                "a cancellation is not a no, and asking once whether to rebook costs",
                "nothing.",
            ]
        ),
    )
