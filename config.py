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
ROUTER_API_KEY = os.getenv("ROUTER_API_KEY", "dummy")

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

# Audio
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1024
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small.en")
# Half of the 16 available cores; leaves headroom for TTS/server work.
WHISPER_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "8"))
# Vocabulary bias so the wake word and domain terms decode cleanly.
WHISPER_INITIAL_PROMPT = os.getenv(
    "WHISPER_INITIAL_PROMPT",
    "Jarvis, Stark, workshop, archive, crate, analysis, system, online."
)

# Wake Word
WAKE_WORD = os.getenv("WAKE_WORD", "jarvis")
WAKE_WORD_TIMEOUT = int(os.getenv("WAKE_WORD_TIMEOUT", "10"))
PORCUPINE_ACCESS_KEY = os.getenv("PORCUPINE_ACCESS_KEY", "")

# TTS
TTS_VOICE = os.getenv("TTS_VOICE", "en-GB-RyanNeural")
TTS_RATE = os.getenv("TTS_RATE", "+0%")
TTS_VOLUME = os.getenv("TTS_VOLUME", "+0%")

# Conversation
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))

# Debug
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# Paths
BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"