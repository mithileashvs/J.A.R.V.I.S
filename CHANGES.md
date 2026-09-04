# Voice pipeline fixes — what changed and why

This archive contains only the files that changed, to keep it small.
Drop these 4 files into your project root (`J.A.R.V.I.S/`), overwriting
the existing ones. No new dependencies — `aiohttp` was already in
`requirements.txt`.

```
J.A.R.V.I.S/agent.py               (modified)
J.A.R.V.I.S/whisper_stt.py         (modified)
J.A.R.V.I.S/main.py                (modified)
J.A.R.V.I.S/backend_bridge_llm.py  (NEW file)
```

## Fix 1 — Voice was never reaching your real pipeline (the main bug)

**Root cause:** `agent.py` gave LiveKit's `AgentSession` a raw
`openai.LLM(...)` pointed straight at Ollama, guided only by the
persona prompt in `prompts.py`. That LLM never called
`classify_intent()`, `context_manager.gather()`,
`model_selector.select_model()`, or `llm_orchestrator.run()` — the
pipeline `main.py`'s `/chat` endpoint uses for everything typed.
`/voice/transcript` and `/voice/state` only ever *display* what the
voice agent already decided; nothing fed back into request processing.
That's why "What is Python?" or "what's 2 plus 2" got an in-character
non-answer over voice while the identical text typed into the chat box
worked correctly.

**Fix:** new file `backend_bridge_llm.py` — a drop-in LLM adapter for
`AgentSession`. Every turn is still generated first by the real Ollama
LLM *with your 5 voice tools* (`get_weather`, `search_web`,
`send_email`, `open_application`, `open_website`) exactly as before —
if it decides to call a tool, that's passed through completely
unmodified, so "open Chrome" / "what's the weather" / etc. are
untouched. Only when it does **not** call a tool (a plain
conversational/knowledge/calculation answer) does the bridge discard
that draft and instead POST the utterance to your real `/chat`
endpoint, speaking whatever the shared pipeline returns instead.

It also correctly recognizes the "narrate the tool's result" follow-up
round (e.g. turning a weather lookup into "It's 72 and sunny, Sir")
and never reroutes that — `/chat` has no idea a tool ran, so replacing
that narration would have silently discarded the real fetched data.

`agent.py` changes:
- imports and wires `BackendBridgeLLM` in as `llm=`, wrapping the
  original `openai.LLM(...)` as its `fallback_llm=`.
- `on_user_speech` / `on_conversation_item` no longer POST transcripts
  themselves — `backend_bridge_llm.py` now owns that (see below), so
  they're console/debug logging only.
- removed the now-dead `send_to_backend()` helper.

`main.py` changes:
- `ChatRequest` gained one new optional field: `skip_user_save: bool
  = False`. When `backend_bridge_llm.py` calls `/chat`, it sets this to
  `True` because it already saved+broadcast the user's utterance itself
  a moment earlier — without this flag the same user turn would get
  saved/broadcast twice. Typed chat is unaffected (defaults to `False`).

## Fix 2 — the actual "stuck in LISTENING" cause

**Root cause:** `whisper_stt.py` called
`self._model.transcribe(...)` — a synchronous, CPU-bound
faster-whisper call — directly inside an `async def`. On `medium`
model / CPU this can take several seconds, and for that whole
duration it blocked the **one** asyncio event loop the entire agent
process runs on: no state-change events, no LiveKit heartbeats, no
backend POSTs could run until it finished. From the browser that's
indistinguishable from "stuck," and a long enough block could even
time out the room connection with no useful error (the error/retry
timer lives on the same frozen loop). Notably `piper_tts.py` already
wrapped its own blocking call in `loop.run_in_executor(...)` — this
file just never got the same treatment.

**Fix:** the blocking work now runs via
`loop.run_in_executor(None, self._transcribe_sync, audio, language)`,
keeping the event loop free while transcription happens on a worker
thread. No behavior/accuracy change — same model, same settings, same
initial_prompt — purely moves where it executes.

## Not changed (flagged in the audit, lower severity)

- Two parallel state machines (`state.py`'s `JarvisState` for typed
  chat vs. LiveKit's own `agent_state_changed` for voice) still exist
  side by side. Unifying them is a bigger change than the two fixes
  above and wasn't touched here.
- The orphaned root-level `llm_orchestrator.py` (dead code, superseded
  by `core/llm_orchestrator.py`) is still present but unused — harmless
  as-is, just worth deleting next time you're in there.

## How to verify

1. Say "What is Python?" or "what's 2 plus 2" — should now get the
   same correct answer voice gets typed into chat.
2. Say "open Chrome" / "what's the weather" — should still work
   exactly as before (unaffected tool path).
3. Watch the agent's console output: a pipeline-routed turn prints
   `[VOICE] Routing through shared /chat pipeline: '...'` and
   `[VOICE] Pipeline reply: '...'`.
4. During a slower "medium"-model transcription, the state/UI should
   keep updating instead of appearing frozen on LISTENING.
