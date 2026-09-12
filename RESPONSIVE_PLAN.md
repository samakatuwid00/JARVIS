# RESPONSIVE_PLAN.md — make the JARVIS HUD work at every screen size

Status: planned, not started (2026-09-12). Follows PHONE_PLAN.md Phase 2,
which only added a first single-column pass under 700px.

## What the screenshots showed

Playwright at 360x780, 400x860 (Android UA), 860x400 (phone landscape),
768x1024 (tablet), 1024x700 (small laptop). Files in the session scratchpad
`shots/`.

| Size | Problem |
|---|---|
| All | `html, body { overflow: hidden }` (`jarvis_hud_v3.html:28`): anything below the fold is unreachable. Nothing scrolls. |
| All | Height uses `100vh`. On Android the browser bar makes `100vh` taller than the visible area, so the bottom gets hidden. |
| Phone portrait | Mostly works. Transcript box is a fixed 140px and sits low; the space above it shows the background artifact's desktop layout, cropped (orb cut in half, "GOOD EVENING, SIR…" text bleeding through). |
| Phone portrait | Side panels are hidden entirely, so the phone has no Active Tasks (job progress), Now Playing (Stop / Spotify), Voice Feedback mode, or System Status. |
| Phone portrait | Apps button is 27px tall (touch target should be 44px+). Input placeholder is cut off ("…TYPE, THEN PRE"). |
| Phone landscape 860x400 | Desktop 3-column layout. Transcript starts at y=554 on a 400px screen: completely off-screen. TALK covers the middle. Topbar status wraps to 4 lines; pills and Apps button run off the right edge. |
| Tablet 768x1024 | Desktop 3-column squeezed. Transcript is a narrow box at the bottom; TALK sits on top of the input. Left/right columns cut off at the bottom. Topbar overflows (Voice/FB pills and Apps off-screen). |
| Small laptop 1024x700 | Apps button clipped at the right edge. Transcript's bottom half and the lower panels are below the fold with no scroll. |
| Apps modal (phone) | Usable. Close button small; tab row "Shadow review" cut off. |

## Constraints

- `hud_artifact.html` (the animated background, loaded in `#hud` iframe) is a
  minified bundle and is kept untouched by design. It can only be sized,
  scaled, dimmed or hidden from the outside.
- All the layout lives in `jarvis_hud_v3.html` (CSS + the markup IDs). No
  framework; changes are CSS plus a little JS.
- Desktop at 1280px+ must look the same as today.

## Layout per size

Breakpoints by width, plus one height rule for landscape phones.

### Desktop, 1100px and wider
Keep the 3 columns. Fixes only:
- Columns scroll inside themselves (`overflow-y: auto`) instead of being cut.
- Topbar never overflows: Brain pill truncates with an ellipsis; Voice and FB
  pills hide below 1280px; Apps button always visible.
- Status text on one line with an ellipsis.

### Tablet and small laptop, 701–1099px
Two regions instead of three columns:
- Topbar (one row, compact).
- Transcript in the middle, full width, filling the remaining height.
- The side panels move into a **panel drawer** (see below), opened from a
  topbar button. On a wide enough screen (landscape tablet) the drawer can sit
  docked on the right instead of overlaying.

### Phone portrait, 700px and narrower
Full-height column using `100dvh`:
1. Compact topbar: brand, status dot + one-line status, Online dot, Apps and
   Panels as 44px icon buttons.
2. Transcript fills all remaining space (no fixed 140px), newest line at the
   bottom, scrolls.
3. Input row.
4. Bottom dock with TALK (centered), respecting the Android gesture bar
   (`env(safe-area-inset-bottom)`).

### Phone landscape (height 500px or less, any width)
- Topbar shrinks to one thin row.
- Transcript takes the left side, full height.
- TALK docks on the right edge, vertically centered, so it never covers text.

### Panel drawer (phone, tablet, landscape)
The existing side sections (`#job-tray`, `#jv-np`, `#jv-vf`, `#jv-stats`,
`#jv-left`) move into one drawer on smaller screens, as a bottom sheet on
phones and a right-side sheet on tablets. Active Tasks and Now Playing first
(what you need while away from the PC), diagnostics last. Same DOM nodes,
moved by CSS/JS, so every existing script that updates them keeps working.
A badge on the Panels button shows the running-task count.

### Background artifact
Options (decision needed, see bottom):
- **A. Hide on phones, keep a light orb.** Hide the `#hud` iframe under
  700px and in landscape phones; the wrapper's own `#jv-glow` / `#jv-rings`
  state animation stays. Saves battery (the artifact animates constantly).
- **B. Scale it.** Render the iframe at a fixed 1280x800 and CSS-scale it to
  fit, so it looks like the desktop HUD shrunk down. Keeps the look; tiny
  text, same battery cost.
- **C. Dim it harder.** Keep as is, raise the backdrop dim on small screens
  so the cropped text stops bleeding through.

Tablet and desktop keep the artifact as today.

## Touch and details (all touch devices, `pointer: coarse`)
- Every button at least 44x44px (Apps, drawer, music, voice-feedback,
  modal close).
- Shorter input placeholder on phones ("Type to JARVIS…").
- Input font 16px on touch screens (avoids zoom-on-focus in some browsers).
- When the on-screen keyboard opens, the input row stays visible
  (`100dvh` + `visualViewport` resize handling if needed).
- Apps modal on phones: full screen, 44px close button; apps_panel tab row
  scrolls horizontally instead of cutting off.

## Steps

1. **Foundation** — replace `100vh` with `100dvh` (with `100vh` fallback),
   let columns/drawer scroll internally, one-line status, topbar overflow
   rules. Fixes desktop/small-laptop clipping on its own.
   *Done 2026-09-12.* Shell has a fixed `dvh` height with rows
   `auto minmax(0, 1fr)`; columns scroll; status is one line with the full
   text in `title`; brain pill ellipsizes; Voice/FB pills hide below
   1280px. Probe: 1280/1400/1920 boxes identical to before; status 1 line
   and Apps on-screen at every size; 1024x700 columns scroll instead of
   clipping. TALK still covers the input at 768px and in landscape (step 4).
2. **Phone portrait** — full-height column, flexible transcript, bottom dock,
   44px targets, placeholder, background option (decision).
   *Done 2026-09-12.* Under 700px: `#hud` hidden (soft orb gradient on
   `#stage` instead), topbar on two rows (brand, dot, Online, Apps / status),
   transcript fills down to the TALK dock. Touch screens: 44px controls,
   16px input. Viewport meta adds `interactive-widget=resizes-content`
   (Android keyboard shrinks the layout, so the input stays visible) and
   `viewport-fit=cover`. Phone placeholder "TYPE TO JARVIS…". Probe at
   360/400: transcript 569/649px tall (was 140), TALK clear of the input,
   no target under 44px. 1280+ identical; non-touch 1024 identical to
   step 1. Keyboard behaviour still needs the real phone.
3. **Panel drawer** — markup for the sheet + Panels button + task badge;
   move the side sections into it under 1100px.
   *Done 2026-09-12.* Below 1100px the columns collapse and `placePanels()`
   moves the section nodes into `#jv-drawer` in the order Active Tasks,
   Now Playing, Voice Feedback, System Status, Last Turn, Language Model;
   at 1100px+ they return to their columns in the original order. Right
   sheet (380px) on tablets, bottom sheet (82dvh) on phones; closes on the
   x, the backdrop or Escape. Panels button mirrors `#jv-taskcount` as a
   badge. `positionStats()` no longer anchors System Status to the
   artifact's THREAT ASSESSMENT block (it vanished whenever the artifact
   lacked it, and on phones); `#jv-stats` now takes wheel/touch so its
   column scrolls under it. Only desktop change: System Status at 1280px
   is 300px wide (its column) instead of 287px.
4. **Tablet + landscape** — two-region layout, docked drawer on wide
   tablets, landscape rule with TALK on the right.
   *Done 2026-09-12.* The two-region layout came with step 3 (columns
   collapse below 1100px). Touch tablets in portrait keep a 112px dock
   under the input for TALK. Touch screens 500px tall or less (phone
   landscape): artifact hidden, thin topbar, transcript fills the height,
   TALK docked on the right edge. Checked at 768x1024, 1024x768 (touch),
   860x400, 915x412 (POCO landscape), 400x860: TALK overlaps neither the
   input nor the transcript. Non-touch and desktop sizes identical to
   step 3. Docked drawer on wide tablets skipped: the overlay sheet
   already works at 1024px.
5. **Apps modal** — full screen on phones, tab row scroll.
   *Done 2026-09-12.* Under 700px the Apps window fills the screen; on
   touch screens its frame takes the space under the 44px close button
   instead of assuming a 54px head. In `apps_panel.html` the view tabs stay
   on one line and scroll sideways below 640px. Same commit fixes a step 3
   regression: the Panels button pushed the phone topbar to three rows
   (83 to 114px); Panels and Apps are now 44px icon-only buttons on phones
   (inline SVG, no CDN), with the task badge on the Panels icon. Topbar
   back to 83px. Checked at 360x780 and 400x860; all sizes above 700px
   identical to step 4.

All five steps done. Still to do on the real phone: keyboard open with
the input focused, drawer, Apps window, a full voice turn.

Each step is its own commit, testable in Chrome devtools device mode.

## How it gets checked
- Playwright matrix: 360x780, 400x860, 860x400, 768x1024, 1024x768,
  1024x700, 1280x800, 1400x900, 1920x1080. For each: no horizontal overflow,
  transcript and input fully inside the viewport, TALK not overlapping the
  input or text, every drawer section reachable, touch targets 44px+ on
  touch sizes, and 1280px+ screenshots unchanged from today.
- Real phone (POCO X7 Pro) over Tailscale: portrait, landscape, keyboard open,
  drawer, Apps modal, a full voice turn.

## Decisions (2026-09-12)
1. Background on phones: **A** — hide the `#hud` iframe under 700px and on
   landscape phones; keep the wrapper's `#jv-glow` / `#jv-rings`.
2. Panel drawer: **all sections**, ordered Active Tasks, Now Playing,
   Voice Feedback, System Status, Language Model.
