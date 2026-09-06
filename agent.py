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
    print(f"[JARVIS AGENT] Connecting to room: {ctx.room.name}")

    await ctx.connect()
    print(f"[JARVIS AGENT] Connected. Starting session...")

    session = AgentSession(
        # FIX: reverted to plain "small" — it's the one model that's
        # actually already fully cached on your machine and confirmed
        # fast in every successful test so far. Both "medium" and
        # "small.en" required fresh downloads that stalled on a slow/
        # rate-limited connection to HuggingFace (note the "sending
        # unauthenticated requests" warning in your logs) — not worth
        # gambling on a third model needing a fresh multi-hundred-MB
        # download. Getting accuracy gains from the free levers instead
        # (initial_prompt below, beam_size, the earlier stereo-downmix
        # fix) rather than a bigger model.
        # FIX (accuracy, zero extra setup): bumped "small" -> "medium".
        # "small" was tuned for a laptop with no GPU — a meaningfully
        # more accurate model, still fine on CPU with a little extra
        # latency. (You do have an RTX 4050 — if this still mishears
        # things, switching device="cuda" here would let an even bigger
        # model run just as fast, but that needs matching NVIDIA cuDNN/
        # cuBLAS libraries installed separately first, so it's not done
        # by default here.)
        stt=LocalWhisperSTT(model_size="medium", device="cpu", compute_type="int8"),
        # ARCHITECTURE FIX: previously this was a bare
        # openai.LLM(...) talking straight to Ollama — meaning a voice
        # turn NEVER reached intent_router/context_manager/
        # model_selector/llm_orchestrator, the pipeline main.py's
        # /chat endpoint uses for typed messages. That's why voice
        # answered general-knowledge/calculation questions in-persona
        # instead of correctly, even though the identical text typed
        # into the chat box worked fine. BackendBridgeLLM wraps the
        # same raw Ollama LLM (kept as `fallback_llm` so tool calls
        # like get_weather/search_web/send_email/open_application/
        # open_website — which only exist on the voice side — still
        # work exactly as before) and, for any turn that ISN'T a tool
        # call, routes the utterance through the real /chat pipeline
        # instead. See backend_bridge_llm.py for the full rationale.
        llm=BackendBridgeLLM(
            fallback_llm=openai.LLM(model=OLLAMA_MODEL, base_url=OLLAMA_URL, api_key="ollama"),
        ),
        tts=LocalPiperTTS(model_path=PIPER_MODEL_PATH, speed=0.95),
        vad=silero.VAD.load(),

        # ── FIX 1: Reduce STT latency ─────────────────────────
        # NOTE: these were tuned very aggressively (0.3s/0.8s) purely for
        # low latency. That's very likely a direct cause of "STT
        # recognizes the wrong words" — a short pause mid-sentence
        # (taking a breath, thinking of the next word) can trigger
        # end-of-turn before you're actually done talking, so whisper
        # transcribes a truncated sentence and produces a different
        # (wrong-looking) result rather than what you actually said in
        # full. Relaxed slightly to give natural speech room to finish
        # before the turn is cut off; still fast enough to feel
        # responsive.
        min_endpointing_delay=0.5,
        max_endpointing_delay=1.5,
        min_consecutive_speech_delay=0.0,
        preemptive_generation=True,

        # ── FIX 4: Proper interruption handling ───────────────
        # min_interruption_duration was 0.3s — tightened to 0.2s so
        # cutting JARVIS off feels closer to instant rather than
        # needing a deliberate pause-then-speak. min_interruption_words
        # is already at its minimum (1), so that's not a lever left to
        # pull. false_interruption_timeout stays at 0.6s as a guard: if
        # what looked like an interruption turns out to be background
        # noise rather than real speech, JARVIS resumes instead of
        # staying cut off.
        allow_interruptions=True,
        min_interruption_duration=0.2,
        min_interruption_words=1,
        false_interruption_timeout=0.6,
        resume_false_interruption=True,
        discard_audio_if_uninterruptible=True,

        # ── FIX 4: Echo cancellation warmup ───────────────────
        aec_warmup_duration=3.0,
    )

    # ── User speech: console/debug logging only ────────────────
    # NOTE: this used to ALSO POST the transcript to
    # /voice/transcript to save it into Live Conversation. That's now
    # backend_bridge_llm.py's job (see its "TRANSCRIPT SAVING" docs) —
    # it's the one place that knows whether a given turn ends up
    # tool-routed, pipeline-routed (in which case /chat itself saves
    # it), or is the post-tool narration round, so it can save each
    # user/assistant turn exactly once instead of this handler and
    # /chat both saving the same message.
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

    # ── JARVIS response: console/debug logging only ────────────
    # NOTE: saving to Live Conversation is now backend_bridge_llm.py's
    # job too, for the same reason as above. This handler is kept
    # purely so failures are still visible in the console the moment
    # they happen (e.g. an empty completion), without duplicating the
    # save/broadcast backend_bridge_llm.py already did.
    @session.on("conversation_item_added")
    def on_conversation_item(event):
        try:
            item = event.item
            # The AgentSession framework also fires
            # conversation_item_added for internal control items (e.g.
            # AgentHandoff, used for multi-agent handoff) that don't
            # have a .role at all — getattr(..., None) skips those
            # silently instead of throwing.
            role = getattr(item, "role", None)
            if role != "assistant":
                return
            text = getattr(item, "text_content", None) or ""
            if text.strip():
                print(f"[AI] Response: {text}")
            else:
                # An empty assistant text item means TTS has nothing to
                # synthesize, so agent_state_changed goes straight from
                # "thinking" back to "listening", skipping "speaking"
                # entirely. Log what the item actually contained so we
                # can tell whether this was a tool-call-only completion
                # (expected — the tool-call round has no spoken text)
                # or a genuinely empty LLM response.
                print(f"[AI] (no spoken text this turn — raw item: {item!r})")
        except Exception as e:
            print(f"[AI ERROR] conversation_item_added handler failed: {e}")
            logger.warning(f"conversation_item_added error: {e}")

    # ── Voice state diagnostics ─────────────────────────────
    @session.on("agent_state_changed")
    def on_agent_state_changed(event):
        state = getattr(event, "new_state", None)
        if state == "listening":
            print("[VOICE] Listening...")
        elif state == "thinking":
            print("[COMMAND] Processing...")
        elif state == "speaking":
            print("[TTS] Speaking...")
        # Forward the real state to the backend -> frontend so the UI
        # can show live feedback (see send_state_to_backend docstring).
        if state:
            asyncio.create_task(send_state_to_backend(state))

    try:
        await session.start(
            room=ctx.room,
            agent=Assistant(),
            room_input_options=RoomInputOptions(
                noise_cancellation=noise_cancellation.BVC(),
            ),
        )
    except Exception as e:
        # If this fails (e.g. the BVC noise-cancellation plugin can't
        # load/license itself, or STT/TTS model loading throws), the
        # agent process would otherwise sit there having registered with
        # the room but never actually listening — which looks identical
        # to "JARVIS does not respond" from the browser side. Surface it
        # loudly instead of letting it disappear into the framework.
        print(f"[JARVIS AGENT ERROR] session.start() failed: {e}")
        logger.error(f"session.start() failed: {e}", exc_info=True)
        raise

    try:
        greeting_instruction = _build_greeting_instruction()
        await session.generate_reply(
            instructions=greeting_instruction
        )
    except Exception as e:
        print(f"[JARVIS AGENT ERROR] generate_reply() failed: {e}")
        logger.error(f"generate_reply() failed: {e}", exc_info=True)

    print(f"[JARVIS AGENT] Session active and listening.")


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
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="Jarvis",
        )
    )