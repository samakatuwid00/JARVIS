# PHONE_PLAN.md — use JARVIS from the Android phone to control this PC

Status: planned, not started (2026-09-12).

## Goal

Open JARVIS on the Android phone, talk to it, and have it act on the PC —
at home or on mobile data. The brain, tools and apps stay on the PC; the
phone is one more HUD client.

## Not in scope

- Controlling the phone itself (listing or opening phone apps). Parked until
  JARVIS is stable on the desktop. When it comes back, the cheap route is
  ADB over wireless debugging (`adb shell pm list packages -3`, `am start`)
  as new tools, not a native Android app.
- The Electron desktop app (parked separately). Phase 0 here is shared work.
- Always-on listening on the phone. A browser tab cannot listen with the
  screen off or in the background; on the phone it is tap-to-talk.

## What already works

- The HUD derives its server from the page URL
  (`jarvis_hud_v3.html:1341`, `location.host`), so a phone that opens the
  right address connects without code changes.
- Command audio is sent to the server and transcribed there by Whisper
  (`mtype == "audio"`, `jarvis_web.py:1697`), so the phone mic feeds the
  same path.
- Replies are TTS clips sent to the client that asked (`tts_b64`), so the
  phone speaks the answer. The server-side `sd.play` in
  `voice_engine.py:202` belongs to the CLI (`jarvis.py`), not the web server.
- Android Chrome ships Web Speech, so the browser wake word and live
  transcript work on the phone while the page is open and the screen is on.

## Problems to fix first

- **`/ws` has no origin check** (`jarvis_web.py:1550`, `websocket.accept()`).
  Browsers do not apply CORS to WebSockets, so any web page open in any
  browser on a device that can reach the server can connect and send
  commands. The HTTP middleware (`jarvis_web.py:47`) covers `/apps`,
  `/shadow`, `/prefs` only.
- **Server binds `0.0.0.0`** (`jarvis_web.py:2148`) with no login.
- **Firewall allowed Python inbound on Public networks** for three
  interpreters, including `pythoncore-3.14-64\python.exe` (the one running
  JARVIS). Still enabled — disabling needs an admin shell; see "Firewall"
  below.
- **HUD layout is desktop-only**: three-column grid
  (`260px | 1fr | 260px`), one breakpoint at 1180px.
- **`broadcast_wake` sends to every HUD** (`jarvis_web.py:120`): a wake the
  PC's server-side WakeEngine hears would open capture on the phone too.

## Phase 0 — Security (in the current web app, testable in Chrome)

- [x] Bind `127.0.0.1` by default; `JARVIS_HOST` env overrides it.
      (2026-09-12. Port comes from `.env`: `JARVIS_PORT=8001`.)
- [x] `/ws`: reject connections whose `Origin` host does not match the
      `Host` header, unless listed in `JARVIS_ALLOWED_ORIGINS`
      (comma-separated, e.g. the Tailscale HTTPS name). No `Origin` at all
      (non-browser clients such as `ws_verify.py`) is still accepted.
      Rejected with HTTP 403; server logs `[WS] rejected origin ...`.
- [x] Host allowlist (after the ship gate flagged DNS rebinding): every HTTP
      request and `/ws` must name `127.0.0.1`, `localhost`, `[::1]` or a
      host from `JARVIS_ALLOWED_ORIGINS`. A rebinding page's
      Host+Origin pair used to match itself; now HTTP 400 / WS 403.
- [x] Firewall: disable the 6 Public-profile Python allow rules (admin).
      Done 2026-09-12 via a UAC-elevated PowerShell.

Done when: Chrome on the PC works as before; a page on another origin
(e.g. a local `python -m http.server` page) cannot open `/ws`; port 8000 is
not reachable from another device on the LAN.

## Phase 1 — Access through Tailscale

- [x] Install Tailscale on the PC (`winget install tailscale.tailscale`) and
      the Android phone; sign in with the same account on both (manual).
      Done 2026-09-12: PC `niko` (100.69.4.42), phone `poco-x7-pro`.
- [x] `tailscale serve --bg 8001` — gives
      `https://niko.taild2b1d6.ts.net`, tailnet-only (not Funnel). HTTPS is
      what lets the phone browser use the mic. Serve and HTTPS certificates
      had to be enabled once in the Tailscale admin. Survives reboots
      (`tailscale serve status` to check; `tailscale serve --https=443 off`
      to remove).
- [x] Add that origin to `JARVIS_ALLOWED_ORIGINS` (in `.env`).
      Verified from the PC: page 200 over HTTPS (first hit ~20 s while the
      certificate was issued), `/ws` accepted with the ts.net Origin,
      rejected with a foreign one.

The server can stay on `127.0.0.1`: `tailscale serve` proxies from the PC
itself, so no firewall rule or router change is needed.

Side effect: proxied requests arrive from `127.0.0.1`, so the
"this computer only" pages (`/shadow`, `/prefs`) become visible to the
phone. Acceptable for your own phone.

Done when: the phone on mobile data (Wi-Fi off) loads the HUD over the
`ts.net` address and the mic permission prompt appears.

## Phase 2 — Phone HUD

- [x] Single-column layout under 700px; side columns hidden (no drawer yet);
      `#jv-talk` tap-to-talk button fixed at the bottom. (2026-09-12)
- [x] Web app manifest (`/manifest.webmanifest`) + icons
      (`static/icon-192.png`, `static/icon-512.png`, served at `/icon-*.png`).
      No service worker yet; check whether Android Chrome offers "Install".
- [x] Server-side wakes go only to the PC's own HUD: clients with
      `X-Forwarded-For` (tailscale serve) or a non-loopback `Host` land in
      `WS_REMOTE` and `broadcast_wake` skips them. The HUD also ignores
      `type: wake` when `IS_MOBILE`.
- [x] Phone HUD skips the Web Speech wake listener (`IS_MOBILE`, UA or
      coarse pointer under 900px). Finding: on Android Chrome the
      recognizer never starts while the page holds a getUserMedia stream —
      no error, no beep, status looked normal. Tap-to-talk opens the same
      Whisper command capture; second tap ends the clip early.

Verified with Playwright (Android UA, 400x800, fake mic): no horizontal
overflow, columns hidden, status "TAP TALK, THEN SPEAK", tap -> "LISTENING…",
no `[wake]` console lines. Desktop at 1400px unchanged.

Done when: at ~400px wide nothing overflows horizontally; the home-screen
icon opens JARVIS without browser chrome.

## Phase 3 — Test

- [ ] Phone on mobile data: "open Spotify" opens it on the PC; reply is
      spoken on the phone.
- [ ] Round-trip latency from end of speech to start of reply, phone vs PC.
- [ ] PC HUD and phone HUD open at the same time: a wake at the PC opens
      capture only on the PC; a command from the phone runs once.
- [ ] Reconnect: lock and unlock the phone, switch Wi-Fi to mobile data;
      the HUD reconnects on its own.

## Open questions

- Which headers `tailscale serve` forwards (does the backend see the
  `ts.net` `Host`, and the `Tailscale-User-Login` identity header?). Decides
  whether the origin check compares against `Host` or a fixed allowlist.
  Check once Tailscale is installed.
- Whether Chrome on Android still requires a service worker for install.
- Is LAN access from other devices wanted at all once Tailscale exists?
  Plan assumes no.

## Firewall

Six inbound "python.exe" Allow rules on the Public profile (TCP and UDP for
`pythoncore-3.14-64`, `local\python\bin` and uv's `cpython-3.11.15`). A
non-admin attempt on 2026-09-12 failed with "Access is denied". Disable them
from an **administrator** PowerShell; Private-profile rules stay as they are:

```powershell
Get-NetFirewallRule -DisplayName python.exe |
  Where-Object { $_.Profile.ToString() -eq 'Public' -and $_.Action.ToString() -eq 'Allow' } |
  Disable-NetFirewallRule
```

To restore:

```powershell
Get-NetFirewallRule -DisplayName python.exe |
  Where-Object { $_.Profile.ToString() -eq 'Public' -and $_.Action.ToString() -eq 'Allow' } |
  Enable-NetFirewallRule
```
