"""router_health.py - which 9router models can answer right now.

9router fronts many providers, and on 2026-09-12 most were down at once:
antigravity out of quota for four days (503 "Unavailable (reset after 95h
47m)"), github / cline / kilocode unlicensed or paid, gemini-cli 403 for a
few minutes, and cursor answering 200 with no text at all. Cygnus tried the
same dead models on every turn and ended on the slow local model.

This keeps a small health table, filled by a background probe every few
minutes and by the brain's own failed calls (mark_unhealthy), so the chain
can ask healthy_models() on every turn without waiting on the network. A
model counts as healthy only when it returned real text: an empty 200 is a
failure. friendly() and the notice functions word "which model is answering"
for the user.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time

PROBE_EVERY_S = float(os.getenv("JARVIS_ROUTER_PROBE_S", "300"))
PROBE_TIMEOUT_S = 20.0
DEFAULT_COOLDOWN_S = 300.0
EMPTY_COOLDOWN_S = 1800.0          # a provider that answers with nothing is broken, not busy
# Tried after the configured ROUTER_MODEL + ROUTER_FALLBACK_MODELS, best first.
EXTRA_CANDIDATES = [m.strip() for m in os.getenv(
    "JARVIS_ROUTER_CANDIDATES",
    "gc/gemini-2.5-flash,gc/gemini-3-flash-preview,gc/gemini-2.5-pro,gc/gemini-2.5-flash-lite,"
    "cu/claude-4.5-sonnet,cu/gemini-3-flash-preview,gh/gpt-5.4-mini,cl/anthropic/claude-sonnet-4.6"
).split(",") if m.strip()]

_lock = threading.Lock()
_state: dict[str, dict] = {}       # model -> {"ok", "until", "latency", "reason", "ts"}
_thread = None
_last_switch_notice = 0.0


def candidates() -> list[str]:
    """Every model worth trying, in the owner's order: configured first."""
    try:
        from config import ROUTER_MODEL, ROUTER_FALLBACK_MODELS
        configured = [ROUTER_MODEL] + list(ROUTER_FALLBACK_MODELS)
    except Exception:
        configured = []
    seen, out = set(), []
    for m in configured + EXTRA_CANDIDATES:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def healthy_models(now: float | None = None) -> list[str]:
    """Models that answered with real text at their last check and are not
    cooling down, best first. Instant: no network. [] until the first probe."""
    _ensure_probing()
    now = now or time.time()
    with _lock:
        return [m for m in candidates()
                if _state.get(m, {}).get("ok") and now >= _state[m].get("until", 0)]


def _cooldown(reason: str) -> float:
    m = re.search(r"reset after\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", reason or "", re.I)
    if m and any(m.groups()):
        h, mi, s = (int(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + s
    return EMPTY_COOLDOWN_S if "empty reply" in (reason or "") else DEFAULT_COOLDOWN_S


def mark_unhealthy(model: str, reason: str = "") -> None:
    """A live call to `model` failed (the brain calls this on 503 / 429 /
    errors / empty replies): skip it until its reset time, or five minutes."""
    now = time.time()
    with _lock:
        _state[model] = {"ok": False, "until": now + _cooldown(reason),
                         "reason": " ".join(str(reason).split())[:160], "ts": now,
                         "latency": None}


def mark_healthy(model: str, latency: float | None = None) -> None:
    with _lock:
        _state[model] = {"ok": True, "until": 0, "reason": "", "ts": time.time(), "latency": latency}


def probe_once(models: list[str] | None = None) -> dict:
    """Ask each model not cooling down for one short sentence, in parallel."""
    import concurrent.futures as cf
    from openai import OpenAI
    from config import ROUTER_BASE_URL, ROUTER_API_KEY
    client = OpenAI(base_url=ROUTER_BASE_URL, api_key=ROUTER_API_KEY or "x",
                    max_retries=0, timeout=PROBE_TIMEOUT_S)
    now = time.time()
    with _lock:
        todo = [m for m in (models or candidates()) if now >= _state.get(m, {}).get("until", 0)]

    def check(model):
        start = time.time()
        try:
            r = client.chat.completions.create(
                model=model, max_tokens=40,
                messages=[{"role": "user", "content": "Reply with one short sentence: are you there?"}])
            text = (r.choices[0].message.content or "").strip() if r.choices else ""
            if text:
                mark_healthy(model, round(time.time() - start, 2))
            else:
                mark_unhealthy(model, "empty reply")
        except Exception as e:
            mark_unhealthy(model, str(e))

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(check, todo))
    return snapshot()


def snapshot() -> dict:
    with _lock:
        return {m: dict(v) for m, v in _state.items()}


def _loop():
    while True:
        try:
            probe_once()
        except Exception as e:
            print(f"[ROUTER-HEALTH] probe failed: {e}", flush=True)
        time.sleep(PROBE_EVERY_S)


def _ensure_probing() -> None:
    """Start the background probe once per process (never under pytest)."""
    global _thread
    if _thread is not None or os.getenv("PYTEST_CURRENT_TEST") or "pytest" in sys.modules \
            or os.getenv("JARVIS_ROUTER_PROBE", "1") == "0":
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="router-health")
    _thread.start()


# ------------------------------------------------------------------ wording

def friendly(model: str) -> str:
    """'ag/gemini-3.8-flash-low' -> 'Gemini 3.8 Flash', 'ag/claude-sonnet-4-6'
    -> 'Claude Sonnet 4.6', 'gh/gpt-5.4-mini' -> 'GPT-5.4 Mini'."""
    name = str(model or "").split("/")[-1].lower()
    if name.startswith("jarvis-qwen") or name.startswith("qwen"):
        return "the local Qwen model"
    name = re.sub(r"-(?:extra-low|low|medium|high|preview|agent|thinking|max|review)\b", "", name)
    name = re.sub(r"(\d)-(\d)(?=$|-)", r"\1.\2", name)
    words = ["GPT" if p == "gpt" else (p if p[:1].isdigit() else p.capitalize())
             for p in name.split("-") if p]
    return re.sub(r"GPT (\d)", r"GPT-\1", " ".join(words)) or str(model)


def switch_notice(failed_model: str, next_model: str) -> str | None:
    """Said the moment the chain gives up on a model (brain_gemini calls it).
    Once per turn's worth of hops: a chain that walks past five dead models
    must not speak five times."""
    global _last_switch_notice
    now = time.time()
    if now - _last_switch_notice < 30:
        return None
    _last_switch_notice = now
    return f"{friendly(failed_model)} isn't available right now, so I'm switching to another model, sir."


def answering(model: str | None, backend: str | None) -> str | None:
    """Who answered a turn, in the user's words, or None for a local tool."""
    if backend == "ollama":
        return "the local model"
    if model:
        return friendly(model)
    return {"cerebras": "Cerebras", "groq": "Groq", "gemini": "Gemini"}.get(backend or "")


def answer_notice(model: str | None, backend: str | None, previous: str | None) -> str | None:
    """The line said with a reply when the model answering changed since the
    last reply to this client ('Claude 4.5 Sonnet is answering now.'), else None."""
    who = answering(model, backend)
    if not who or who == previous:
        return None
    if backend == "ollama":
        return "The cloud models are unavailable, so the local model is answering; it's slower."
    return f"{who} is answering now."


def primary() -> str | None:
    """The configured first model, as the user would name it."""
    try:
        from config import ROUTER_MODEL
        return friendly(ROUTER_MODEL)
    except Exception:
        return None
