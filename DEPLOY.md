# Deploy the dashboard on your Linux server

The dashboard is a FastAPI app. Running it on your always-on Linux server gives you
(a) a live UI to view leads/outreach/analytics, and (b) a public endpoint for the
**cal.com "call booked" webhook** so the funnel completes automatically.

Everything below is **free**. Run the commands yourself (they start a long-running
service + expose a URL, so they're yours to authorize).

> ## ⚠️ Host it on a PERSONAL machine/account — never a corporate one
> **Do NOT deploy this on an employer/corporate machine or network.** A public
> dashboard + inbound webhook running on corporate infra will be flagged by
> corporate security and violates most acceptable-use policies. Use a personal
> free platform (**Render** / **Fly.io**) or a **personal** cloud account you own,
> off any employer network. And **set `COPILOT_DASHBOARD_PASSWORD`** before you
> expose the URL — a blank password disables auth and leaves every page public.

---

# Option D — Private dashboard + webhook-only tunnel (most security-clean)

Keep the dashboard **fully private** (bound to `127.0.0.1`, login-gated, viewed via
SSH tunnel) and expose **only** the cal.com webhook via a short-lived Cloudflare tunnel.

```bash
# in .env: set a strong COPILOT_DASHBOARD_PASSWORD (+ the usual secrets)
./deploy/tunnel.sh          # runs dashboard on 127.0.0.1 only + prints an https tunnel URL
```
- View the UI yourself:  `ssh -L 8000:localhost:8000 <you>@<host>` → open `http://localhost:8000`.
- Point cal.com `Booking created` at the printed `https://…/webhooks/cal` (same `COPILOT_CAL_WEBHOOK_SECRET`).
- The UI never leaves localhost; only the HMAC-verified webhook + `/healthz` are reachable through the tunnel.

> ⚠️ Even a tunnel is outbound traffic from the host — run this on a **personal** machine/VM, not the corporate jumpserver. If you must use the corp box, bring the tunnel up only while testing, then Ctrl-C it.

---

# Option C — Render.com (free, personal HTTPS, recommended)

The fastest way to get a personal HTTPS URL with zero server admin. Render builds
the repo's `Dockerfile`, runs the `/healthz` healthcheck, and serves the app over
automatic TLS at `https://<your-app>.onrender.com`. A `render.yaml` Blueprint ships
in the repo root.

1. Sign up for a **personal** [Render](https://render.com) account (not a work one).
2. In Render: **New → Blueprint** and connect this GitHub repo (or **New → Web
   Service** → pick the repo → Runtime: **Docker**). Render reads `render.yaml`.
3. Render builds the `Dockerfile` and deploys it as a **free** web service.
4. Set the environment variables (they're declared `sync: false`, so you enter the
   values in the Render dashboard — nothing secret is committed):
   `COPILOT_ANTHROPIC_API_KEY`, `COPILOT_DATABASE_URL` (your Neon Postgres URL),
   `COPILOT_SMTP_HOST` / `COPILOT_SMTP_USER` / `COPILOT_SMTP_PASSWORD`,
   `COPILOT_CAL_WEBHOOK_SECRET`, and **`COPILOT_DASHBOARD_USER` +
   `COPILOT_DASHBOARD_PASSWORD`** (set a strong password — this is a public URL).
5. Render gives you `https://<your-app>.onrender.com`. Point the cal.com webhook at
   `https://<your-app>.onrender.com/webhooks/cal` with the same secret.

> Free Render web services sleep after inactivity and cold-start on the next
> request; that's fine for a personal dashboard + webhook receiver.

---

# Option A — Docker + Nginx (recommended)

The repo ships a `Dockerfile`, `docker-compose.yml`, and `nginx/default.conf`. The
dashboard runs in a container behind an Nginx reverse proxy. **Validated: the image
builds and every page (`/`, `/analytics`, `/runs`, `/strategy`, `/webhooks/cal`, …) serves.**

### 1. Prereqs
- Docker + Docker Compose on the server.
- A populated `.env` in the repo root (`COPILOT_ANTHROPIC_API_KEY`, `COPILOT_SMTP_*`, `COPILOT_DATABASE_URL` = your Neon URL, `COPILOT_CAL_WEBHOOK_SECRET`).

### 2. Bring it up
```bash
cd /home/surya/github-portfolio/ai-freelance-copilot
docker compose up -d --build
docker compose ps            # app + nginx should be "running"
curl -s localhost/healthz    # -> {"status":"ok"}
```
Nginx serves the dashboard on **port 80**; the app container is internal only.

### 3. Make it reachable for the cal.com webhook
Open port 80 (and 443 for TLS) in your GCP firewall, or front it with a tunnel.
- **HTTP (quick test):** point cal.com at `http://<server-ip>/webhooks/cal` if cal.com allows http.
- **HTTPS (recommended):** get a domain → issue a cert with **certbot** (`certbot certonly --standalone -d your-domain.com`), drop `fullchain.pem`/`privkey.pem` into `nginx/certs/`, uncomment the `443` block in `nginx/default.conf` + the `443:443` port in `docker-compose.yml`, then `docker compose up -d`. Point cal.com at `https://your-domain.com/webhooks/cal`.
- **No domain?** Run `cloudflared tunnel --url http://localhost:80` for a free HTTPS URL (see Option B step 4).

### 4. Operate
```bash
docker compose logs -f app       # tail app logs
docker compose restart app       # after a config/.env change
docker compose pull && docker compose up -d --build   # update
docker compose down              # stop
```

> The container uses your `COPILOT_DATABASE_URL` (Neon), so the dashboard shows exactly what the GitHub Actions workflows write — same data, one source of truth.

---

# Option B — systemd (no Docker)

## 1. One-time setup (on the server)
```bash
cd /home/surya/github-portfolio/ai-freelance-copilot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt psycopg2-binary
# make sure .env has COPILOT_ANTHROPIC_API_KEY, COPILOT_SMTP_*, COPILOT_DATABASE_URL (your Neon url),
# and COPILOT_CAL_WEBHOOK_SECRET (any long random string you'll also paste into cal.com)
```

## 2. Run it as a background service (systemd --user)
```bash
mkdir -p ~/.config/systemd/user
cp deploy/copilot-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now copilot-dashboard
loginctl enable-linger "$USER"        # keep it running after you log out
systemctl --user status copilot-dashboard   # confirm it's active
```
The dashboard is now on `http://localhost:8000` on the server.
(No systemd? Use `nohup ./deploy/run-dashboard.sh > data/dashboard.log 2>&1 &` instead.)

## 3. View the dashboard from your laptop (no public exposure)
SSH tunnel — safest way to see the UI without opening any port:
```bash
ssh -L 8000:localhost:8000 <you>@<server>
# then open http://localhost:8000 in your laptop browser
```

## 4. Public HTTPS URL for the cal.com webhook (free, no domain)
cal.com needs a public **https** URL to POST bookings to. The zero-config free way is a Cloudflare quick tunnel:
```bash
# install cloudflared (one-time)
curl -L -o cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/
# start a tunnel to the dashboard (prints a https URL like https://xyz.trycloudflare.com)
cloudflared tunnel --url http://localhost:8000
```
Copy the printed `https://…trycloudflare.com` URL.

> Prefer a stable URL? Use a named Cloudflare Tunnel, or open GCP firewall port 8000 + put a
> reverse proxy (Caddy auto-HTTPS) in front — but the quick tunnel is fine to start.

## 5. Point cal.com at it
In **cal.com → Settings → Developer → Webhooks → New**:
- **Subscriber URL:** `https://<your-tunnel>.trycloudflare.com/webhooks/cal`
- **Event triggers:** `Booking created`
- **Secret:** the same value as `COPILOT_CAL_WEBHOOK_SECRET` in your `.env`

Now when a prospect books a call, the dashboard marks that lead **Call booked** and it
shows in the Analytics funnel (emailed → replied → call booked → won).

---

## Notes
- The dashboard reads the **same Neon DB** the cloud workflows write to (set `COPILOT_DATABASE_URL`), so you see everything the automation does.
- Restart after config changes: `systemctl --user restart copilot-dashboard`.
- Stop it: `systemctl --user disable --now copilot-dashboard`.
- The quick-tunnel URL changes each restart; update the cal.com webhook if you restart the tunnel (or use a named tunnel for a fixed URL).
