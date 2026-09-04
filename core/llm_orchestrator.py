"""
JARVIS LLM orchestration layer (Phase 5, Sections 2 & 3).

Wraps the existing Ollama integration (main.py's ask_ollama() /
intent_router.py's classify(), both already using the `ollama` package
directly) with:

  - bounded conversation memory (never sends unlimited history)
  - a summarized-older-turns fallback once history exceeds the bound
  - context-aware prompt construction (folds in context_manager's
    GatheredContext block)

This module does NOT execute tools and does NOT decide permissions —
it only builds the message list an LLM call should receive and,
optionally, makes that call. main.py remains free to keep calling
ask_ollama() directly for the plain butler-voice chit-chat path; this
module is for Phase 5's new assistant modes, which need longer,
structured prompts that ask_ollama()'s one-sentence system prompt
isn't shaped for.

The actual `ollama.chat(...)` call is isolated behind `_call_model` so
tests can monkeypatch it without a live Ollama server, matching how
test_phase3.py already mocks `ollama.chat` for intent_router.classify.
"""

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("jarvis-orchestrator")

# Hard bound on how many prior turns get sent verbatim. Matches the
# same order of magnitude as context_manager's
# _CONVERSATION_HISTORY_LIMIT (6) but kept as this module's own
# constant since orchestrator prompts (study/CSE explanations) can
# reasonably want a couple more turns of back-and-forth than a
# context-block summary needs — see module docstring: "Do NOT blindly
# send unlimited conversation history to Ollama."
MAX_VERBATIM_TURNS = 8

# Once a single message's content exceeds this many characters, it's
# truncated with a marker rather than sent in full — a pasted stack
# trace or file dump in earlier conversation shouldn't balloon every
# subsequent prompt.
MAX_MESSAGE_CHARS = 1200


@dataclass
class OrchestratedPrompt:
    system_prompt: str
    messages: list[dict]           # [{"role": "user"/"assistant", "content": ...}, ...]
    summarized_count: int = 0      # how many older turns got folded into a summary line
    context_block: Optional[str] = None


def _truncate(content: str) -> str:
    if len(content) <= MAX_MESSAGE_CHARS:
        return content
    return content[:MAX_MESSAGE_CHARS] + f"... [truncated, {len(content) - MAX_MESSAGE_CHARS} more chars]"


def _summarize_older_turns(older: list[dict]) -> Optional[str]:
    """
    Deliberately simple, local, extractive summary (Section 15: "Do
    not over-engineer this phase") — no LLM call, no vector store.
    Picks out the user's messages (what they asked/wanted across the
    turns being dropped) since that's what's most useful for a
    follow-up to still make sense; assistant replies from those older
    turns are omitted from the summary since context_manager's own
    context block already covers durable facts (project, files) and
    duplicating full replies would defeat the point of bounding.
    """
    if not older:
        return None
    user_asks = [m["content"].strip() for m in older if m.get("role") == "user" and m.get("content", "").strip()]
    if not user_asks:
        return None
    preview = "; ".join(a[:80] for a in user_asks[-5:])
    return f"(Earlier in this conversation, the user also asked about: {preview})"


def build_bounded_messages(
    history: list[dict],
    max_turns: int = MAX_VERBATIM_TURNS,
) -> tuple[list[dict], int]:
    """
    Pure function: given full history (oldest first, same shape as
    memory.get_history()'s return), return (bounded_messages,
    summarized_count). Bounded_messages only contains role in
    {"user","assistant"} and is truncated per-message.
    """
    relevant = [m for m in history if m.get("role") in ("user", "assistant")]
    if len(relevant) <= max_turns:
        return (
            [{"role": m["role"], "content": _truncate(m["content"])} for m in relevant],
            0,
        )

    older = relevant[: len(relevant) - max_turns]
    recent = relevant[len(relevant) - max_turns:]
    bounded = [{"role": m["role"], "content": _truncate(m["content"])} for m in recent]

    summary = _summarize_older_turns(older)
    if summary:
        bounded.insert(0, {"role": "assistant", "content": summary})

    return bounded, len(older)


def build_prompt(
    task_system_prompt: str,
    history: list[dict],
    context_block: Optional[str] = None,
    max_turns: int = MAX_VERBATIM_TURNS,
) -> OrchestratedPrompt:
    """
    Assemble a full prompt for a Phase 5 assistant call. task_system_prompt
    is the assistant-specific instructions (e.g. cse_assistant's debugging
    format, study_assistant's teaching style) — kept separate from
    context_block so callers can log/inspect/test each independently.
    """
    bounded, summarized_count = build_bounded_messages(history, max_turns=max_turns)

    system_prompt = task_system_prompt
    if context_block:
        system_prompt = f"{task_system_prompt}\n\nContext:\n{context_block}"

    return OrchestratedPrompt(
        system_prompt=system_prompt,
        messages=bounded,
        summarized_count=summarized_count,
        context_block=context_block,
    )


# ── Model call (isolated for testability / graceful degradation) ──

def _call_model(
    system_prompt: str,
    messages: list[dict],
    user_message: str,
    *,
    model: Optional[str] = None,
    temperature: float = 0.4,
    num_predict: int = 500,
) -> str:
    # Section 8: default to config.py's TEXT_MODEL (JARVIS_TEXT_MODEL)
    # instead of a hardcoded literal; callers can still override per-call.
    if model is None:
        from config import TEXT_MODEL
        model = TEXT_MODEL
    import ollama
    full_messages = [{"role": "system", "content": system_prompt}]
    full_messages.extend(messages)
    full_messages.append({"role": "user", "content": user_message})
    response = ollama.chat(
        model=model,
        messages=full_messages,
        options={"temperature": temperature, "num_predict": num_predict},
    )
    return response["message"]["content"].strip()


def run(
    task_system_prompt: str,
    user_message: str,
    history: list[dict],
    context_block: Optional[str] = None,
    max_turns: int = MAX_VERBATIM_TURNS,
    model_caller: Optional[Callable[..., str]] = None,
) -> str:
    """
    Build a bounded, context-aware prompt and call the model.

    Never crashes the assistant (Section 16: "J.A.R.V.I.S should
    continue operating where possible") — if the model call fails
    (Ollama unavailable, timeout, malformed response), this returns an
    honest apology string instead of raising, matching main.py's
    existing ask_ollama() error-handling convention at the /chat layer.

    model_caller lets callers (and tests) inject a stand-in for
    ollama.chat without needing a live server.
    """
    prompt = build_prompt(task_system_prompt, history, context_block, max_turns)
    caller = model_caller or _call_model
    try:
        return caller(prompt.system_prompt, prompt.messages, user_message)
    except Exception as e:
        logger.error(f"[orchestrator] model call failed: {e}")
        return "I ran into trouble reaching my language model just now, Sir — my apologies. Is Ollama running?"
