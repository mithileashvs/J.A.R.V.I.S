"""
JARVIS model selector.

One reusable place that decides which Ollama model a given request
should use, so the decision isn't duplicated/hardcoded across main.py,
core/llm_orchestrator.py, and anywhere else that calls Ollama.

Deliberately NOT a keyword matcher. Model selection is based entirely
on:
  - the intent already classified by intent_router.py's LLM-based
    classify() (semantic, not string-matching "python"/"java"/"code"
    in the raw message), and
  - whether an image is actually attached (from real attachment/
    context state — see main.py's _recall_attachment/cached_image —
    never inferred from words like "look"/"see"/"image" in the text).

This module does not call Ollama, does not execute tools, and does
not decide permissions — it only answers "which model" for a request
that's already been classified. Callers (main.py, core/llm_orchestrator.py)
remain responsible for actually making the call and for tool/permission
handling, exactly as before this module existed.
"""

import logging
from typing import Optional

from config import TEXT_MODEL, CODING_MODEL, VISION_MODEL
from intent_router import Intent

logger = logging.getLogger("jarvis-model-selector")

# Intents that represent coding / software-engineering work. Matches
# intent_router.py's own CODE_ANALYSIS/CODE_EXPLANATION/DEBUG/
# DEVELOPER_MODE/PROJECT_ANALYSIS guidance ("about existing code,
# errors, project structure, or debugging"). This set is the single
# place that decides "is this intent coding-flavored" — add a new
# coding-shaped intent here, not as a special case at each call site.
_CODING_INTENTS = {
    Intent.CODE_ANALYSIS,
    Intent.CODE_EXPLANATION,
    Intent.DEBUG,
    Intent.DEVELOPER_MODE,
    Intent.PROJECT_ANALYSIS,
}


def select_model(intent: Intent, *, has_image: bool = False) -> Optional[str]:
    """
    Return the Ollama model name to use for this request.

    has_image must reflect an ACTUAL attached/cached image (main.py's
    cached_image / an upload this turn) — never inferred from the
    message text. If has_image is True, the vision model always wins
    regardless of intent, since a request with a real image attached
    needs the model that can actually see it.

    Returns None only when has_image=True but VISION_MODEL isn't
    configured — callers must treat None as "no usable model", the
    same honest-failure contract main.py's /chat/upload already uses
    ("no vision model configured"), never silently falling back to a
    text-only model for an image request.
    """
    if has_image:
        if not VISION_MODEL:
            logger.warning(
                "[MODEL] image attached but no VISION_MODEL configured — "
                "refusing to silently fall back to a text-only model."
            )
            return None
        logger.info(f"[MODEL] intent={intent.value} category=vision model={VISION_MODEL}")
        return VISION_MODEL

    if intent in _CODING_INTENTS:
        model = CODING_MODEL or TEXT_MODEL  # fall back if coding model unset
        category = "coding" if CODING_MODEL else "coding(fallback->general)"
    else:
        model = TEXT_MODEL
        category = "general_chat"

    logger.info(f"[MODEL] intent={intent.value} category={category} model={model}")
    return model


def check_model_availability() -> dict:
    """
    Lightweight startup diagnostic (Section "MODEL AVAILABILITY" of
    the routing spec): confirms the 3 configured models are actually
    pulled in Ollama, using ollama.list() — a cheap metadata call, not
    a generation request (main.py's existing startup check already
    does a live `ollama.chat(...)` ping against TEXT_MODEL for that;
    this is a separate, additional check covering CODING_MODEL and
    VISION_MODEL too, without firing 2 more real generation calls at
    every startup).

    Never downloads a missing model — only reports what's missing so
    the person can `ollama pull` it themselves. Never raises; a
    failure to even reach Ollama here is reported as
    available=False for every configured model rather than crashing
    startup (matches config.py's validate()/main.py's existing Ollama
    check: only genuinely load-bearing failures should ever raise at
    import/startup time).
    """
    configured = {
        "text": TEXT_MODEL,
        "coding": CODING_MODEL,
        "vision": VISION_MODEL,
    }
    result = {"checked": True, "models": {}}

    try:
        import ollama
        installed_response = ollama.list()
        installed_names = {m.get("model") or m.get("name") for m in installed_response.get("models", [])}
    except Exception as e:
        logger.warning(f"[MODEL] Could not reach Ollama to check model availability: {e}")
        for label, name in configured.items():
            result["models"][label] = {"name": name or None, "available": False}
        result["checked"] = False
        return result

    for label, name in configured.items():
        if not name:
            result["models"][label] = {"name": None, "available": False}
            continue
        # Ollama tags are matched loosely (with/without ":latest")
        # since `ollama list` and JARVIS_*_MODEL env vars don't always
        # agree on whether the ":latest" suffix is spelled out.
        available = name in installed_names or f"{name}:latest" in installed_names or name.split(":")[0] in {n.split(":")[0] for n in installed_names}
        result["models"][label] = {"name": name, "available": available}
        if not available:
            logger.warning(f"[MODEL] Configured {label} model '{name}' does not appear to be pulled in Ollama.")

    return result
