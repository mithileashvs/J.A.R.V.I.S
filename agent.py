from dotenv import load_dotenv
import os
import logging
import asyncio
import aiohttp
from datetime import datetime
from livekit import agents
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import noise_cancellation, openai, silero
from prompts import AGENT_INSTRUCTIONS, SESSION_INSTRUCTION
from tools import get_weather, search_web, send_email, open_application, open_website
from whisper_stt import LocalWhisperSTT
from piper_tts import LocalPiperTTS
from backend_bridge_llm import BackendBridgeLLM
from config import TEXT_MODEL
import memory

# ── Local model settings ───────────────────────────────────
OLLAMA_URL = "http://localhost:11434/v1"   # Ollama's OpenAI-compatible endpoint
# Bug fix: this used to hardcode its own "llama3.1:8b" instead of reading
# config.py's JARVIS_TEXT_MODEL like main.py's OLLAMA_MODEL alias does —
# so changing JARVIS_TEXT_MODEL would silently leave the voice agent on
# a different model than text chat. Now uses the same single source of
# truth (Section 8's stated goal: no hardcoded model name scattered
# through the code).
OLLAMA_MODEL = TEXT_MODEL

PIPER_MODEL_PATH = "models/en_GB-alan-medium.onnx"  # downloaded voice model
# NOTE: switched from en_US-lessac-medium to en_GB-alan-medium for a
# calmer, drier British voice closer to the film JARVIS. Download it
# with (run from the project root, same venv):
#   python -m piper.download_voices en_GB-alan-medium
# This puts BOTH en_GB-alan-medium.onnx and .onnx.json into models/ —
# no manual file copying needed this time. If you'd rather try a
# different British voice, en_GB-northern_english_male is another good
# option; just change this path and re-download to match.

load_dotenv()
logger = logging.getLogger("jarvis")

BACKEND_URL = "http://localhost:8000"

# Preloaded at worker process start (see __main__) so the first voice
# session does not join the room while Whisper/Piper are still loading —
# that race made the UI show LISTENING while the agent was still deaf.
_PRELOADED_STT = None
_PRELOADED_TTS = None
_PRELOADED_VAD = None


def _preload_voice_models():
    """Load STT/TTS/VAD once in the worker process before accepting jobs."""
    global _PRELOADED_STT, _PRELOADED_TTS, _PRELOADED_VAD
    if _PRELOADED_STT is not None:
        return
    print("[JARVIS AGENT] Preloading Whisper STT (this can take a minute the first time)...")
    # "small" is cached/fast on CPU and avoids the long deaf window that
    # "medium" caused while the UI already showed LISTENING.
    _PRELOADED_STT = LocalWhisperSTT(model_size="small", device="cpu", compute_type="int8")
    print("[JARVIS AGENT] Preloading Piper TTS...")
    _PRELOADED_TTS = LocalPiperTTS(model_path=PIPER_MODEL_PATH, speed=0.95)
    print("[JARVIS AGENT] Preloading Silero VAD...")
    _PRELOADED_VAD = silero.VAD.load()
    print("[JARVIS AGENT] Voice models ready.")


async def send_state_to_backend(state: str):
    """
    Push agent processing state to the backend so the frontend can show
    real, live feedback ("JARVIS is thinking...") while STT/LLM are
    running, instead of the voice button just sitting on "LISTENING"
    with no indication anything is happening.

    This matters because a slow-but-legitimate STT/LLM pass (a few
    seconds on CPU) with zero UI feedback looks identical to "JARVIS
    didn't hear me" — which is exactly what led to a user manually
    disconnecting mid-turn in testing, aborting the reply that was
    already on its way.
    """
    # LiveKit may hand us an enum/Literal — normalize to a plain string
    # the frontend switch expects ("listening" | "thinking" | "speaking").
    state_str = getattr(state, "value", None) or str(state)
    state_str = state_str.split(".")[-1].strip().lower()
    try:
        async with aiohttp.ClientSession() as http:
            await http.post(
                f"{BACKEND_URL}/voice/state",
                json={"state": state_str},
                timeout=aiohttp.ClientTimeout(total=2),
            )
    except Exception as e:
        logger.warning(f"Could not send state to backend: {e}")


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=AGENT_INSTRUCTIONS,
            tools=[
                get_weather,
                search_web,
                send_email,
                open_application,
                open_website,
            ],
        )


async def entrypoint(ctx: agents.JobContext):
    logger.info(f"JARVIS agent connecting to room: {ctx.room.name}")
    print(f"[JARVIS AGENT] Job received for room: {ctx.room.name}")

    # Ensure models exist even if preload in __main__ was skipped
    # (e.g. agent imported differently). Do this BEFORE joining the
    # room so the browser never sees an agent participant that cannot
    # hear yet.
    await send_state_to_backend("thinking")
    try:
        _preload_voice_models()
    except Exception as e:
        print(f"[JARVIS AGENT ERROR] Model preload failed: {e}")
        logger.error(f"Model preload failed: {e}", exc_info=True)
        raise

    await ctx.connect()
    print(f"[JARVIS AGENT] Connected to room. Starting session...")

    session = AgentSession(
        stt=_PRELOADED_STT,
        llm=BackendBridgeLLM(
            fallback_llm=openai.LLM(model=OLLAMA_MODEL, base_url=OLLAMA_URL, api_key="ollama"),
        ),
        tts=_PRELOADED_TTS,
        vad=_PRELOADED_VAD,

        min_endpointing_delay=0.5,
        max_endpointing_delay=1.5,
        min_consecutive_speech_delay=0.0,
        preemptive_generation=True,

        allow_interruptions=True,
        min_interruption_duration=0.2,
        min_interruption_words=1,
        false_interruption_timeout=0.6,
        resume_false_interruption=True,
        discard_audio_if_uninterruptible=True,

        # Was 3.0s — discarded the user's first phrase ("wake up jarvis")
        # right after the UI showed LISTENING. Keep a tiny settle only.
        aec_warmup_duration=0.3,
    )

    @session.on("user_input_transcribed")
    def on_user_speech(event):
        if not event.is_final:
            return
        text = event.transcript.strip()
        if text:
            print(f"[STT] Recognized: {text}")
            print(f"[COMMAND] Received: {text}")
        else:
            print("[STT] No speech recognized")

    @session.on("conversation_item_added")
    def on_conversation_item(event):
        try:
            item = event.item
            role = getattr(item, "role", None)
            if role != "assistant":
                return
            text = getattr(item, "text_content", None) or ""
            if text.strip():
                print(f"[AI] Response: {text}")
            else:
                print(f"[AI] (no spoken text this turn — raw item: {item!r})")
        except Exception as e:
            print(f"[AI ERROR] conversation_item_added handler failed: {e}")
            logger.warning(f"conversation_item_added error: {e}")

    @session.on("agent_state_changed")
    def on_agent_state_changed(event):
        state = getattr(event, "new_state", None)
        if state == "listening":
            print("[VOICE] Listening...")
        elif state == "thinking":
            print("[COMMAND] Processing...")
        elif state == "speaking":
            print("[TTS] Speaking...")
        if state:
            asyncio.create_task(send_state_to_backend(state))

    # Also surface when the user is mid-utterance so the UI leaves
    # a frozen LISTENING label while speech is being captured.
    @session.on("user_state_changed")
    def on_user_state_changed(event):
        state = getattr(event, "new_state", None)
        state_str = getattr(state, "value", None) or str(state or "")
        state_str = state_str.split(".")[-1].strip().lower()
        if state_str == "speaking":
            print("[VOICE] User speaking...")
            asyncio.create_task(send_state_to_backend("thinking"))
        elif state_str in ("listening", "away"):
            print(f"[VOICE] User state: {state_str}")

    lk_url = (os.getenv("LIVEKIT_URL") or "").lower()
    use_bvc = "livekit.cloud" in lk_url
    if use_bvc:
        try:
            room_input = RoomInputOptions(
                noise_cancellation=noise_cancellation.BVC(),
            )
            print("[JARVIS AGENT] Using LiveKit Cloud BVC noise cancellation.")
        except Exception as e:
            print(f"[JARVIS AGENT] BVC unavailable ({e}) — starting without it.")
            room_input = RoomInputOptions()
    else:
        print("[JARVIS AGENT] Local LiveKit — starting without BVC (mic audio path).")
        room_input = RoomInputOptions()

    try:
        await session.start(
            room=ctx.room,
            agent=Assistant(),
            room_input_options=room_input,
        )
    except Exception as e:
        print(f"[JARVIS AGENT ERROR] session.start() failed: {e}")
        logger.error(f"session.start() failed: {e}", exc_info=True)
        raise

    # Session can hear the mic now — tell the UI before the greeting
    # so "wake up jarvis" isn't spoken into a still-loading pipeline.
    await send_state_to_backend("listening")
    print(f"[JARVIS AGENT] Session active and listening.")

    try:
        greeting_instruction = _build_greeting_instruction()
        await session.generate_reply(
            instructions=greeting_instruction
        )
    except Exception as e:
        print(f"[JARVIS AGENT ERROR] generate_reply() failed: {e}")
        logger.error(f"generate_reply() failed: {e}", exc_info=True)


def _build_greeting_instruction() -> str:
    """
    Build a time-aware, memory-aware greeting instruction instead of the
    static SESSION_INSTRUCTION text every time.

    Real JARVIS volunteers information rather than just waiting to be
    asked — the biggest functional gap toward that feel was that the
    greeting never actually referenced anything real. This computes the
    genuine local time (the LLM has no clock of its own — it can only
    speak the time if we hand it the actual value) and, when there's
    prior conversation in jarvis_memory.db, references the last thing
    you talked about, the way JARVIS in the films picks up threads
    across sessions rather than greeting Tony from a blank slate every
    time.
    """
    now = datetime.now()
    hour = now.hour
    if hour < 12:
        part_of_day = "morning"
    elif hour < 17:
        part_of_day = "afternoon"
    else:
        part_of_day = "evening"
    time_str = now.strftime("%I:%M %p").lstrip("0")

    memory_line = ""
    try:
        history = memory.get_all_history(limit=5)
        # Find the most recent USER message from a past turn (skip the
        # standard "JARVIS online..." greeting lines the assistant sends
        # itself, so we reference something the person actually said).
        for row in history:
            if row.get("role") == "user" and row.get("content", "").strip():
                last_user_msg = row["content"].strip()
                memory_line = (
                    f' Last time, Sir, you asked about: "{last_user_msg}" — '
                    f"if that's still relevant, briefly acknowledge it in your greeting; "
                    f"otherwise just greet normally."
                )
                break
    except Exception as e:
        logger.warning(f"Could not load memory for greeting: {e}")

    return f"""
# Task
Assist the user using available tools when needed.
Speak only in English.
It is currently {time_str} — good {part_of_day}, Sir.
Begin your greeting by naturally acknowledging the time of day (don't just state the clock time
robotically — reference it the way a butler would, e.g. "Good {part_of_day}, Sir.").{memory_line}
Keep the entire greeting to ONE short sentence.
"""


if __name__ == "__main__":
    # Named agents ONLY receive explicit CreateAgentDispatch jobs. On
    # local livekit-server --dev that dispatch often fails or races,
    # leaving the browser alone in the room (LISTENING, no STT).
    # Default: unnamed worker on localhost (auto job accept); named
    # "Jarvis" elsewhere. Override with JARVIS_AGENT_NAME ("" = auto).
    lk_url = (os.getenv("LIVEKIT_URL") or "").lower()
    is_local = "localhost" in lk_url or "127.0.0.1" in lk_url
    agent_name = os.getenv("JARVIS_AGENT_NAME")
    if agent_name is None:
        agent_name = "" if is_local else "Jarvis"
    agent_name = agent_name.strip()

    # Load Whisper/Piper/VAD before the worker starts accepting jobs so
    # the first voice click is not a multi-minute "fake listening" wait.
    try:
        _preload_voice_models()
    except Exception as e:
        print(f"[JARVIS AGENT ERROR] Startup preload failed: {e}")
        raise

    worker_kwargs = {"entrypoint_fnc": entrypoint}
    if agent_name:
        worker_kwargs["agent_name"] = agent_name
        print(f"[JARVIS AGENT] Worker name={agent_name!r} (explicit dispatch required)")
    else:
        print("[JARVIS AGENT] Unnamed worker (auto-accept room jobs — local mode)")

    agents.cli.run_app(agents.WorkerOptions(**worker_kwargs))
