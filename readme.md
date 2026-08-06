# Project Ultron

Personal AI assistant for macOS. Gemini Live voice, desktop control, vision,
web GUI (`web_gui/`), optional local LLMs via LM Studio.

Run:

```bash
.venv/bin/python ultron_hub.py
# GUI: http://127.0.0.1:8766
```

## Remote access (phone / other devices)

Ultron is reachable from your phone over your private Tailscale network —
nothing is exposed to the public internet. The hub only ever listens on
`127.0.0.1`; Tailscale Serve terminates HTTPS and proxies in, so access
requires a device signed in to your tailnet.

One-time setup:

1. Install Tailscale on the Mac and on your phone, sign in to the same account.
2. Run `./setup_remote.sh` (configures Tailscale Serve: GUI at `/`,
   WebSocket gateway at `/ws`).
3. On the phone, open the printed URL, e.g.
   `https://vinces-macbook-pro.tailc596b4.ts.net/`, and add it to the home
   screen for one-tap access.

Usage notes:

- First tap on the page unlocks mic + audio (mobile browser autoplay rules).
- Ultron's voice plays through the phone only — the Mac's speakers and mic are
  muted automatically while a remote session is connected.
- The phone camera is the primary camera during a remote session (rear camera
  by default; Flip button switches front/rear). The Mac webcam and screen are
  used only when explicitly requested ("use the Mac camera / laptop screen").
- Keep the Mac awake while away: `caffeinate -imsu &` (or Amphetamine).
- Stop sharing: `tailscale serve reset`.

### Remote command approval

While any remote (Tailscale) client is connected, every
`execute_shell_command` / `execute_applescript_task` call is held until you
approve it on a connected device — an approval card pops up in the GUI with
the exact command and Run/Deny buttons. Denied or unanswered (45s) commands
are never executed. Local-only sessions behave as before (no prompt).
