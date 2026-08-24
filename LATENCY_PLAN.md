# JARVIS Latency Plan — Speed Up "Thinking" on Simple Tasks

## Current latency map (one simple turn, voice)
| Stage | Cost | Notes |
|-------|------|-------|
| Whisper STT (local, CPU) | 1–4s | voice only; text turns skip |
| `brain.think()` → `_think_router` | ~4s | 9router `cx/gpt-5.4-mini`, but sends ~2.7k-token tool schema + `tool_choice=auto` + 10-iter loop **even for "2+2?"** |
| `tts_to_b64` (edge-tts, network) | 1–3s | **dominant cost for short replies** — a 1-word answer still pays full MP3 round-trip |
| Total simple turn | ~6–11s | no streaming anywhere |

Root causes:
1. **edge-tts is a per-reply network call** — unavoidable latency for tiny answers.
2. **Tool schema + tool loop sent on every call** — wasted tokens/decision time on trivial intents.
3. **No fast path for trivial intents** — "hello", "2+2", "what time is it" all go through the full cloud+TTs pipeline.
4. **No streaming** — browser waits for full reply + full TTS before any audio.

## Plan (ordered by impact / risk)

### P1 — Simple-task fast path (highest impact, low risk)
Add a `classify_intent(text)` that detects trivial intents **before** the brain:
- math/arithmetic → evaluate locally (safe `ast.literal_eval` over a whitelist)
- greetings / "how are you" / thanks → canned instant reply (no model, no TTS network if cached)
- time/date → `datetime` local
- "repeat/stop/help" → local handlers
If trivial: return immediately, bypass `_think_router` AND bypass edge-tts when a **local TTS** is available.
*Why:* removes the ~4s cloud call + ~2s TTS for the majority of "simple tasks" the user mentioned. Should drop simple turns to <1s (math/time) or <2s (greeting w/ local TTS).

### P2 — Local TTS fallback (removes edge-tts network dependency)
edge-tts requires internet every call. Add a **local** TTS option:
- Prefer `pyttsx3` (offline, espeak) OR `Coqui TTS` if already installed; fall back to edge-tts only if no local engine.
- For trivial-intent replies (P1), use local TTS so there is **zero network** in the hot path.
*Risk:* voice quality lower than edge-tts; make it configurable (`JARVIS_TTS=local|edge|auto`).
*Why:* eliminates the 1–3s edge-tts round-trip that dominates short replies.

### P3 — Drop tools for trivial calls (medium impact, trivial risk)
In `_think_router`, only attach `tools`/`tool_choice` when `classify_intent` says the query *might* need a tool (web/search/calc-via-tool). For simple chat, send `tools=None`. Also lower `max_tokens` to ~256 for simple intents.
*Why:* shaves the ~2.7k-token tool prefix + the "should I call a tool?" decision on every simple turn.

### P4 — Streaming TTS (perceived-speed win, higher effort)
Replace `tts_to_b64` (whole-reply-then-send) with chunked edge-tts / local streaming: send audio in `audio-chunk` WS messages as phrases are synthesized, so JARVIS *starts speaking* before the full answer exists.
*Why:* cuts perceived latency even when the answer is long. More work (WS protocol change + HUD audio-queue). Do **after** P1–P3 prove the gains.

### P5 — Warm/cheap primary for simple tasks (optional)
Route trivial intents to the fastest backend (local `jarvis-qwen3` is already warm via `_preload_ollama`, ~4–7s though; or a tiny model). Given P1 handles most, this is low priority.

## Recommended sequence
1. **P1 + P3** together (small, safe, big win) — implement, restart, verify with timing probes.
2. **P2** (local TTS) — implement behind `JARVIS_TTS` flag, verify voice quality acceptable.
3. **P4** (streaming) — only if P1–P3 still feel slow on long answers.

## Verification approach (no existing test suite)
- Add `TURN_STATS["latencies"]` already exists — log wall-time per stage (STT / think / TTS) via `time.perf_counter()` around each in `jarvis_web.py`.
- Ad-hoc probe: WS text turns for "2+2", "hello", "what time is it"; assert reply + measure `wall` < target (e.g. <2s for math/time, <3s greeting).
- Confirm `last_backend` reflects fast path (e.g. `"local"` / `"instant"`) on `/status` or a new telemetry field.
- Keep current `cx/gpt-5.4-mini` as the fallback for non-trivial queries (unchanged chain).

## Explicit non-goals / risks
- Not touching the cloud chain for complex queries — only adding a fast *pre-filter*.
- Local TTS voice quality trade-off — must be user-accepted before defaulting.
- Arithmetic eval must be sandboxed (whitelist chars) to avoid code-exec risk.
