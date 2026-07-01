# Email Deliverability — getting into inboxes, not spam

Deliverability is the make-or-break for cold outreach. A perfect email that lands
in spam reaches no one. This guide takes you from "sending from Gmail" (fine to
start) to a professional, high-inbox-placement setup.

> **Rule of thumb:** volume × reputation × content. Keep volume low, reputation
> clean, and content human — and you'll land in the inbox.

---

## Phase A — Do these now (free, no new domain)

You're already sending from your Gmail at low volume, which is okay to start. Tighten these:

1. **Keep volume low & ramping.** Leave `COPILOT_MAX_EMAILS_PER_DAY` at **5–8**. Do not spike it. New sending patterns that jump to 50/day look like spam.
2. **Plain text, no images, ≤1 link.** The copilot already writes plain, human emails — keep it that way. One link max (your cal.com), no tracking pixels, no attachments.
3. **Personalized first line.** Already handled — the first line references their post. Generic openers ("Dear Sir/Madam", "I hope this finds you well") are spam signals.
4. **Real identity + opt-out.** Already in the footer (your name, site, "reply unsubscribe"). Keep it.
5. **Warm the account gradually.** For the first 2 weeks, keep it to ~5/day and reply to a few normal emails from the account so it looks human.
6. **Test your spam score.** Send one of your drafts to **[mail-tester.com](https://www.mail-tester.com)** → aim for **9–10/10**. It tells you exactly what to fix.

That gets you reasonable placement. For serious volume and professionalism, do Phase B.

---

## Phase B — Dedicated sending domain (the real upgrade, ~30–45 min)

Sending business outreach from `@gmail.com` caps your credibility and deliverability. A dedicated domain with proper auth is the professional standard.

### 1. Buy a domain (~$10/yr)
Get something like `surya-devsecops.com` or `suryacloud.dev` (Namecheap, Cloudflare, Porkbun). A domain that matches your brand also boosts trust in the email itself.

### 2. Set up a mailbox on it
Pick one:
- **Google Workspace** (~$6/mo) — you get `hello@surya-devsecops.com`, and it's the same Gmail SMTP you already use. Easiest.
- **Cloudflare Email Routing (free) + a sending service** — forward inbound to your Gmail, send via a provider (below).
- **A cold-email-friendly provider** (Zoho Mail free tier, Fastmail, or a sending API like Resend/Postmark for transactional).

### 3. Add the 3 DNS records (this is what stops spam-foldering)
In your domain's DNS, add:

| Record | Type | Value (example — use your provider's exact values) |
|--------|------|------|
| **SPF** | TXT | `v=spf1 include:_spf.google.com ~all` (Google Workspace) |
| **DKIM** | TXT | Generated in your mail provider's admin (Google: Admin → Apps → Gmail → Authenticate email) — paste the key it gives you |
| **DMARC** | TXT (`_dmarc`) | `v=DMARC1; p=none; rua=mailto:you@yourdomain.com` (start with `p=none`, tighten to `quarantine` later) |

> These three prove you're a legitimate sender. Without them, cold email from a new domain almost always spam-folders.

### 4. Point the copilot at the new mailbox
Update your `.env` / GitHub secrets:
```
COPILOT_SMTP_HOST=smtp.gmail.com            # (Google Workspace uses Gmail SMTP)
COPILOT_SMTP_USER=hello@surya-devsecops.com
COPILOT_SMTP_PASSWORD=<app password for the new mailbox>
COPILOT_OWNER_EMAIL=hello@surya-devsecops.com
```
(The IMAP host stays `imap.gmail.com` for a Workspace mailbox.)

### 5. Warm up the new domain (critical — don't skip)
A brand-new domain has zero reputation. Ramp slowly:

| Week | Emails/day (`MAX_EMAILS_PER_DAY`) |
|------|-----|
| 1 | 5 |
| 2 | 8 |
| 3 | 12 |
| 4+ | 15–20 (only if reply rate is healthy) |

Send a few normal person-to-person emails from it too in week 1. Optionally use a warm-up service (Warmup Inbox, Instantly, Mailreach) for a few weeks.

### 6. Monitor
- **[Google Postmaster Tools](https://postmaster.google.com)** — add your domain, watch spam rate & reputation.
- Keep an eye on your **reply rate** in the dashboard Analytics page — a sudden drop often means placement slipped.

---

## Content hygiene checklist (the copilot mostly handles these)
- ✅ Plain, conversational, first line specific to them
- ✅ One link (cal.com), no image/tracking pixel, no attachment
- ✅ Real name + site + opt-out in the signature
- ✅ Low volume, deduped, opt-outs honored
- ❌ Avoid: "guarantee", "free", "act now", ALL CAPS, "!!!", link shorteners

---

## TL;DR
- **Today:** keep volume 5–8/day, run a draft through mail-tester.com.
- **To scale:** buy a domain → Google Workspace mailbox → add SPF + DKIM + DMARC → point the copilot at it → warm up over 3–4 weeks → monitor in Postmaster Tools.

Deliverability is the difference between "the automation runs" and "clients actually see your emails." It's worth the 45 minutes.
