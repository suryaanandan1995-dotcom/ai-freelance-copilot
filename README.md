# AI Freelance Copilot

> An agentic copilot that discovers, qualifies, researches, and **drafts** tailored freelance proposals from your portfolio — then hands every draft to **you** to review and submit. It never auto-submits, because that would get your account banned.

![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-dashboard-009688?style=flat-square&logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-agent%20pipeline-1C3C3C?style=flat-square)
![Anthropic Claude](https://img.shields.io/badge/Anthropic-Claude-D97757?style=flat-square&logo=anthropic&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-6E56CF?style=flat-square&logo=anthropic&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00?style=flat-square&logo=sqlalchemy&logoColor=white)
![CI](https://img.shields.io/github/actions/workflow/status/suryaanandan1995-dotcom/ai-freelance-copilot/ci.yml?branch=main&label=CI&style=flat-square)
![License: MIT](https://img.shields.io/badge/License-MIT-green?style=flat-square)

## Overview

**AI Freelance Copilot** is a multi-agent system that does the tedious top of the freelancing funnel for you. It fans out to public opportunity feeds, scores each lead against your real skills, researches the prospect, and drafts a specific, non-spammy proposal grounded in your portfolio — quantified wins and all. Every draft lands in a review queue with a fit score and a one-click link, and you get a digest. You decide what to send.

It is built to run **offline by default**: a deterministic hashing embedder and an in-memory vector store mean the full test suite and a dry run need no API key and no network. Plug in your Claude API key and it drafts with Opus; leave it out and the architecture still stands up end to end.

## The Problem It Solves

Freelancers lose hours every day to the same grind: skim dozens of listings, guess which ones fit, research the client, and write a fresh proposal that doesn't read like a template. Most people either burn out or start blasting generic copy — which platforms punish.

This copilot collapses that grind into a single reviewed queue:

- **Discovery** across multiple read-only sources, deduplicated.
- **Qualification** with an explicit 0–100 fit score and the portfolio repos that prove it.
- **Research + drafting** that cite your actual projects and quantified results via RAG.
- **A compliance gate** that rejects anything too short, generic, or spammy before it ever reaches you.
- **A hard cost cap** so a runaway run can't quietly spend your Claude budget.

You spend your time picking winners and pressing send, not trawling job boards.

## How It Works & the Safety Model

This system **never auto-submits anything to any platform.** That is a deliberate, load-bearing design decision — not an oversight.

The pipeline only ever **discovers → qualifies → researches → DRAFTS → queues**. Drafts are written to a local database with status `drafted` and surfaced in an approval dashboard. A **human reviews each one and submits it themselves** on Upwork / LinkedIn / wherever.

Auto-submitting proposals or connection requests **violates the Terms of Service** of Upwork, LinkedIn, and every comparable platform, and is a fast track to an account ban. So:

- The lead sources are **read-only** — they fetch public listings and nothing else.
- The `allow_send` and `dry_run` settings exist only so a human-driven dashboard can mark an item as *already sent by a person*. There is no code path that posts to a freelance platform.
- The closest thing to "submit" is the MCP `mark_submitted` tool, which merely records in the local CRM that you submitted it.

## Architecture

![Architecture](docs/architecture.png)

Lead sources feed a fetch-and-dedupe step, then each fresh lead runs through a **LangGraph** agent pipeline — *qualify → research → write → review* — with the proposal writer pulling proof points from a portfolio **RAG** knowledge base and a **cost guardrail** metering every Claude call. Approved drafts are queued in the database, an **email/WhatsApp digest** goes out, and the **approval dashboard** lets a human review and submit. When you mark a lead **won**, the **learning loop** embeds that winning proposal back into the KB. A `/metrics` endpoint exposes everything to Prometheus and Grafana.

> The editable diagram source lives at [`docs/architecture.drawio`](docs/architecture.drawio) — open it with [diagrams.net](https://app.diagrams.net) to modify.

## Agents

| Agent | Model | Role |
|-------|-------|------|
| **Qualifier** | Sonnet (cheap) | Scores lead fit 0–100 and maps it to portfolio repos that prove it. |
| **Researcher** | Sonnet | Summarizes the opportunity into structured enrichment (stack, pain points). |
| **Proposal Writer** | Opus (strong) | Drafts a tailored proposal via RAG, citing real projects and quantified wins. |
| **Compliance / Reviewer** | None (deterministic rules) | Gate: length, anti-spam, must-cite, dedupe — approve or reject before queuing. |
| **Follow-up** | Opus/Sonnet | Drafts a short, polite nudge for a lead gone quiet (human still sends it). |

## Lead Sources

All sources are **read-only** — they fetch public listings and submit nothing.

| Source | What it reads |
|--------|---------------|
| **HN "Who is hiring"** | The two most recent monthly Hacker News hiring threads (public Algolia API). **The main source of leads with a public contact email** — posters routinely publish "email jobs@company.com to apply", which is what makes the [auto-email outreach](#auto-email-outreach) channel possible. Comments are ranked by *contact-hint then AI-infra relevance* **before** the per-run limit truncates them, so the leads that survive are the ones that can actually be emailed. **Job seekers are excluded**: the pinned `whoishiring` account also posts a "Who wants to be hired?" thread, and Algolia's `query` is ranked relevance rather than equality, so résumés came back for this search — 5 of 60 measured leads, ranking *high* because the ranking rewards exactly what a CV has (a published address plus dense keywords). |
| **HN "Freelancer? Seeking freelancer?"** | The companion monthly thread, where the poster is explicitly hiring a contractor. |
| **Day-rate contract** *(optional)* | Adzuna's official jobs API across **10 country endpoints** — UK (onsite *and* remote) plus remote-only in the US, Germany, Netherlands, France, Australia, New Zealand, Switzerland, Austria and Belgium. This is the segment that actually pays day rates (£525–£550/day DevOps, **£550 median for LLM roles with vacancies up +247% YoY**), and the volume is mostly *outside* the UK: 16,223 remote contract vacancies in the US against 3,687 in the UK. Off unless you set `COPILOT_ADZUNA_APP_ID` + `COPILOT_ADZUNA_APP_KEY` (free key: [developer.adzuna.com](https://developer.adzuna.com)); returns nothing, with a log line, when unconfigured. |
| **Remote boards** | RemoteOK, WeWorkRemotely & Remotive feeds — works out of the box, no config. |
| **Jobicy / Working Nomads** | Two further remote-jobs feeds, no config. |
| **Contra / startup** | Startup-oriented opportunity feeds (configurable via `COPILOT_STARTUP_FEEDS`), deduped across feeds since aggregators syndicate each other. |
| **Upwork RSS** *(optional)* | Upwork **discontinued public RSS on 2024-08-20**, so this is off by default. Set `COPILOT_UPWORK_FEEDS` only if you have a third-party RSS bridge; otherwise use Upwork's native saved-search alerts and bid manually. The adapter returns nothing (no error) when unconfigured. |

> Most board listings (Upwork, LinkedIn, remote boards) link back to a platform and expose **no direct email**, so they stay human-submit. The Hacker News threads are the exception — and the only place the auto-email channel sends to.
>
> **`reddit_forhire` was removed, not disabled.** Reddit began returning `403 Blocked` to unauthenticated JSON requests; the adapter contributed nothing but a failing HTTP call on every run for a month. A source that cannot fetch is deleted rather than left in the registry looking operational.

### Which markets, and three ways that went wrong

Remote contract work isn't geographically bounded, so the contract source spans the UK,
the US, the EU and ANZ. The UK is searched **onsite and remote** (that's where the
contractor is); every other market is **remote-only**, because a role requiring
relocation is not a lead and paying an LLM to qualify one is pure cost.

Getting there surfaced three traps, all of the same family — a filter that looks right
and fails quietly:

| what looked right | what it actually did |
| --- | --- |
| `where=Remote` on the US endpoint | returned **0** of 4,816 matches; the endpoint already scopes the country, and any `where` on top of the remote phrase zeroes the set. `where=Germany` on the German endpoint: 0 of 104. |
| `where=remote` as the remote filter | 0 results in **every** country. The working filter is `what_phrase=remote`. |
| a bare Adzuna job id as `external_id` | ids are unique *per country endpoint*, not globally — so a German role could collide with a British one and be dropped as a duplicate, silently, because dedupe logs nothing. Ids are now namespaced `de:1234`. |

A fourth, separate leak: the keyword gate reads the title **and** description together —
deliberately, since a genuine role often names its stack only in the body. But that means
one tech word anywhere in a long description passes the whole listing, and job
descriptions are full of them. A live fetch qualified *Marketing Manager* (its body says
"agentic"), *Account-Based Marketing Mgr* ("AWS" — the role is at AWS), *Strategic
Sourcing Principal* ("Azure") and four Project Manager roles. Each cost a Claude call to
score and none was qualifiable. Titles are now screened against an exclusion list, which
returns *which word matched* rather than a bool — a silent filter makes an over-strict
gate indistinguishable from an empty market, and this project has shipped that bug
before.

Salary figures carry the **currency of their endpoint** and are never converted: an
approximate rate in a proposal is a wrong number, and a wrong number is worse than no
number. The same reasoning already stops the annualised figure being back-computed into
a guessed day rate.

Transient failures are retried (2 attempts, exponential backoff) but **400/401/403/404
are not** — those fail identically forever, so retrying only delays the report that
would fix them. Without the retry, Adzuna's sporadic free-tier 503s marked the whole
source `broken`, which is how you teach yourself to ignore the one verdict that means
"there is a bug".

## Auto-Email Outreach

The proposal pipeline is **draft-and-queue for a human** because auto-submitting to Upwork/LinkedIn violates their ToS. But there is **one** channel that is safe to fully automate: **sending a plain email from your own address** to someone who *publicly posted a contact address looking to hire*. That is not a platform ToS violation, and it is the basis of the auto-email outreach subsystem ([`outreach/`](outreach/)).

It is **off by default** and deliberately low-volume. Enable it with `--auto-email` on the `run` command:

```bash
python main.py run --auto-email --notify
```

For each freshly **queued, strong-fit** lead, it:

1. **Extracts a contact email** ([`outreach/extract.py`](outreach/extract.py)) from the lead description / raw fields — rejecting `noreply@`, `@example.`, asset/error domains, etc. In practice this only ever fires on HN "Who is hiring" posts; board listings expose no address and are skipped.
2. **Drafts a short (110–150 word) human cold-intro email** with Opus ([`outreach/pitch.py`](outreach/pitch.py)) — first line specific to their post, one cited portfolio project, at most one quantified win, a soft CTA to the booking link, signed with real identity.
3. **Sends it** over your SMTP ([`outreach/sender.py`](outreach/sender.py)) and records an `OutreachRecord`.

**Every guardrail is on by default:**

- **Master gate** — nothing sends unless `COPILOT_AUTO_EMAIL=true` *and* `COPILOT_SMTP_HOST` is set. The sender is a hard no-op otherwise, so the default config can never email anyone.
- **Fit floor** — only leads scoring ≥ `COPILOT_OUTREACH_MIN_FIT` (default **70**) are contacted. It shipped at **80**, a bar **no run had ever cleared once**, so the send path was closed by arithmetic: (the first version of this line claimed 78 was the highest score the scorer could produce — that was wrong, and instructively so. 78 was the max of the three most recent *aggregates*, the only numbers left in the CI logs; the July run in `copilot.db` records 13 leads scoring **72–88**. Pinning a ceiling to that stale sample would have made the test the next gate fighting the product, so the assertion is the domain bound instead.) the pipeline paid Opus prices to draft a proposal, logged `queued: 1`, then discarded it as `low_fit` — and reported the run a success. That is this repo's signature defect, a gate that cannot pass for the reason it exists, and it is why 24 consecutive green runs emailed nobody. The floor is now pinned to `min_fit_score` by [`tests/test_thresholds.py`](tests/test_thresholds.py): **a lead good enough to draft is good enough to send**, since the draft is the expensive half. It is deliberately *not* lower — measured fit p50 is 28 and p90 is 58, and the floor still has to exclude those, because sender reputation is the one asset cold outreach cannot rebuy.
- **Daily cap** — at most `COPILOT_MAX_EMAILS_PER_DAY` sends per UTC day (code default **20**; the shipped [`.env.example`](.env.example) sets **8**). Low volume protects reply quality, domain reputation, and legality. The cap is counted **across every channel** ([`outreach/quota.py`](outreach/quota.py)) — cold emails and follow-ups draw on one budget, because a sending domain's reputation isn't a property of the code path that used it.
- **Warmup ramp** — the configured cap is a ceiling, not a target. `effective_cap` limits any day to `max(10, 1.5 x the busiest day in the last 14)`, so raising the cap or a jump in contactable supply cannot turn 6 sends/week into 20/day overnight; a volume step change from an unknown domain is what reputation systems are built to catch. It caps only, never raises. **The first version of this did nothing at all**: the timestamps come back from a plain `DateTime` column as naive, comparing them against an aware `now` raised `TypeError` on every call, and the blanket `except` — there so a measurement bug can never block a send — returned the unramped cap silently. The fallback now logs "**NO warmup ramp**" and a test asserts that wording.
- **Dedupe** — the `outreach` table has a **UNIQUE** email column; an address is **never emailed twice**, across runs.
- **Suppression list** — `data/suppressed.txt` (one lowercased email per line) is honored before every send. Drop an address in there to permanently stop emailing it.
- **Opt-out footer** — every email always carries a plain-text identity + opt-out line (`Reply 'unsubscribe' …`) and a `Reply-To` to `COPILOT_OPT_OUT_MAILBOX`.

**Legality.** This is B2B outreach to people who *published a hiring contact* — a textbook **legitimate-interest** basis under UK **PECR**/GDPR and consistent with CAN-SPAM: a real sender identity, a real reply address, an easy opt-out, no deception, and low volume by design. It is **not** scraped bulk marketing. Upwork/LinkedIn proposals stay human-submit — this channel is **email only** and never touches a platform API.

> Stats from a run include `emailed` and an `emailed_skipped` breakdown (`low_fit`, `duplicate`, `suppressed`, `daily_cap`, and a **reason-coded** `no_email_*` — `no_address_in_post`, `domain_refused_mail`, `rejected_do_not_contact`, `rejected_non_hiring`) so you can see exactly why each lead was or wasn't contacted. The single `no_email_pregate` key it replaced covered **851 of 1,047** leads and named none of them.

### Deploying the schedule

Outreach runs unattended, so the **dedupe/cap state must persist between runs** — otherwise the `outreach` table resets and you could re-email people. Two supported ways:

1. **System cron on an always-on box** with a **persistent SQLite file** (or the Kubernetes CronJob). The DB lives on disk and survives across runs, so dedupe works out of the box. Add `--auto-email` to the scheduled command and set the env vars.
2. **GitHub Actions** ([`.github/workflows/outreach.yml`](.github/workflows/outreach.yml)) — runners are **ephemeral**, so you **must** point `COPILOT_DATABASE_URL` at a **persistent hosted Postgres**. Without it the dedupe table is thrown away every run. The workflow runs weekdays 06:00 UTC (plus manual dispatch), with a `concurrency` group so runs never overlap, and reads `COPILOT_ANTHROPIC_API_KEY`, `COPILOT_SMTP_*`, `COPILOT_AUTO_EMAIL`, and `COPILOT_DATABASE_URL` from repo secrets.

## Auto-Reply

When a prospect replies to one of those cold emails, the auto-reply subsystem ([`reply/`](reply/)) reads the reply over **IMAP** and responds **autonomously in your voice** — so a conversation keeps moving even while you sleep. It **fully auto-negotiates** the back-and-forth (answering technical and logistical questions helpfully) with one hard exception, and it only ever talks to people you actually emailed (a sender matching an `OutreachRecord` or an existing `ReplyRecord`).

**Hard guardrails** — this is where the safety lives:

- **Never commits pricing, scope, timeline, or contracts.** If the prospect asks about rate, cost, budget, scope, or a deadline, the bot says it depends on specifics and **proposes a short [cal.com](https://cal.com/) call** with your booking link. It cannot quote a firm number, agree to a fixed scope, or accept an NDA / contract on its own — those decisions stay with you, on the call. (Set `COPILOT_STANDARD_RATE` if you want it to mention a rough ballpark; blank = always defer.)
- **You're BCC'd on every reply.** Every autonomous message copies your inbox (`COPILOT_OPT_OUT_MAILBOX` or your owner email), so you see exactly what went out the moment it's sent.
- **Capped per thread.** After `COPILOT_MAX_REPLIES_PER_THREAD` (default **6**) autonomous replies to one prospect, the bot stops replying to that thread — a hard stop against reply loops.
- **Unsubscribe handling.** A not-interested / "unsubscribe" / "remove" / "stop" / hostile reply is acknowledged in one polite line and the address is appended to the suppression list (`data/suppressed.txt`) so nothing further is sent.
- **Truthful.** It never invents experience beyond your portfolio.

**Detection and response are gated separately.** Reading the inbox is what marks a lead `replied`, which is what stops the follow-up sequence and what the optimizer measures as reply rate — so it must not depend on whether you auto-answer. `COPILOT_REPLY_DETECTION` is **on by default** and sends nothing: it records the inbound, marks the lead replied, and honours opt-outs. `COPILOT_AUTO_REPLY` (**off** by default) is what adds the autonomous answering described above. Sharing one gate meant that with auto-reply off nobody was ever marked replied, so prospects who answered kept receiving follow-up nudges while reply rate read `0.0` forever.

Enable full autonomy by setting `COPILOT_AUTO_REPLY=true` plus your SMTP/IMAP creds (IMAP reuses `COPILOT_SMTP_USER` + `COPILOT_SMTP_PASSWORD`; a Gmail app password works for both), then run a pass manually with `python main.py reply` or on the schedule in [`.github/workflows/reply.yml`](.github/workflows/reply.yml) (every 2h, 08:00–18:00 UTC on weekdays). Claude is only invoked when a real unread reply exists, so an empty inbox pass costs nothing — frequent polling is cheap. Like outreach, the per-thread cap and conversation log live in the DB, so on ephemeral runners point `COPILOT_DATABASE_URL` at persistent Postgres.

> **Still watch your inbox.** This runs *with* you, not instead of you. You're BCC'd on every reply and can jump into any thread at any time — reply directly yourself, and the human touch takes over. The bot deliberately hands off anything about money, scope, or commitment to a call with **you**.

## Dashboard (Mission Control)

Everything the system does is visible in one place — and mirrored as MCP tools so the agents can read the same state (for future self-improvement).

![Dashboard](docs/dashboard.png)

| Page | Shows |
|------|-------|
| **Inbox** | Ranked leads with editable proposal drafts + one-click actions |
| **Pipeline** | Leads grouped by status (drafted → approved → submitted → won/lost) |
| **Outreach** | Every cold email sent — status, replied?, follow-ups |
| **Conversations** | Full reply threads (inbound + the agent's auto-replies) |
| **Analytics** | Funnel (emailed → replied → won), reply rate, Claude cost, emails today |
| **Runs** | Every workflow run — ok/fail, cost, key stats (failures alert you by email) |

Run it: `python main.py dashboard` → `http://localhost:8000`. The same data is exposed to agents via MCP tools: `funnel_stats`, `list_outreach`, `list_conversations`, `run_history`.

**Dashboard authentication.** The UI and every write action are protected by **HTTP Basic auth** (constant-time credential check). Set `COPILOT_DASHBOARD_USER` (default `admin`) and `COPILOT_DASHBOARD_PASSWORD` **before exposing the dashboard on any public URL**. A **blank** `COPILOT_DASHBOARD_PASSWORD` disables auth (convenient for local / SSH-tunnel dev). `/healthz`, `/metrics`, and the HMAC-verified `POST /webhooks/cal` stay open so health checks, Prometheus scrapers, and cal.com keep working. See [`DEPLOY.md`](DEPLOY.md) for a free personal HTTPS deploy on Render.

**Call tracking.** To complete the funnel (emailed → replied → **call booked** → won), add a [cal.com](https://cal.com) webhook for the `BOOKING_CREATED` event pointing at `https://<your-dashboard-host>/webhooks/cal`, and set the shared secret in `COPILOT_CAL_WEBHOOK_SECRET` (the endpoint verifies the `X-Cal-Signature-256` HMAC; leave the secret blank to skip verification in dev). When someone you've emailed books a call, that outreach row is stamped `call_booked_at` and the "Call booked" bar lights up in Analytics. The dashboard must be **publicly reachable** for the webhook to fire — a `localhost` instance won't receive it.

**Booked-call briefings, without a public host** (`calls/`). The webhook above is correct and, on this deployment, had **never fired once** — it needs a reachable dashboard, and the dashboard is deliberately unhosted. So `call_booked_at` was NULL on every row ever written, the KPI report said *0 calls booked* for a month, and on **2026-08-20** a real 15-minute call sat unread in the inbox while every automated surface said nothing had happened. A metric that cannot rise is not a metric.

Detection therefore moved to the one production surface that already works: **the mailbox**. cal.com emails a confirmation for every booking, and the reply pass already logs into that mailbox every two hours with credentials that exist. `python main.py calls` (also run inside every reply pass, and again from the daily monitor so a weekend booking isn't briefed after the call has started) sweeps `FROM cal.com`, stamps `call_booked_at`, and emails a **briefing** — not a notification. A notification is a second thing to go and research; the owner reads email once a day and opens no UI, so the email has to be the whole preparation:

- **WHO** — name, address, and whether they are in the outreach ledger at all.
- **WHY THEY BOOKED** — for a lead we cold-emailed, the job title, company, source, post URL, their own words, and *the pitch an agent sent in your name* (with a warning to read it before joining: contradicting your own email in the first two minutes is the fastest way to lose a warm lead). For an **inbound** booking the purpose is reported as **unknown**, because it is: an invented purpose reads beautifully and walks you into the call confidently wrong. Instead it narrows a personal-mailbox booking to four possibilities (prospect / recruiter / peer / someone selling to you) and gives the one question that separates them in the first 60 seconds.
- **HOW TO HANDLE IT** — the 15 minutes allocated minute by minute, closing on a small paid slice rather than a contract, plus what not to do (no firm price for undefined scope, no unpaid test tasks, no NDA on the call).

**The mail is HTML, so the parser reads HTML.** cal.com's confirmation has **no `text/plain` part** — it is written for humans. Every heuristic here is line-anchored, so on the first real booking the address regex still found the invitee (addresses survive markup) while `When` sat inside `<p style=…>Friday, August 21, 2026</p>` and matched nothing. The stored booking read *"Senthil Govindarajan — ?"* and the briefing told the owner to go and look up the time themselves, for a call the next day. A field left blank for a reason unrelated to the data is indistinguishable from a field the sender omitted. Bodies are now normalised (`<style>`/`<script>` dropped, block ends → newlines, tags stripped, entities unescaped, `&nbsp;` → space) before anything is matched, `text/html` is accepted when there is no plain part, and a booking already stored with a blank field is **backfilled and re-briefed once** — blanks only, so a differently formatted reminder can never silently rewrite a booking the owner has read. Lenient parser at the edge, strict types inside.

**What the first production sweep taught it.** The very first run reported `booked=2` when exactly one booking existed: cal.com's own release notes (*"Changelog: Cal.com v6.8 — Cal Events, new troubleshooter…"*) matched a keyword list that had been loosened to tolerate cal.com's rewording, and a `CALL BOOKED — (unknown) — ?` briefing went out for a call that did not exist. A check that fires for a reason unrelated to the one it exists for is worse than no check. The guard is now **structural**: a booking notification names a **person** who is neither you nor cal.com infrastructure, *and* carries at least one other artifact of a real event (a parsed time, or a link to join it). A changelog has no attendee at all — its only address is a support mailbox, which is discarded before the guard even runs. Two independent signals, either of which may fail, because requiring *both* the person and the time made a formatting change enough to lose a real booking silently — the same brittleness as the keyword list, in the opposite direction. The row the wrong version wrote is deleted by a self-healing purge rather than a manual cleanup step, and `booked=2` was only diagnosable at all because `calls --list` names rows instead of counting them.

**And what the second sweep taught it.** With the changelog gone and the real booking matched, the stored row still read `Senthil Govindarajan — ?`, and the counters still said `briefed=1`. Two bugs, one shape. First, `visible_text` was reading markup correctly but the date regex was **anchored to a weekday** — a presentation choice of cal.com's, not part of the data — so it now falls back to *a month name beside a day and a year, anywhere on the line*, and to a single clock time rather than only a range. Second, and worse: the confirmation ships **both** a `text/html` part and a `text/plain` stub, and `_body_text` preferred plain *whenever it was non-empty*. The stub was non-empty and held no date, so it won, the time vanished, and every counter reported success. The choice is now made on **content, not part order** — if the plain alternative carries no date or time and an HTML part exists, the HTML is read (`calls.parse.when_text` exists to answer exactly that question). A mechanism that reports success while quietly not doing its job is the failure mode this whole section is a monument to. When a time still fails to parse, the sweep logs the *candidate lines* — bounded by construction to a digit, no `@`, no URL, ≤6 words, ≤60 characters, so a public log cannot publish an attendee's name or address while still being enough to fix the format.

Properties worth the name: **read-only against mail** (flags are never touched, so your own unread state survives and a re-read is harmless — idempotency comes from `CallRecord.booking_uid`, the cal.com booking id, not from mail flags); **briefed exactly once**, with `notified` flipping only *after* the send returns True, so an SMTP outage means retried next pass rather than silently lost; a **cancellation is classified before a booking**, because a cancellation email quotes the original event and would otherwise read as a new one and send you to a dead call; and the invitee is resolved as *every address in the body minus yours minus cal.com's*, which survives cal.com relabelling or translating its own template. Set `COPILOT_DETECT_CALLS=false` to switch it off.

**Naming the rows, not just counting them.** On **2026-08-20** a cal.com call was booked the morning after a run that reported `'emailed': 1` — and nothing readable could say *who had been emailed*. Every count was correct and every count was useless: the recipient lived in `OutreachRecord`, the database DSN is a repo secret, the dashboard is deliberately unhosted, and `outreach/sender.py` logged only **failed** sends, so a success left no trace at all. The run log helpfully listed the four *proposed* addresses that were never sent; the one address that mattered was the only one absent.

Two fixes, both aimed at "a call just got booked — which company is that?":

- **Every send now logs its recipient** (`send_outreach: SENT to h***@bactrix.com | subject=…`). Masked, not plain: this repository is **public**, so its Actions logs are public, and printing a prospect's full address would publish their personal data and feed scrapers. The domain answers "which company?" and was published by the poster themselves; the local part is theirs. The failure path was masked too — it had been leaking full addresses into a public log since the day it was written.
- **`python main.py ledger --days 30`** prints one line per contact: masked address, status, `CALL BOOKED` / `replied` / `no reply (N follow-ups)`, plus the company, title, source and the original post URL. It runs on GitHub via the **Outreach Ledger** workflow (`workflow_dispatch`, plus Monday 07:45 UTC), so the answer needs no local machine, no hosted dashboard and no database password. That job is given `COPILOT_DATABASE_URL` and *deliberately no SMTP credentials* — it has no reason to send anything, so it has no way to.

`kpi` says how many; `ledger` says who. A funnel you cannot enumerate is a funnel you cannot work.

## Cost Guardrail

Every pipeline run creates a `CostTracker` seeded with `COPILOT_MAX_USD_PER_RUN` (default **$5.00**). The metered LLM wrapper checks the budget **before** each Claude call and records token usage **after**. When cumulative spend reaches the cap, the next call raises `BudgetExhausted`, the run stops cleanly, and the result is flagged `budget_exhausted: true` — no crash, no surprise bill. Pricing is tracked per model (Opus 4.8 at $5 / $25 per MTok).

## Outcome Reporting

Uptime is not a result. The first production month of this pipeline exited **green on
every scheduled run** while producing 1 email, 0 replies, 0 calls and 0 projects, for
$8.55 — because every reported number was an *activity* count (fetched / new / dropped)
and all of them looked healthy. Activity metrics cannot fail the way this system failed.

So [`monitor/kpi.py`](monitor/kpi.py) reports the funnel in outcome terms over a rolling
window, and names the bottleneck stage:

```bash
python main.py kpi --days 30
```

```text
OUTCOMES — last 30 days
  contactable     41
  emailed         18
  replied         2   (11.1%)
  calls booked    1   (50.0%)
  won             0   (n/a)
  spend           $6.41
  per reply       $3.21

1 call(s) booked, none won yet. The machine is working; the remaining variable is the call itself.
```

Three deliberate choices in there:

- **The verdict is ordered top-down.** An empty top-of-funnel outranks an empty
  downstream stage, because tuning a pitch nobody received cannot change the outcome —
  which is exactly what a month of prompt-tuning against zero contactable leads
  achieved.
- **A rate with no denominator reports `(n/a)`, not `0.0%`.** "0% reply rate" invites
  rewriting a pitch; "n/a" correctly says nobody was emailed.
- **The digest subject carries the outcome.** `[Copilot] NO CONTACTABLE LEADS — sourcing
  needs attention` cannot be mistaken for a normal run in an inbox; `12 drafts queued`
  was, seventeen times.

### Per-source attribution

`sources: 0/46 cleared 70` correctly identifies *targeting* as the bottleneck, and is
still not actionable: with seven sources enabled it does not say which ones produced the
46. "All seven are mediocre" and "six are useless, one is good" give identical totals and
need opposite fixes — re-target everything, or drop six and widen the seventh.

So every run attributes the funnel per source and states a verdict, worst first:

```text
BY SOURCE (worst first — retire what never produces)
  contract_jobs    fetched=0    new=0    contactable=0    queued=0   broken: HTTPStatusError: 400 Bad Request
  hn_freelancer    fetched=0    new=0    contactable=0    queued=0   dead: fetched nothing
  remote_boards    fetched=25   new=25   contactable=0    queued=0   email-blocked: 25 new leads, none with a contact (best score 88) — human-submit channel only
  hn_hiring        fetched=12   new=12   contactable=7    queued=0   off-ICP: best score 68 < 70
```

The verdicts are deliberately distinct failures, not severity grades:

| verdict | meaning | fix |
| --- | --- | --- |
| `broken` | the adapter reported an error | fix the request; the source is fine |
| `dead` | fetched nothing at all | credentials, or the endpoint is gone |
| `starved` | fetched leads, none survived the run cap | raise `max_leads_per_run` |
| `stale` | fetched only leads already in the DB | widen the query, or retire it |
| `unscored` | pre-gated before reaching the model | check the pre-gates |
| `off-ICP` | scored, none cleared the bar | re-target, or lower the bar |
| `no-output` | scores cleared the bar, nothing queued | look between score and queue: contacts, `max_proposals_per_day`, the run budget |
| `email-blocked` | good leads, none carry an address | nothing — use the human-submit queue |

Two properties matter more than the table itself:

- **A row is seeded for every *enabled* source before fetching**, so a source that yields
  nothing still appears. Building the table from returned leads would omit it entirely,
  and an absent row reads as "not a problem" — which is the failure this report exists to
  expose. `contract_jobs` (then named `uk_contract`) sat `DISABLED` through a month of
  green runs.
- **Dead sources sort above working ones, and reach the subject line.** A run that queues
  three drafts *and* has a broken source used to read as unqualified success.
- **A verdict must be falsifiable by the code that emits it.** See below.

### The verdict that made itself true

`email-blocked` used to read `unreachable: leads have no email, so scoring them is wasted
spend`, and it was checked *before* the `unscored` branch. Both facts together made it
unfalsifiable. The pipeline dropped uncontactable leads **before** qualification, so such
a source never produced a score, so `scores` was always empty, so this branch always won —
and it accused the source of wasting scoring spend that had never been spent. The report
was describing a decision the code had made, and presenting it as a property of the market.

What it cost: `contract_jobs` queries ten country endpoints for day-rate contracts and is
the only feed that reports an actual rate. Adzuna publishes no employer address **by
design** — it monetises the redirect click — so 100% of its leads are uncontactable. For
three consecutive runs it was therefore never scored, and the digest recommended retiring
it. That is the feed that surfaced a **Sr Forward Deployed Engineer role at
$208,000–$249,600**, the single best-matched lead the system has ever seen.

Three changes, and the third is the one that generalises:

1. **The gate moved to the stage it names.** `require_contact_before_draft` now blocks
   *drafting*, not scoring. It was skipping the cheap Sonnet qualification in order to
   protect the expensive Opus research+draft — which `route_after_qualify` already gates
   on fit score. Saving the cheap stage to protect the expensive one is a category error,
   and it bought silence at the price of the only signal that could have exposed it.
2. **`unscored` is now checked first**, so "I never looked" can never be reported as a
   finding about the leads.
3. **The run-level bottleneck refuses to blame targeting on a censored sample.** When more
   new leads were withheld from the model than shown to it, `_fit_summary` says so instead
   of printing *"The lead mix is off-ICP; fix targeting, not the threshold."* It printed
   that for three runs over a sample the best-targeted source was excluded from.

The general rule, and the reason this section exists: **a check whose inputs are produced
by the thing it is checking cannot fail.** It is the same shape as the unit test that
asserted `contract_only == 1` against a mock looser than the real server, and as a fit gate
set above any score the scorer can emit. When a report and the code it reports on share a
cause, the report stops being evidence.

### The mirror: a verdict that flattered a source that produced nothing

Fixing an under-crediting verdict immediately produced an over-crediting one, which is
worth recording because the two look nothing alike and are the same mistake.

The run after the fixes above sent the first emails in the project's life (7). It also
reported **two** sources as `productive: 8 cleared 70` — byte-identical strings. One had
queued all 8. The other, `remote_boards`, queued **0**: it had 1 contactable lead out of
22. The verdict was printing `passed` (scores at or above the threshold) as though it were
output, and clearing the bar is not output — a lead can score 90 and still be dropped for
having no address, for hitting `max_proposals_per_day`, or because the run ran out of
money. So the digest congratulated a feed that delivered nothing, in the same words it used
for the one feed that delivered everything.

Hence `no-output`, checked *last* so that `email-blocked` and `off-ICP` keep their more
specific diagnoses, and a productive verdict now leads with the number it can prove:
`productive: 8 queued, 8 of 26 scores cleared 70`.

The same run also reported `bottleneck: none — leads are clearing the bar` while
`budget_exhausted: true` — it had died on its cost ceiling at lead ~123 of 200. The scores
really were healthy; "none" was still false, because the binding constraint was money and
the report is the only place that could say so. A truncated run's score distribution is a
**prefix of what the budget bought**, not a sample of the market, so budget now outranks
every distribution verdict — including the censored-sample one.

### Not every gate failure is a false negative

The seeker filter in `hn_hiring` is the one defect here that was not a reporting problem.
The HN Algolia query pins `author_whoishiring`, and that account posts three monthly
threads with near-identical titles — "Who is hiring?", "Who wants to be hired?" and
"Freelancer? Seeking freelancer?". `query` is *ranked relevance*, not equality, so the
seeker thread came back too. Measured live: **5 of 60 leads were résumés**, and they ranked
13th, 15th, 28th, 37th and 38th — high, because the ranking rewards a published address
plus keyword density, and a CV has both in abundance. One was from an engineer describing
himself as ex-homeless.

`hn_hiring` is the only source that has ever produced a sent email, so this was live: the
system was one scheduled run from cold-pitching freelance DevOps services to unemployed
engineers asking for work. The employer-side override took three attempts against real
data, and both failures are pinned as tests: bare `join` matched "Open to **join**ing
early-stage startups", and `(?:come|to)\s+join` still matched "to **join**ing" for want of
a word boundary. **An override that fires on the phrase it exists to exclude is worse than
no override, because the exclusion still looks like it ran.**

The `starved` verdict exists because the first version of this report **got it wrong on
its first live run**: it counted `fetched` *after* the run cap, and `fetch_all`
concatenates sources in registry order, so the cap took a prefix — everything from source
#1 and nothing from the rest. Six of seven sources were reported `dead: fetched nothing`
when they had never been reached. A wrong verdict is worse than a missing one, because it
gets acted on: the fix would have been to debug six healthy sources. `fetched` is now
counted before the cap, the cap **interleaves** across sources instead of truncating a
prefix, and being cut off by the cap has its own verdict naming its own lever.

The `broken` verdict has the same origin. `contract_jobs` sent Adzuna a filter named
`contract_only`; the real parameter is `contract`, and Adzuna answers an unknown filter
name with an HTTP 400 — so **every request that source ever made failed**. Adapters
swallow transport errors and return `[]` by contract (a bad feed must never abort a run),
which made "the API rejected us" indistinguishable from "no jobs matched": the report said
`dead: fetched nothing` and pointed at the queries, when the fix was one word of code.
Sources now record a `last_error`, and a source that failed is never called dead.

Its unit test asserted `params["contract_only"] == 1` and passed for the life of the bug,
because the mocked HTTP client accepts any parameter name a real server would reject — a
test that pinned the defect it existed to prevent. Renaming the assertion would not have
helped; the mock is now **as strict as the server**, failing on any parameter Adzuna does
not document, so the next invented parameter cannot pass either.

Each run also reports its **fit-score distribution** rather than a bare `dropped: 34`,
since that number has two causes needing opposite fixes: scores clustered just below the
threshold mean the threshold is too strict, scores far below it mean the sources are
off-ICP.

### The two halves of the funnel never met

Ten days of production data (6 weekday runs, 2026-08-10 → 08-17) explained a month of
near-zero sends, and no single metric in the digest had been wrong:

| | 10-day total |
|---|---|
| listings read | 17,905 |
| new leads | 1,047 |
| cleared the fit bar of 70 | **269** (max 97) |
| carried a usable email | **196** |
| **both at once** | **7** |
| emails sent | **6** |

Every number on the left looked healthy. The intersection is the only one that predicts a
send, and nothing computed it. Per source, qualified vs contactable:

| source | qualified (70+) | contactable |
|---|---|---|
| contract_jobs (Adzuna) | 79 | 0 |
| jobicy | 76 | 14 |
| remote_boards | 76 | 1 |
| working_nomads | 17 | 0 |
| contra_startup | 14 | 0 |
| hn_hiring | 7 | **181** |

**Two populations, barely overlapping.** 181 of the 196 addresses came from
`hn_hiring` — whose posts are full-time employment ads, median fit **28** against a bar of
70 — while 186 qualified leads came from job boards that supplied **one** address between
them, because a board monetises the click and routes through an apply form. The per-run
overlap was 1, 4, 0, 0, 0, 2; on the three zero days it was a coin flip over a set of size
one.

This is the project's recurring shape — **a check that cannot fail for the reason it
exists** — at the level of the funnel itself:

* `contactable: 29` was true and reported `[ok]` against a floor of 1, on the third
  consecutive run that emailed nobody. The floor guarded a number that never dropped.
  `check_contactable_supply` now measures **qualified ∩ reachable** and reports both, since
  the gap between them is the diagnosis: zero addresses is a *sourcing* problem, addresses
  attached to the wrong work is a *routing* problem, and they need opposite fixes.
* `bottleneck: contacts — N of M carry no email` was true but stopped one level short of
  the lever. It now names the sources on each side, so "widen the queries" and "lower the
  bar" are visibly ruled out: neither can make a job board publish an address.

Three fixes followed from the diagnosis rather than from guessing:

1. **`outreach/discover.py`** — contactability used to be *"did the post body happen to
   contain an address"*, one regex over text we were handed. Discovery visits the
   **company's own site** and reads the address it publishes. It never guesses a pattern
   (`careers@`, `first.last@`): guessed addresses bounce, and bounces burn the sending
   domain, which is the one asset cold outreach cannot rebuy. It reuses the existing
   contact gate rather than reimplementing it, requires the address's domain to match the
   company's, blocklists aggregators and ATS hosts so a board can never be mistaken for
   the employer, and honours `robots.txt`. How it resolves that company site decides
   whether the result may be *sent* or only *shown* — see
   [Send on evidence, propose on a guess](#send-on-evidence-propose-on-a-guess).
2. **`sources/reddit_forhire.py`, back in the registry** — r/forhire `[Hiring]` posts are
   the one source where a *client* posts *contract* work *with* an address, which is
   precisely the missing overlap. It had been out since 2026-08-03 for a fixable reason:
   `403` on 48/48 unauthenticated fetches. App-only OAuth is free.
3. **`outreach/apply_pack.py`** — the 262 qualified leads with no address have no
   automated route at all (`auto_submit` is permanently off). Listing a link is not a
   hand-off; applying still meant re-reading the post and writing the pitch 262 times. A
   pack makes each one paste-and-submit.

The correction worth recording: an earlier read of two runs concluded *contactability* was
the binding constraint. It wasn't — 196 leads **were** contactable. Two runs could not
distinguish "few addresses" from "addresses on the wrong leads", and those have different
fixes. A sample that cannot separate two hypotheses does not favour either.

### Send on evidence, propose on a guess

Discovery resolves a company's domain two ways, and only one of them is evidence:

* **the post is hosted on the company's own site** — the domain *is* where the listing was
  published, so writing to it is replying to the party that posted;
* **the post is on a job board**, so the domain is derived from the company **name**
  (`"Acme Corp"` → `acme.com` → `.io` → `.ai`) and accepted if the homepage mentions the
  company.

The second path is the *only* one that fires for the 262-lead population discovery was built
for — those leads come from boards by definition. It was also measured the first time it ever
ran, by accident: discovery was unpinned in the offline test suite, a fixture company called
"Acme Corp" resolved the real `acme.com`, fetched its homepage and returned
`frobozz07@mail.acme.com`. A stranger's address, first try, from a unit test. Generic company
names all have a `.com` owner who is not the client.

So the resolution is neither "ship it" nor "delete it". **A name is not an identifier**, and
a guessed address is reported, never mailed: the digest shows it under `POSSIBLE ADDRESSES`
with both the page it came from and the post it was matched to, which is all a human needs to
bin it in two seconds. `discover_send_to_guessed_domains` (default **off**) exists so the
decision can later be revisited from weeks of free proposals the owner has eyeballed, instead
of re-argued from the heuristic. Same shape as `apply_yourself`: automate to the hand-off,
then hand off completely.

Two counters, never one: `discovered` counts addresses usable enough to send to,
`discovery_attempts` counts lookups. `contactable` is deliberately **not** incremented by
either — it means "the listing published an address", and that is the measurement the whole
qualified-vs-reachable diagnosis rests on. Folding discovered addresses into it would erase
the split that exposed the problem while the feeds stayed exactly as unreachable.

### The counter that named nothing

`no_email_pregate` accounted for **851 of 1,047** leads and identified none of them. It is now
four reason codes (`no_address_in_post`, `domain_refused_mail`, `rejected_do_not_contact`,
`rejected_non_hiring`), because each has a different lever and one bucket that big is a fact
without a next step. `find_deliverable_email_with_reason` returns the code the gate actually
took, rather than a caller re-deriving it.

### What already reported success while doing nothing

Two more instances of the project's signature defect, both found in the same pass:

* **The warmup ramp never ramped.** `peak_daily_sends` compared a tz-aware `now` against
  timestamps read back from a plain `DateTime` column, so it raised `TypeError` on every
  call — and `effective_cap`'s blanket `except Exception` (there so a measurement bug can
  never block a send) returned the unramped configured cap and logged nothing. The fallback
  now says **"NO warmup ramp"** in the log line, and a test asserts that wording: a permissive
  fallback that is indistinguishable from a working guard is not a guard.
* **Apply-pack spend was unmetered.** Packs are Opus calls made *after* the lead loop, whose
  `finally` uninstalls the cost tracker — so pack cost was billed to the account, gated by no
  budget and shown in no report. The tracker is re-installed for the pack build and
  `cost_usd` re-read afterwards.

And the mechanism that watches for the next one: `check_discovery_productive` fails when
discovery has attempted lookups across consecutive runs and found **zero** addresses, because
a blocked user-agent, a `robots.txt` change and "companies genuinely publish no address" all
produce the identical `discovered: 0` and a clean exit code. It is gated on *attempts*, so a
run that never needed to look does not count toward the streak — `0 of 0` and `0 of 40` need
opposite fixes.

### What the test suite had to be pinned against

Two production defaults were pinned off in `tests/conftest.py`, for the same reason the Reddit
credentials were: a default that changes what the suite *does* means a green suite that goes
red on a correct production change.

* `COPILOT_DISCOVER_CONTACTS=false` — the acme.com incident above. An offline suite whose
  premise is that it touches no network was making real outbound requests to strangers'
  websites. `tests/test_discover.py` opts back in and supplies its own web via
  `httpx.MockTransport`.
* `COPILOT_APPLY_PACKS=false` — an extra model call per hand-off lead consumed the scripted
  responses of tests counting model calls to prove something else, and a pre-gate test
  asserting "exactly one call" started reading 2.

## Observability

The dashboard exposes a Prometheus `/metrics` endpoint. Key series:

- `copilot_leads_fetched_total`, `copilot_leads_qualified_total`, `copilot_proposals_drafted_total`
- `copilot_proposals_won_total`, `copilot_proposals_lost_total`
- `copilot_claude_cost_usd_total`
- `copilot_fit_score`, `copilot_proposal_quality`, `copilot_rag_retrieval_seconds` (histograms)

A ready-made Grafana board (leads/day, fit-score distribution, win rate, Claude cost) lives at [`monitoring/grafana-dashboard.json`](monitoring/grafana-dashboard.json). In Kubernetes, [`k8s/servicemonitor.yaml`](k8s/servicemonitor.yaml) wires the Prometheus Operator to scrape `/metrics`.

## Learning Loop

When you mark a lead **won** in the dashboard, `record_outcome` flips its status, stamps the proposal, and **embeds the winning proposal back into the RAG knowledge base** as a `kind: "win"` document. Because the Proposal Writer retrieves proof points from that same store, future drafts start citing what has actually closed — the system compounds on its own wins. Marking a lead **lost** records the outcome without touching the KB.

## Inbound Content Engine

Beyond outbound proposals, a small content engine drafts **inbound** marketing from the same portfolio KB — LinkedIn posts, case studies, and gig descriptions — so the pipeline that wins clients also helps attract them:

```bash
python main.py content --kind case-study --topic "Kubernetes cost optimization"
```

## Repository Structure

```text
ai-freelance-copilot/
├── main.py                     # CLI entrypoint (run / dashboard / mcp / build-kb / stats / content)
├── pipeline.py                 # integration core: fetch → graph → queue (never submits)
├── config.py                   # pydantic-settings (safe, offline defaults)
├── costs.py                    # CostTracker + per-run budget guardrail
├── core/
│   ├── schemas.py              # shared Pydantic contracts (Lead, ScoredLead, ...)
│   └── state.py                # LangGraph CopilotState
├── agents/
│   ├── graph.py                # LangGraph orchestrator (qualify→research→write→review)
│   ├── llm.py                  # metered Claude wrapper + offline FakeChat
│   ├── qualifier.py            # fit scoring (Sonnet)
│   ├── researcher.py           # enrichment (Sonnet)
│   ├── proposal_writer.py      # RAG-grounded drafting (Opus)
│   ├── compliance.py           # deterministic review gate
│   └── followup.py             # polite nudge drafts
├── sources/                    # read-only lead adapters + registry
├── outreach/                   # auto-email channel: extract → discover → pitch → send (gated, deduped, opt-out)
│                               #   discover.py = find a published address; apply_pack.py = the hand-off
├── monitor/funnel.py           # output-based health checks (a green run that reaches nobody is a failure)
├── reply/                      # auto-reply: inbox (IMAP) → respond (guardrailed) → sender → runner
├── rag/                        # embedder, vector store, retriever, ingest, learning loop
├── db/                         # SQLAlchemy models + session
├── observability/metrics.py    # Prometheus metrics (no-op if absent)
├── interfaces/
│   ├── dashboard.py            # FastAPI approval dashboard + /metrics + /healthz
│   ├── notify.py               # email / WhatsApp digest
│   └── mcp_server.py           # FastMCP stdio server for AI clients
├── content/                    # inbound content engine (posts / case studies / gigs)
├── scripts/build_kb.py         # build the portfolio RAG KB
├── monitoring/grafana-dashboard.json
├── k8s/                        # deployment, cronjob, service, configmap, secret, servicemonitor
├── docs/architecture.drawio
├── tests/                      # offline test suite (no API key, no network)
├── Dockerfile · Makefile · requirements.txt · pyproject.toml
└── .github/workflows/            # ci.yml · outreach.yml (scheduled auto-email) · reply.yml (scheduled auto-reply)
                                  # ledger.yml (who was contacted, read-only, on demand)
```

## Prerequisites

- **Python 3.11+**
- An **Anthropic (Claude) API key** — *optional*: the system runs and tests pass fully offline without one (deterministic embedder + `FakeChat`). A key is only needed for live drafting.
- *(Optional)* SMTP credentials for the email digest, or a WhatsApp Business Cloud API token.
- *(Optional)* Docker + a Kubernetes cluster for deployment.

## Quickstart

```bash
# 1. Clone and enter the repo
git clone https://github.com/suryaanandan1995-dotcom/ai-freelance-copilot.git
cd ai-freelance-copilot

# 2. Create a virtual environment and install
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Build the portfolio knowledge base (offline, no API key)
python -m scripts.build_kb

# 4. Run one pipeline pass (discover → qualify → research → draft → queue)
python main.py run

# 5. Serve the approval dashboard, then open http://localhost:8000
python main.py dashboard
```

To receive digests, copy `.env.example` to `.env` and configure SMTP (`COPILOT_SMTP_HOST`, `COPILOT_SMTP_USER`, …) or the WhatsApp variables, and set `COPILOT_NOTIFY_CHANNEL`. Add `--notify` to `python main.py run` to send one after a run.

## Configuration

All variables are prefixed `COPILOT_` (see [`.env.example`](.env.example)).

| Variable | Default | Description |
|----------|---------|-------------|
| `COPILOT_DATABASE_URL` | `sqlite:///copilot.db` | Storage DSN (use PostgreSQL in prod). |
| `ANTHROPIC_API_KEY` | _empty_ | Claude API key (live runs only). |
| `COPILOT_MODEL_OPUS` | `claude-opus-4-8` | Strong model for drafting. |
| `COPILOT_MODEL_SONNET` | `claude-sonnet-4-6` | Cheap model for scoring/triage. |
| `COPILOT_MAX_USD_PER_RUN` | `5.0` | Hard Claude-spend cap per run. Raised from $2.00 after run `31172835060` **hit it and truncated** at lead ~123 of 200. Sized from the measured $0.016428/lead, so the ceiling can actually fund a full `MAX_LEADS_PER_RUN` run (200 x $0.016428 = $3.29) with headroom for a fully-drafting one. Weekdays-only schedule → ≤23 runs/month, so ≤$115/month worst case, ~$72 expected. |
| `COPILOT_MIN_FIT_SCORE` | `70` | Leads below this are dropped. |
| `COPILOT_MAX_LEADS_PER_RUN` | `200` | Max leads processed per run. Raised from 50 after a run fetched **186** leads and scored **50**. The original justification — $0.13 of a $2.00 ceiling, so cost was not binding — **no longer holds**: removing the pre-gate `continue` means uncontactable leads are now scored, and per-lead cost went $0.0026 → **$0.016428** (6.3x). A lead cap the spend ceiling cannot fund is decorative, so `MAX_USD_PER_RUN` was raised with it. |
| `COPILOT_MAX_PROPOSALS_PER_DAY` | `15` | Anti-spam daily draft cap. |
| `COPILOT_DRY_RUN` / `COPILOT_ALLOW_SEND` | `true` / `false` | Safety flags — auto-send is never enabled. |
| `COPILOT_NOTIFY_CHANNEL` | `email` | `email` · `whatsapp` · `none`. |
| `COPILOT_DASHBOARD_BASE_URL` | `http://localhost:8000` | Base URL used for links in digests. |
| `COPILOT_SMTP_HOST` … `COPILOT_NOTIFY_EMAIL_TO` | _empty_ | SMTP digest configuration. |
| `COPILOT_WHATSAPP_TOKEN` / `_PHONE_ID` / `_TO` | _empty_ | WhatsApp Business Cloud API. |
| `COPILOT_RAG_STORE_PATH` | `data/portfolio_kb.json` | Vector store path. |
| `COPILOT_OWNER_*` | Surya A | Identity used in proposals/signature. |
| `COPILOT_ADZUNA_APP_ID` / `_APP_KEY` | _empty_ | Free Adzuna API credentials ([developer.adzuna.com](https://developer.adzuna.com)) enabling the UK day-rate contract source. Unset = that source is skipped. |
| `COPILOT_VERIFY_CONTACT_DOMAIN` | `true` | Require the contact domain to publish MX/A records before sending. Protects sender reputation — the one asset cold outreach cannot rebuy. Set `false` only for offline tests/dev. |
| `COPILOT_REQUIRE_CONTACT_BEFORE_DRAFT` | `true` | Drop leads with no reachable email **before** spending Opus tokens drafting to them. |
| `COPILOT_ALERT_AFTER_ZERO_EMAIL_RUNS` | `3` | Consecutive zero-email runs before the health monitor alerts. Also the number of runs that must have *attempted* discovery before `check_discovery_productive` will call a zero yield a fault. |
| `COPILOT_DISCOVER_CONTACTS` | `true` | Visit the company's own site for a published address when the post carries none. Makes real outbound HTTP, honours `robots.txt`, and is pinned **off** in the test suite. |
| `COPILOT_MAX_CONTACT_DISCOVERIES_PER_RUN` | `40` | Lookup budget per run, counted on **attempts** not hits — a cap that counted only successes would let a run where nothing is findable fetch every lead's worth of pages. |
| `COPILOT_DISCOVER_SEND_TO_GUESSED_DOMAINS` | `false` | When the domain was guessed from the company *name* rather than published by the post, mail it. Off: those addresses are proposed in the digest instead. See [Send on evidence, propose on a guess](#send-on-evidence-propose-on-a-guess). |
| `COPILOT_APPLY_PACKS` | `true` | Draft a paste-ready application pack for each qualified lead automation cannot reach. Pinned **off** in tests. |
| `COPILOT_MAX_APPLY_PACKS_PER_RUN` | `5` | Packs per digest, best fit first. Each is one Opus call, metered against `MAX_USD_PER_RUN`. |
| `COPILOT_CAL_WEBHOOK_SECRET` | _empty_ | HMAC secret for the cal.com booking webhook; blank skips verification (dev only). |

## Deployment

**Docker**

```bash
docker build -t ghcr.io/suryaanandan1995-dotcom/ai-freelance-copilot:latest .
docker run --rm -p 8000:8000 --env-file .env \
  ghcr.io/suryaanandan1995-dotcom/ai-freelance-copilot:latest
```

The image runs as a non-root user and serves the dashboard by default.

**Kubernetes** ([`k8s/`](k8s/))

```bash
kubectl apply -f k8s/configmap.yaml
cp k8s/secret.example.yaml k8s/secret.yaml   # fill in real values, do not commit
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/deployment.yaml -f k8s/service.yaml
kubectl apply -f k8s/cronjob.yaml             # Mon–Fri `python main.py run --notify`
kubectl apply -f k8s/servicemonitor.yaml      # Prometheus scrape of /metrics
```

The **Deployment** serves the always-on approval dashboard; the **CronJob** runs the weekday (Mon–Fri) discovery pass and emails a digest. Both run read-only-rootfs, non-root, with all capabilities dropped.

**Scheduled auto-email outreach.** To run the [auto-email channel](#auto-email-outreach) on a schedule, add `--auto-email` to the recurring command (`python main.py run --auto-email --notify`) and set `COPILOT_AUTO_EMAIL=true` + SMTP creds. Because dedupe/cap state must survive across runs, use a **persistent SQLite file** (cron/Kubernetes CronJob on an always-on box) or a **persistent hosted Postgres `COPILOT_DATABASE_URL`** (GitHub Actions [`outreach.yml`](.github/workflows/outreach.yml), since its runners are ephemeral).

## CI/CD

GitHub Actions runs on every push and pull request to `main` ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)):

1. Checkout (`actions/checkout@v4`)
2. Set up Python 3.11 (`actions/setup-python@v5`)
3. `pip install -r requirements.txt`
4. `ruff check .`
5. `pytest -q`

The workflow declares `permissions: contents: read` (least privilege). The full test suite is deterministic and runs **offline** — no API key, no network.

## License

Released under the [MIT License](LICENSE).

## Author

**Surya A** — DevSecOps + AI Infrastructure Engineer

- Email: suryaanandan1995@gmail.com
- LinkedIn: https://www.linkedin.com/in/surya-devsecops/
