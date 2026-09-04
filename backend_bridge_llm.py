"""
Bridges the LiveKit voice pipeline into JARVIS's own request-processing
pipeline (intent_router -> context_manager -> model_selector ->
llm_orchestrator/ask_ollama) — the exact same code path main.py's
/chat endpoint uses for typed messages.

ROOT CAUSE THIS FIXES (see audit notes):
agent.py used to hand AgentSession a raw openai.LLM(...) pointed
straight at Ollama, guided only by the persona prompt in prompts.py.
That LLM never called classify_intent(), context_manager.gather(),
model_selector.select_model(), or llm_orchestrator.run() — so a voice
turn NEVER reached the logic typed chat gets. /voice/transcript and
/voice/state (main.py) only ever *display* what the voice agent already
decided; they don't feed anything back into request processing. That
disconnect is why "What is Python?" or "what's 2 plus 2" got answered
in-character instead of correctly, while the identical text typed into
the chat box worked fine.

DESIGN
------
AgentSession still needs *something* that understands LiveKit's native
function-calling protocol so voice-only actions (get_weather,
search_web, send_email, open_application, open_website — registered as
tools on the Assistant agent, and NOT wired into the typed /chat
intent set at all) keep working exactly as before. So this does not
replace the underlying LLM; it wraps it:

  1. Every turn is first generated normally by the wrapped/raw LLM
     (`fallback_llm`, the original openai.LLM -> Ollama), *with* tools
     available, exactly as before.
  2. If that generation decided to call a tool, we pass its chunks
     through completely unchanged — tool-driven voice actions are
     unaffected, zero behavior change, zero added risk.
  3. If it did NOT call a tool (a plain conversational/knowledge/
     calculation answer), we throw that draft away and instead POST
     the user's actual utterance to the real /chat pipeline — the same
     endpoint the typed UI calls — and speak whatever that returns.

Piper TTS is already non-streaming (it waits for the full reply text
before synthesizing anything — see piper_tts.py), so buffering the
fallback LLM's output before deciding costs no perceptible latency
that wasn't already there.

The one turn with no user message at all — the scripted startup
greeting via session.generate_reply(instructions=...) — has nothing
for intent_router to classify, so it always goes straight to the raw
LLM, same as before.

TRANSCRIPT SAVING
------------------
This module is also the single place that decides when a voice turn's
user/assistant text gets saved+broadcast to Live Conversation (via
POST /voice/transcript), instead of agent.py's event handlers doing it
independently. That avoids double-saving: when a turn IS routed
through /chat, /chat already saves+broadcasts both sides of it
(passing skip_user_save=True here since the user side was already
recorded the moment STT finalized); when a turn ISN'T routed through
/chat (a tool call, or the no-user-text greeting), this module saves
it directly since nothing else will.
"""

import logging
import uuid

import aiohttp

from livekit.agents import llm
from livekit.agents.llm import ChatChunk, ChatContext, ChoiceDelta, LLMStream
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions
from livekit.agents._exceptions import APIConnectionError

logger = logging.getLogger("jarvis-backend-bridge-llm")

BACKEND_URL = "http://localhost:8000"
VOICE_SESSION_ID = "voice-session"  # matches main.py's /voice/* endpoints


def _last_user_text(chat_ctx: ChatContext) -> str | None:
    """Pull the most recent user utterance out of the chat context, or
    None if this turn has no real user message (e.g. the startup
    greeting, which is instructions-only)."""
    for item in reversed(chat_ctx.items):
        if getattr(item, "type", None) == "message" and getattr(item, "role", None) == "user":
            text = item.text_content
            if text and text.strip():
                return text.strip()
    return None


def _is_post_tool_round(chat_ctx: ChatContext) -> bool:
    """True if a tool was already called (and returned a result) for the
    current user turn -- i.e. this chat() call is the follow-up round
    where the LLM turns a tool's output into a spoken sentence
    ("It's 72F and sunny, Sir"). That answer must NOT be replaced by a
    /chat pipeline call: /chat has no idea a tool ran or what it
    returned, and would either answer generically or wrongly re-run its
    own (tool-less) logic, silently discarding the real, already-fetched
    result. Detected by checking whether a function_call/
    function_call_output item appears after the most recent user
    message in the context.
    """
    items = chat_ctx.items
    last_user_idx = None
    for i, item in enumerate(items):
        if getattr(item, "type", None) == "message" and getattr(item, "role", None) == "user":
            last_user_idx = i
    if last_user_idx is None:
        return False
    return any(
        getattr(item, "type", None) in ("function_call", "function_call_output")
        for item in items[last_user_idx + 1:]
    )


class BackendBridgeLLM(llm.LLM):
    """Drop-in LLM for AgentSession that routes non-tool voice turns
    through JARVIS's shared /chat pipeline instead of answering
    in-persona directly. See module docstring for the full rationale.
    """

    def __init__(self, fallback_llm: llm.LLM):
        super().__init__()
        self._fallback = fallback_llm

    @property
    def model(self) -> str:
        return "jarvis-backend-pipeline"

    @property
    def provider(self) -> str:
        return "jarvis"

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools=None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls=NOT_GIVEN,
        tool_choice=NOT_GIVEN,
        extra_kwargs=NOT_GIVEN,
    ) -> LLMStream:
        fallback_stream = self._fallback.chat(
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
        )
        user_text = _last_user_text(chat_ctx)
        # A tool already ran earlier in this same turn -> this call is
        # the LLM narrating that tool's result. Never worth rerouting
        # (see _is_post_tool_round's docstring for why).
        if _is_post_tool_round(chat_ctx):
            user_text = None
        return _BackendBridgeStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            fallback_stream=fallback_stream,
            user_text=user_text,
        )


async def _save_transcript(role: str, content: str) -> None:
    """Save+broadcast one side of a voice turn to Live Conversation.
    Only called for turns that /chat itself won't already save (see
    TRANSCRIPT SAVING in the module docstring)."""
    if not content or not content.strip():
        return
    try:
        async with aiohttp.ClientSession() as http:
            await http.post(
                f"{BACKEND_URL}/voice/transcript",
                json={"role": role, "content": content},
                timeout=aiohttp.ClientTimeout(total=3),
            )
    except Exception as e:
        logger.warning(f"[voice] could not save transcript ({role}): {e}")


class _BackendBridgeStream(LLMStream):
    def __init__(self, llm_instance, *, chat_ctx, tools, conn_options, fallback_stream, user_text):
        super().__init__(llm_instance, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._fallback_stream = fallback_stream
        self._user_text = user_text

    async def _run(self) -> None:
        # A non-None user_text here means this call is a genuinely new
        # user turn (not the post-tool narration round, not the
        # instructions-only greeting) — record it now, once, regardless
        # of whether it ends up being tool-routed or pipeline-routed.
        is_new_user_turn = self._user_text is not None
        if is_new_user_turn:
            await _save_transcript("user", self._user_text)

        # ── Step 1: let the raw LLM decide, tools included ──────────
        used_tool = False
        buffered_chunks: list[ChatChunk] = []
        try:
            async for chunk in self._fallback_stream:
                buffered_chunks.append(chunk)
                if chunk.delta and chunk.delta.tool_calls:
                    used_tool = True
        except Exception as e:
            print(f"[VOICE ERROR] Underlying LLM call failed: {e}")
            logger.error(f"[voice] fallback LLM failed: {e}", exc_info=True)
            raise
        finally:
            await self._fallback_stream.aclose()

        if used_tool:
            # Tool-driven turn (open an app, send an email, check the
            # weather, ...) — pass the original generation straight
            # through, completely unaffected by this bridge. (No
            # spoken text to save yet — a tool-call chunk carries no
            # .content; the follow-up narration round saves it below.)
            for chunk in buffered_chunks:
                self._event_ch.send_nowait(chunk)
            return

        if not self._user_text:
            # Either the scripted startup greeting (no user turn at
            # all) or the post-tool narration round (user turn already
            # saved in round 1) — nothing to route through /chat, just
            # speak the raw LLM's draft, and save IT as the assistant
            # side of this turn since /chat never sees it.
            draft_text = "".join(
                c.delta.content for c in buffered_chunks if c.delta and c.delta.content
            )
            await _save_transcript("assistant", draft_text)
            for chunk in buffered_chunks:
                self._event_ch.send_nowait(chunk)
            return

        # ── Step 2: plain conversational turn -> route through the
        # real pipeline. skip_user_save=True because we already saved
        # the user's utterance above; /chat will save+broadcast the
        # assistant reply itself. ─────────────────────────────────────
        print(f"[VOICE] Routing through shared /chat pipeline: {self._user_text!r}")
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{BACKEND_URL}/chat",
                    json={
                        "message": self._user_text,
                        "session_id": VOICE_SESSION_ID,
                        "skip_user_save": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except Exception as e:
            print(f"[VOICE ERROR] Backend /chat pipeline call failed: {e}")
            logger.error(f"[voice] /chat call failed: {e}", exc_info=True)
            raise APIConnectionError() from e

        reply_text = (data or {}).get("response", "").strip()
        if not reply_text:
            print("[VOICE ERROR] Backend pipeline returned an empty response")
            reply_text = "I couldn't come up with a response there, Sir."

        print(f"[VOICE] Pipeline reply: {reply_text[:80]!r}")
        self._event_ch.send_nowait(
            ChatChunk(
                id=str(uuid.uuid4()),
                delta=ChoiceDelta(role="assistant", content=reply_text),
            )
        )
