import os
from dotenv import load_dotenv

load_dotenv()

# ── LiveKit ────────────────────────────────────────────────
LIVEKIT_URL        = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY    = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

# ── Google Gemini ──────────────────────────────────────────
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# ── Gmail ──────────────────────────────────────────────────
GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# ── JARVIS Server ──────────────────────────────────────────
FRONTEND_PORT = int(os.getenv("JARVIS_FRONTEND_PORT", "3000"))
BACKEND_PORT  = int(os.getenv("JARVIS_BACKEND_PORT", "8000"))

# ── SQLite Memory ──────────────────────────────────────────
DB_PATH = os.getenv("JARVIS_DB_PATH", "jarvis_memory.db")

# ── Ollama Models ──────────────────────────────────────────
# Section 8 of the upgrade spec: no hardcoded model name scattered
# through the code. TEXT_MODEL handles every plain-text request exactly
# like the previous hardcoded OLLAMA_MODEL did (main.py's ask_ollama()
# and the Phase 3/5 handlers). VISION_MODEL is used automatically
# whenever a request includes an image (main.py's /chat/upload and the
# image-follow-up path in /chat) — it must be an Ollama model that
# actually accepts an `images` field (e.g. "llava", "llava:13b",
# "bakllava", "llama3.2-vision", "moondream"). If VISION_MODEL is left
# unset/empty, image requests are NOT silently downgraded to a fake
# text-only guess — main.py returns an honest
# "no vision model configured" message instead (Section 8's explicit
# requirement, and Section 25's "do not fake functionality").
TEXT_MODEL   = os.getenv("JARVIS_TEXT_MODEL", "llama3.1:8b")
VISION_MODEL = os.getenv("JARVIS_VISION_MODEL", "")

# CODING_MODEL: used for coding/software-engineering-classified intents
# (see model_selector.py). Same "no hardcoded model name scattered
# through the code" rule as TEXT_MODEL/VISION_MODEL above — every
# module that needs the coding model reads it from here, never a
# literal string. Unlike VISION_MODEL, this defaults to a real model
# name rather than "" — an unconfigured coding model just means coding
# requests fall back to TEXT_MODEL (see model_selector.py's
# select_model()), which is a reasonable degradation, not a fabricated
# capability the way silently guessing a vision model would be.
CODING_MODEL = os.getenv("JARVIS_CODING_MODEL", "qwen2.5-coder:7b")

# ── Validation ─────────────────────────────────────────────
# NOTE: JARVIS runs 100% locally now (Ollama + faster-whisper + Piper).
# GOOGLE_API_KEY/GEMINI_MODEL are leftovers from an earlier Gemini-backed
# version and are no longer imported or called by any code in this
# project (main.py's ask_ollama() and agent.py's LLM both talk to Ollama
# only). Previously validate() treated GOOGLE_API_KEY as required and
# raised EnvironmentError — an unhandled exception at *import time* of
# config.py — the moment it was empty. Since main.py does
# `from config import ...` before anything else, that exception would
# abort the entire backend before uvicorn ever starts, which looks
# exactly like "JARVIS does not respond" (nothing is listening on
# :8000, so the frontend's /livekit/token and /chat calls just fail).
# Only the values actually used at runtime (LiveKit) are required now;
# GOOGLE_API_KEY is no longer enforced.
def validate():
    # NOTE: LiveKit (voice) is likewise optional — main.py's /status
    # endpoint already reports it as "not configured" rather than
    # failing, and /livekit/token imports the livekit SDK lazily inside
    # the request handler. Treating it as required here reproduces the
    # exact GOOGLE_API_KEY bug described above: an unhandled
    # EnvironmentError at import time of config.py (which main.py
    # imports before anything else) would abort the whole backend
    # before uvicorn ever starts — even for users who only want text
    # chat and never touch the voice feature. So we warn instead of
    # raising; only genuinely load-bearing values should ever raise here.
    missing = []
    if not LIVEKIT_URL:        missing.append("LIVEKIT_URL")
    if not LIVEKIT_API_KEY:    missing.append("LIVEKIT_API_KEY")
    if not LIVEKIT_API_SECRET: missing.append("LIVEKIT_API_SECRET")
    if missing:
        print(
            f"[JARVIS] Warning: LiveKit not configured ({', '.join(missing)}). "
            "Voice features (/livekit/token) will be unavailable; "
            "text chat and other tools still work normally."
        )

validate()