# Project Ultron

Personal AI assistant for macOS. Gemini Live voice, desktop control, vision,
web GUI (`web_gui/`), optional local LLMs via LM Studio.

Run:

```bash
.venv/bin/python ultron_hub.py
# GUI: http://127.0.0.1:8766
```

## Wake state

Ultron boots **dormant**: no Gemini Live session exists and no audio leaves the
browser. The GUI listens locally (Web Audio) for a **double clap** or the wake
word **"Ultron"** (also: dock mic button, click the orb, or just start typing).
Waking plays a metallic chime, opens the live session, and spins the Brain Core
up. Sleep again with Esc, holding the mic button, or telling Ultron to
"stand by" (`sleep_ultron` tool). Idle sessions auto-sleep after 3 minutes.
`ULTRON_AUTO_WAKE=1` restores the old always-on behaviour.

## GUI

Single-stage command centre — no tabs, no panels, no labels. State is the orb's
colour/motion (steel = dormant, cyan = listening, amber = thinking, violet =
speaking, red = error). Content renders as title-free glass widgets: the model
composes them via `render_ui_widget` (MetricCard / DataChart / ListGroup /
KeyValue / TextBlock / ImageTile / VisionFeed) and clears them with
`clear_ui_widget`. Camera, screen and 3D (SVE) feeds appear as widgets when
live; transcript and tool activity fade in as widgets and expire on their own.

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
