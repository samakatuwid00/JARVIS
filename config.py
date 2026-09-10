"""Configuration for Jarvis AI Assistant"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# API Keys
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"

# 9router (local AI proxy) — primary brain backend
# Set JARVIS_USE_9ROUTER=false in .env to skip straight to Gemini
JARVIS_USE_9ROUTER = os.getenv("JARVIS_USE_9ROUTER", "true").lower() == "true"
ROUTER_BASE_URL = os.getenv("ROUTER_BASE_URL", "http://localhost:20128/v1")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "oc/mimo-v2.5-free")
# `or` not a getenv default: ROUTER_API_KEY= (set but empty) in .env returns ""
# and the OpenAI client then raises "Missing credentials", which reads like a
# JARVIS bug rather than an unconfigured 9router.
ROUTER_API_KEY = os.getenv("ROUTER_API_KEY") or "dummy"
# Free-tier 9router models tried (in order) when the primary is rate-limited
# (429). They carry separate quotas, so a throttled primary hops to a fresh
# model instead of dropping to demo mode. Last real fallback is Ollama (step 3).
ROUTER_FALLBACK_MODELS = [m.strip() for m in
    os.getenv("ROUTER_FALLBACK_MODELS",
              "ag/gemini-3.5-flash-low,ag/gemini-3-flash,antigrav,cx/gpt-5.6-sol").split(",") if m.strip()]

# --- Groq (OpenAI-compatible free tier) -------------------------------------
# Lets JARVIS use a free Groq model so the Gemini quota isn't burned by JARVIS.
# Set JARVIS_USE_GROQ=true in .env to enable. The key is read from the
# schema-mapper project's .env (single source of truth) — no duplication here.
# Override JARVIS_GROQ_KEY_PATH if your schema-mapper .env lives elsewhere.
JARVIS_USE_GROQ = os.getenv("JARVIS_USE_GROQ", "false").lower() == "true"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
if not GROQ_API_KEY:
    _GROQ_ENV_PATH = os.getenv(
        "JARVIS_GROQ_KEY_PATH",
        r"C:/Users/deped/Documents/schema-mapper-backup/July 08, 2026/schema_mapper/.env",
    )
    from pathlib import Path as _Path
    _gp = _Path(_GROQ_ENV_PATH)
    if _gp.exists():
        try:
            from dotenv import dotenv_values as _dec
            _GVALS = {k: v for k, v in _dec(_gp).items() if v}
            GROQ_API_KEY = _GVALS.get("GROQ_API_KEY", "")
        except Exception:
            GROQ_API_KEY = ""

# --- Cerebras (OpenAI-compatible free tier) ---------------------------------
# Second free-tier backstop so a quota wall on Groq can't drop JARVIS to demo
# mode. Read from the same schema-mapper .env (CEREBRAS_API_KEY). Override
# JARVIS_CEREBRAS_KEY_PATH to point elsewhere.
JARVIS_USE_CEREBRAS = os.getenv("JARVIS_USE_CEREBRAS", "false").lower() == "true"
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_BASE_URL = os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gemma-4-31b")
if not CEREBRAS_API_KEY:
    _CEREBRAS_ENV_PATH = os.getenv(
        "JARVIS_CEREBRAS_KEY_PATH",
        r"C:/Users/deped/Documents/schema-mapper-backup/July 08, 2026/schema_mapper/.env",
    )
    _cp = _Path(_CEREBRAS_ENV_PATH)
    if _cp.exists():
        try:
            _CVALS = {k: v for k, v in _dec(_cp).items() if v}
            CEREBRAS_API_KEY = _CVALS.get("CEREBRAS_API_KEY", "")
        except Exception:
            CEREBRAS_API_KEY = ""

# --- Ollama (local, OpenAI-compatible) --------------------------------------
# Last real backend before demo mode: runs on this machine, so it answers when
# every cloud tier is quota-walled or the network is down. Measured 14.1 tok/s
# on qwen3:4b-instruct (CPU only — Ollama has no ROCm path for this iGPU).
# Set JARVIS_PREFER_LOCAL=true to try it FIRST instead of last (offline work,
# battery, or keeping a conversation entirely on-device).
JARVIS_USE_OLLAMA = os.getenv("JARVIS_USE_OLLAMA", "true").lower() == "true"
JARVIS_PREFER_LOCAL = os.getenv("JARVIS_PREFER_LOCAL", "false").lower() == "true"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
# jarvis-qwen3 is qwen3:4b-instruct rebuilt with num_ctx 8192 (Modelfile.jarvis).
# The stock 4096 window is ~2.8k spent before the first user turn — 21 tool
# schemas cost ~2.2k tokens — so history silently truncates mid-conversation.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "jarvis-qwen3")
# Ollama ignores the key, but the OpenAI client requires a non-empty string.
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "ollama")
# 512 tokens at 14 tok/s is ~36s worst case; voice replies are far shorter.
# The 45s timeout the cloud paths use would cut local replies off mid-sentence.
OLLAMA_MAX_TOKENS = int(os.getenv("OLLAMA_MAX_TOKENS", "512"))
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))
# Ollama evicts an idle model after ~5 minutes, and the /v1 endpoint ignores
# keep_alive, so the next turn pays a full cold prefix eval: measured 28.9s vs
# 2.4s warm on the same request. A cheap 1-token ping on the real call shape
# keeps the model resident AND the ~2.9k-token tool prefix in the prompt cache.
# 240s stays comfortably inside the 5-minute eviction window.
# Hard local-only switch. JARVIS_PREFER_LOCAL only reorders the chain — the
# cloud tiers are still there and still answer when the local model errors, so
# "local first" is not the same as "local only". This disables every cloud
# backend AND the Gemini fallback, so nothing ever leaves the machine.
JARVIS_LOCAL_ONLY = os.getenv("JARVIS_LOCAL_ONLY", "false").lower() == "true"
OLLAMA_KEEP_WARM = os.getenv("OLLAMA_KEEP_WARM", "true").lower() == "true"
OLLAMA_WARM_INTERVAL = int(os.getenv("OLLAMA_WARM_INTERVAL", "240"))
# Full context reset after this many minutes without a turn. The local 4b model
# degrades with accumulated history; after a real gap the next turn starts fresh
# instead of replaying stale turns. 0 disables the feature entirely.
IDLE_RESET_MINUTES = int(os.getenv("JARVIS_IDLE_RESET_MINUTES", "10"))
# Ceiling on replayed conversation history, in estimated tokens, for the local
# backend. The 8192-token window is already ~3.7k spent on the system prompt
# plus 24 tool schemas before a single turn of history, so an unbounded history
# overflows it and Ollama re-evaluates an enormous prefix: measured 8.8s for a
# fresh turn, 45.7s at 1.9k tokens of history, 162.4s at 11.5k — past
# OLLAMA_TIMEOUT, at which point the turn fails outright and JARVIS answers
# with an apology instead of calling the tool it was asked for. Above this
# estimate the oldest turns are dropped. Raising it requires a bigger num_ctx
# in Modelfile.jarvis and a model rebuild.
LOCAL_HISTORY_TOKEN_BUDGET = int(os.getenv("JARVIS_LOCAL_HISTORY_TOKENS", "3500"))

# Same ceiling for the CLOUD paths. Until now only the Ollama path trimmed by
# tokens; the router replayed self.conversation in full, bounded solely by
# _cap_conversation's 60-MESSAGE count. Message count is not a proxy for size —
# 60 turns of user text measured ~717 tokens, but 60 messages carrying web
# search and page-read results are unbounded. A big context window is not the
# same as a good one: quality degrades long before the window is full, which is
# what "hallucinates after a long conversation" looks like from outside.
# Generous because cloud windows are large; a ceiling nonetheless.
ROUTER_HISTORY_TOKEN_BUDGET = int(os.getenv("JARVIS_ROUTER_HISTORY_TOKENS", "12000"))

# Tool results are the actual bloat: a page read or search can be tens of
# thousands of characters, and it stays in history for every later turn. The
# CURRENT turn always sees the full result — only the copy kept for replay is
# clipped, so answering quality this turn is unchanged while the tail stops
# growing without bound.
TOOL_RESULT_HISTORY_CHARS = int(os.getenv("JARVIS_TOOL_RESULT_HISTORY_CHARS", "2000"))

# Audio
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1024
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium.en")
# Half of the 16 available cores; leaves headroom for TTS/server work.
WHISPER_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "8"))
# Beam width for command transcription. Measured 2026-09-09 on a 6.21s clip,
# small.en int8 / 8 threads, idle CPU: beam 5 = 1.63s, beam 1 = 1.53s, and the
# decoded text was identical. Beam 5 bought nothing but latency.
WHISPER_BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "1"))

# --- Server-side wake engine's own Whisper ---------------------------------
# The wake engine re-decodes a rolling window roughly once a second, forever.
# Sharing the command model (small.en, 8 threads) meant each pass cost ~1.7s
# against a 1.0s step, so it could never keep up and pinned 8 of 16 cores
# whenever the room was not silent — including while the user was speaking a
# command. Measured effect on the same clip: 1.63s idle vs 23.20s with the wake
# engine running, a 14x penalty on exactly the path the user waits for.
# A separate tiny.en on 2 threads decodes a window in ~0.3s, which is plenty for
# spotting one word, and leaves the command path its cores.
WAKE_WHISPER_MODEL = os.getenv("WAKE_WHISPER_MODEL", "tiny.en")
WAKE_WHISPER_CPU_THREADS = int(os.getenv("WAKE_WHISPER_CPU_THREADS", "2"))

# Vocabulary bias so the wake word and domain terms decode cleanly.
WHISPER_INITIAL_PROMPT = os.getenv(
    "WHISPER_INITIAL_PROMPT",
    "JARVIS, Jarvis, Stark, DepEd, Department of Education, iRIMS-V, iRIMS, "
    "LRMIS, IRIMS, Haristay, Two Goals, Manila, Philippines, Philippine, "
    "dashboard, report, project, vault, compose, play music, YouTube Music, "
    "Brave, analysis, system, online, open, search, weather."
)

# Wake Word
WAKE_WORD = os.getenv("WAKE_WORD", "jarvis")
WAKE_WORD_TIMEOUT = int(os.getenv("WAKE_WORD_TIMEOUT", "10"))
PORCUPINE_ACCESS_KEY = os.getenv("PORCUPINE_ACCESS_KEY", "")

# TTS
TTS_VOICE = os.getenv("TTS_VOICE", "en-GB-RyanNeural")
# +20% measured at 1.20x faster speech (8.93s -> 7.44s on a sample reply) while
# still sounding natural on the Ryan neural voice.
TTS_RATE = os.getenv("TTS_RATE", "+20%")
TTS_VOLUME = os.getenv("TTS_VOLUME", "+0%")

# Voice state feedback (spoken cues while transcribing/thinking/executing).
# essential = useful cues with throttling; full = Phase B milestones too; off = silent.
VOICE_FEEDBACK_MODE = os.getenv("JARVIS_VOICE_FEEDBACK", "essential").strip().lower()
VOICE_THINKING_CUE_S = float(os.getenv("JARVIS_VOICE_THINKING_CUE_S", "4"))
VOICE_THINKING_ESCALATE_S = float(os.getenv("JARVIS_VOICE_THINKING_ESCALATE_S", "12"))
VOICE_CUE_COOLDOWN_S = float(os.getenv("JARVIS_VOICE_CUE_COOLDOWN_S", "8"))


# Conversation
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))

# Debug
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# Paths
BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"