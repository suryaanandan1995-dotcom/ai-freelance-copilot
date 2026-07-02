#!/usr/bin/env bash
# Serve the dashboard on this host's INTERNAL IP — reachable from machines on the
# same private network / VPC only (this box has no public IP). Login-gated.
#
# Access it from your workstation at:  http://<internal-ip>:8000
# (You may need a VPC firewall rule allowing TCP 8000 from your internal ranges.)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt psycopg2-binary
fi

set -a; . ./.env; set +a

# Refuse to serve on the network without a password (the UI would otherwise be open).
: "${COPILOT_DASHBOARD_PASSWORD:?Set COPILOT_DASHBOARD_PASSWORD in .env before serving on the internal network}"

IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
IP="${IP:-0.0.0.0}"

echo ">>> Dashboard: http://$IP:8000  (login: $COPILOT_DASHBOARD_USER / your password) — internal network only"
exec .venv/bin/python main.py dashboard --host "$IP" --port 8000
