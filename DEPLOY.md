# Deploy the dashboard on your Linux server

The dashboard is a FastAPI app. Running it on your always-on Linux server gives you
(a) a live UI to view leads/outreach/analytics, and (b) a public endpoint for the
**cal.com "call booked" webhook** so the funnel completes automatically.

Everything below is **free**. Run the commands yourself (they start a long-running
service + expose a URL, so they're yours to authorize).

---

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
