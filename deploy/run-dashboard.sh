#!/usr/bin/env bash
# Run the mission-control dashboard on this server (foreground).
# For a persistent service, prefer the systemd unit (see DEPLOY.md).
set -euo pipefail
cd "$(dirname "$0")/.."

# one-time venv
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt psycopg2-binary
fi

# load .env (KEY=VALUE lines)
set -a; . ./.env; set +a

exec .venv/bin/python main.py dashboard --host 0.0.0.0 --port 8000
