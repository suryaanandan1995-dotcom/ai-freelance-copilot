# Getting Started — AI Freelance Copilot

A practical, do-this-in-order guide to running the copilot and turning on fully
hands-off weekday outreach. No prior context needed.

> **What it does:** every weekday it discovers freelance leads, scores them
> against your skills, researches the client, and **drafts a tailored proposal**
> from your portfolio. For leads that publicly posted a **contact email** (mainly
> Hacker News "who's hiring"), it can **send a short pitch automatically**
> (rate-limited, deduped, opt-out). Everything else waits in a dashboard for a
> one-click submit. **It never auto-submits to Upwork/LinkedIn** — that gets
> accounts banned.

---

## 0. One-time: prerequisites

- Python 3.11+, git.
- An **Anthropic API key** — [console.anthropic.com](https://console.anthropic.com) → API Keys → add ~$5–10 credit. (Running cost ≈ **$11–16/month**.)

---

## 1. Run it locally (5 min, no sending)

```bash
git clone https://github.com/suryaanandan1995-dotcom/ai-freelance-copilot
cd ai-freelance-copilot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit **`.env`** and set your key (real secrets ONLY go in `.env`, never `.env.example`):

```
COPILOT_ANTHROPIC_API_KEY=sk-ant-...
```

Then:

```bash
python -m scripts.build_kb          # builds the RAG knowledge base from your repos
python main.py run                  # discover -> qualify -> draft (no emails)
python main.py dashboard            # open http://localhost:8000
```

You'll see ranked leads and editable proposal drafts. This is the whole system,
minus sending. Play with it until you're happy.

> Some days the generic boards surface few DevSecOps roles — that's normal (it's
> selective). Lower the bar with `COPILOT_MIN_FIT_SCORE=60` in `.env` to see more.

---

## 2. Turn on fully-automated weekday outreach (≈10 min)

This makes it run in the cloud on its own (GitHub Actions, free) and email
email-reachable leads. **Three steps.**

### 2a. Gmail App Password
- [myaccount.google.com/security](https://myaccount.google.com/security) → turn on **2-Step Verification**.
- [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) → create one named `copilot` → copy the **16-char** code, **remove the spaces**.

### 2b. Free Postgres (so it never emails the same person twice across runs)
- Sign up at [neon.tech](https://neon.tech) (free) → create a project → copy the connection string:
  `postgresql://user:password@host/dbname`

### 2c. Add the secrets and enable the schedule
```bash
R=suryaanandan1995-dotcom/ai-freelance-copilot
gh secret set COPILOT_SMTP_HOST     --repo $R --body "smtp.gmail.com"
gh secret set COPILOT_SMTP_USER     --repo $R --body "suryaanandan1995@gmail.com"
gh secret set COPILOT_SMTP_PASSWORD --repo $R --body "<16-char app password, no spaces>"
gh secret set COPILOT_DATABASE_URL  --repo $R --body "<your neon postgresql:// url>"
gh workflow enable outreach.yml     --repo $R
```
*(`COPILOT_ANTHROPIC_API_KEY` is already set. The workflow already passes `COPILOT_AUTO_EMAIL=true`.)*

Done. It now runs **every weekday at 06:00 UTC (07:00 London)**, fully unattended.

Test it immediately without waiting for the schedule:
```bash
gh workflow run outreach.yml --repo $R
gh run watch --repo $R
```

---

## 3. Your daily routine (~10 min, only when there's interest)

- **Replies land in your inbox** — engage only with people who actually reply.
- Optional: open the dashboard to review **Upwork/LinkedIn** leads (no email → manual) and one-click submit the ones you like. You can ignore these entirely if you only want the email channel.
- When a client hires/replies positively, mark the lead **Won** in the dashboard → the system learns and writes better proposals next time.

---

## 4. The knobs (all in `.env`, or repo secrets/vars for the cloud)

| Setting | Default | What it does |
|---|---|---|
| `COPILOT_AUTO_EMAIL` | `false` | Master send gate. Nothing emails unless this is `true` **and** SMTP is set. |
| `COPILOT_MAX_EMAILS_PER_DAY` | `8` | Daily send cap (protects your domain reputation + stays legal). |
| `COPILOT_OUTREACH_MIN_FIT` | `80` | Only email leads scoring at/above this. |
| `COPILOT_MIN_FIT_SCORE` | `70` | Leads below this are dropped entirely. |
| `COPILOT_MAX_PROPOSALS_PER_DAY` | `15` | Anti-spam cap on drafts. |
| `COPILOT_MAX_USD_PER_RUN` | `2.0` | Hard Claude-spend ceiling per run. |
| `COPILOT_OPT_OUT_MAILBOX` | *(owner email)* | Where "unsubscribe" replies go. |

**Cheaper:** set the proposal writer to Sonnet by exporting
`COPILOT_MODEL_OPUS=claude-sonnet-4-6` (~half the cost), and/or drop
`MAX_PROPOSALS_PER_DAY`.

---

## 5. Safety & legality (why it's built this way)

- **Never auto-submits to Upwork/LinkedIn** — no API for it, and it's a ToS
  violation → permanent ban. A human submits those (one click).
- **Email outreach is low-volume, B2B, opt-out** — only to people who publicly
  posted a contact address looking to hire (UK PECR / GDPR legitimate interest),
  always with your real identity and an easy unsubscribe. Keep the cap low.
- **Secrets** live in `.env` (gitignored) or GitHub repo secrets — never in
  `.env.example` (that file is public).
- Anyone who replies `unsubscribe` → add their email to `data/suppressed.txt`
  (one per line) and they're never contacted again.

---

## 6. Pause / stop

```bash
gh workflow disable outreach.yml --repo suryaanandan1995-dotcom/ai-freelance-copilot   # stop the schedule
# or just set COPILOT_AUTO_EMAIL secret to "false" to keep drafting but stop sending
```

---

**Contact:** Surya A — suryaanandan1995@gmail.com · [linkedin.com/in/surya-devsecops](https://www.linkedin.com/in/surya-devsecops/) · [book a call](https://cal.com/surya-devsecops/15min)
