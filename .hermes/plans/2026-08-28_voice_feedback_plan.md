# JARVIS Voice State Feedback — "Talk while it works"

> Date: 2026-08-28. Status: PLAN (not implemented). Owner: JARVIS demo.
> Goal: when JARVIS is transcribing, thinking, delegating, or executing, the user
> hears a short, friendly cue about what is happening — without becoming chatty
> or delaying the real answer.

## 1. Current state (what already exists)

| Signal | Source | Spoken today? |
|---|---|---|
| `status: transcribing/thinking` | jarvis_web WS | NO (HUD badge only) |
| `progress` ("Delegating to Hermes…", "Hermes finished.") | warm_harness progress_cb → `_progress_reader` | YES (spoken + TTS'd per message) |
| Hermes ack ("On it, sir, working on that now…") | delegate background ack | YES (part of response) |
| Job transitions (queued/running/done/error/timeout) | jobs registry listener → `_broadcast_job` | only terminal states, via `memory_hygiene.proactive_job_announcement` |
| Autonomous step lines ("[STEP 1] ✓ …") | autonomous progress | NO (HUD only) |
| Needs-confirm gate | tools NEEDS_CONFIRM | YES (it is the response) |

Gaps: silence during transcription and thinking; no mid-execution milestones;
no rate-limiting/priority policy; cue audio synthesized on demand (Kokoro ~1.4s)
which can lag and overlap; duplicated announcements possible (progress reader +
job listener both fire on Hermes completion).

## 2. Design principles

1. **Quiet by default, informative by escalation.** A cue is spoken only if the
   state will outlast a threshold (e.g. thinking > 3s) or changes the user's
   decision (confirm needed, task failed).
2. **Never delay the answer.** Cues are short pre-cached clips; the response
   audio always preempts the cue queue.
3. **One voice at a time.** Server emits cues through a single policy engine;
   HUD plays them through one sequential queue (no overlapping clips).
4. **Dedup + cooldown.** Same cue text within 20s is dropped; non-terminal
   cues ≥ 8s apart; terminal events always pass.
5. **User control.** `JARVIS_VOICE_FEEDBACK=essential|full|off` (default:
   essential). "What's your status?" always works regardless.

## 3. Trigger → cue map (friendly lines, Kokoro, cached)

| Trigger | Condition to speak | Cue (spoken) | Priority |
|---|---|---|---|
| `status: transcribing` | always (clip received) | (no voice — HUD earcon) — silence here is fine | HUD only |
| `status: thinking` | router still running at **+4s** | "Thinking, sir." | low |
| `status: thinking` | still at **+12s** | "Still working on it." | low (escalation) |
| Hermes delegate ack | immediate | existing "On it, sir, working on that now…" | normal |
| Hermes milestone (warm_send longer tasks) | every ~45s, max 3/task | "Still on it — <short note>." | normal |
| Autonomous step line | each verified step, throttled | "Step one done." / "Step two, done." | normal |
| Confirm gate | immediate | (the NEEDS_CONFIRM text itself) | high |
| Job done | terminal | existing "Finished, sir — <task>." | high |
| Job error/timeout | terminal | "That didn't go through, sir — <reason>." | high |
| Confirm-gate pending > 5 min | once | "Still waiting on your confirm, sir." | normal |

## 4. Implementation pieces

1. **New `voice_feedback.py` (policy engine, stdlib-only).**
   - `announce(event_kind, text, priority)` — applies dedup/cooldown/escalation,
     returns the line to speak or None.
   - Priority levels: `high` (terminal/gate) > `normal` > `low`; a higher
     priority clears the queue; `low` drops if anything pending.
   - Quiet-hours + `off` mode respected. State kept in-process (no persistence).
2. **Cue cache (`server_tts_cache/`).** At startup, pre-synthesize the ~10
   standard cues with Kokoro into WAV; serve from disk → zero synth latency for
   the common path. Non-cached dynamic lines (task summaries) synthesize live.
3. **jarvis_web wiring.**
   - `_speak_proactive` → route through `voice_feedback.announce()`.
   - `_progress_reader` → same policy engine (prevents overlap with job events).
   - New timers in the WS turn handler: thinking-threshold cues via
     `asyncio.sleep`-based watchdog (cancel when response starts).
   - `on_vad_stop`/`on_vad_detect_stop` hooks from RealtimeSTT (Tier-1 STT
     integration) reuse the same channel for "Got it." style micro-cues.
4. **HUD (jarvis_visual.html).**
   - Sequential playback queue for `{"type":"cue"}` messages; earcon for
     transcribing; captions area already displays progress text.
   - New WS message type: `{"type":"cue","text":...,"audio":...,"priority":...}`
     (reuses the existing proactive flag pattern).
5. **Autonomous step speaking.** `autonomous` progress lines already reach the
   job registry; extend `proactive_job_announcement` to map
   `running`+`[STEP N] ✓` notes to short spoken lines (throttled).
6. **Config (config.py).** `JARVIS_VOICE_FEEDBACK` (essential/full/off),
   `JARVIS_VOICE_THINKING_CUE_S=4`, `JARVIS_VOICE_MILESTONE_S=45`.

## 5. Phasing

- **Phase A — core cues (half-day).** Policy engine + cue cache + thinking
  watchdog + dedup between progress/job channels. Test: thinking > 4s speaks
  "Thinking, sir."; Hermes ack unchanged; done/error lines unchanged.
- **Phase B — milestones (half-day).** Autonomous step lines + Hermes
  mid-task milestones + pending-confirm reminder.
- **Phase C — polish.** Music ducking while speaking (SD ducking or pause),
  voice speed tuning, per-cue voice options, barge-in respect.

## 6. Testing

- Unit: policy engine (dedup, cooldown, priority, off-mode) — pure functions.
- Live WS: scripted turns asserting cue emission on (a) slow router turn,
  (b) Hermes delegation, (c) autonomous goal, (d) error job; assert response
  audio is never blocked by cues (ordering check).
- Latency budget: cue path must not add > 100 ms to the response path
  (cache hit = disk read only).

## 7. Risks

- **Chattiness** → policy engine default `essential` + cooldowns; easy off switch.
- **Kokoro thread-safety** → single ONNX session; cue synth only at startup,
  dynamic lines serialized through one worker thread.
- **Double announcements** → progress channel and job listener both route
  through the same policy engine (single choke point).
