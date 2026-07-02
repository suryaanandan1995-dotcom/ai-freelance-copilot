#!/usr/bin/env bash
# Generate a self-signed TLS cert for the dashboard, valid for this host's
# internal IP + localhost. Self-signed is fine for a private/internal tool:
# it encrypts traffic; your browser shows a one-time "not trusted" warning you
# accept (or import deploy/certs/dashboard.crt into your machine to trust it).
#
# The private key is written to deploy/certs/ which is gitignored — never commit it.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p certs

IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
IP="${IP:-127.0.0.1}"

openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout certs/dashboard.key -out certs/dashboard.crt \
  -subj "/CN=${IP}" \
  -addext "subjectAltName=IP:${IP},IP:127.0.0.1,DNS:localhost"

chmod 600 certs/dashboard.key
echo "Self-signed cert generated for ${IP} -> deploy/certs/dashboard.crt (key: deploy/certs/dashboard.key)"
