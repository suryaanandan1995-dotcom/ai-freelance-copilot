#!/usr/bin/env bash
# Private dashboard + webhook tunnel.
#
# Runs the dashboard bound to 127.0.0.1 ONLY (never public), then opens a
# Cloudflare quick tunnel so cal.com can POST to /webhooks/cal over HTTPS. The
# dashboard UI stays login-gated (this script refuses to run without a password),
# so the tunnel effectively exposes only the safe endpoints (/webhooks/cal,
# /healthz). View the UI yourself via SSH tunnel:  ssh -L 8000:localhost:8000 <you>@<host>
#
# ⚠️  Run this on a PERSONAL machine/VM — NOT a corporate one. An outbound tunnel
#     from company infra can be flagged by corporate security / DLP.
set -euo pipefail
cd "$(dirname "$0")/.."

# one-time venv
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt psycopg2-binary
fi

# load .env
set -a; . ./.env; set +a

# refuse to expose an unauthenticated dashboard
: "${COPILOT_DASHBOARD_PASSWORD:?Set COPILOT_DASHBOARD_PASSWORD in .env before exposing a tunnel}"

# dashboard: localhost only (private)
.venv/bin/python main.py dashboard --host 127.0.0.1 --port 8000 &
APP_PID=$!
trap 'kill "$APP_PID" 2>/dev/null || true' EXIT
sleep 4

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not found. Install it (one-time):"
  echo "  curl -L -o cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
  echo "  chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/"
  exit 1
fi

echo ">>> Dashboard is private on http://127.0.0.1:8000 (login required)."
echo ">>> Copy the https URL below and set the cal.com 'Booking created' webhook to  <url>/webhooks/cal"
echo ">>> (Ctrl-C stops both the tunnel and the dashboard.)"
cloudflared tunnel --url http://127.0.0.1:8000
