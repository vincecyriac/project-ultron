#!/usr/bin/env bash
# FRIDAY remote access — Tailscale Serve setup.
#
# Exposes the local FRIDAY GUI (127.0.0.1:8766) and WebSocket gateway
# (127.0.0.1:8765) over HTTPS *inside your tailnet only*. Nothing is
# published to the public internet. The hub keeps listening on localhost;
# Tailscale Serve terminates TLS and proxies in.
#
# One-time prerequisites:
#   1. Install the Tailscale app on this Mac and sign in.
#   2. Install Tailscale on your phone, sign in to the SAME account.
#   3. In the Tailscale admin console (https://login.tailscale.com/admin/dns):
#      enable MagicDNS and HTTPS Certificates (first run below will prompt too).
#
# Then run:  ./setup_remote.sh
# Phone usage: open the printed https://... URL, tap once to unlock audio, talk.

set -euo pipefail

TS="tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
  if [ ! -x "$TS" ]; then
    echo "ERROR: tailscale CLI not found. Install the Tailscale app first." >&2
    exit 1
  fi
fi

if ! "$TS" status >/dev/null 2>&1; then
  echo "ERROR: Tailscale is not running/signed in. Open the Tailscale app and log in." >&2
  exit 1
fi

echo "Configuring Tailscale Serve mappings..."
# GUI at https://<machine>.<tailnet>.ts.net/
"$TS" serve --bg --https=443 http://127.0.0.1:8766
# WebSocket gateway mounted at /ws on the same origin
"$TS" serve --bg --https=443 --set-path=/ws http://127.0.0.1:8765

echo
"$TS" serve status
echo
HOSTNAME_FQDN=$("$TS" status --json | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
if [ -n "$HOSTNAME_FQDN" ]; then
  echo "FRIDAY remote URL:  https://${HOSTNAME_FQDN}/"
  echo "Open this on your phone (Tailscale app connected), add to home screen."
fi
echo
echo "Reminder: keep the Mac awake while away, e.g.:  caffeinate -imsu &"
echo "To stop sharing:  $TS serve reset"
