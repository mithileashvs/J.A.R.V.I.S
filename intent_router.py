"""
JARVIS intent router.

Classifies a user message into one of a fixed set of intents using
Ollama's structured JSON output (format=<json schema>), instead of
hundreds of fragile keyword checks. The router only classifies — it
does not decide *how* to fulfil an intent; that's for whatever
subsystem reads its output (main.py, future per-intent handlers).

Only GENERAL_CHAT is meaningfully implemented end-to-end today (it's
what the existing /chat and voice pipeline already do). Every other
intent in the taxonomy below is real and gets classified correctly,
but most don't have a subsystem behind them yet (project memory,
debug mode, terminal, etc. are later phases) — routing to them
currently falls back to GENERAL_CHAT with a note, rather than
pretending to run a phase-3+ feature that doesn't exist.
"""

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import ollama

from config import TEXT_MODEL

logger = logging.getLogger("jarvis-intent")

# Section 8: model name now comes from config.py (JARVIS_TEXT_MODEL)
# instead of being duplicated/hardcoded in every module that talks to
# Ollama.
OLLAMA_MODEL = TEXT_MODEL


class Intent(str, Enum):
    GENERAL_CHAT = "GENERAL_CHAT"
    CODE_ANALYSIS = "CODE_ANALYSIS"
    CODE_EXPLANATION = "CODE_EXPLANATION"
    DEBUG = "DEBUG"
    PROJECT_ANALYSIS = "PROJECT_ANALYSIS"
    PROJECT_MEMORY = "PROJECT_MEMORY"
    TERMINAL = "TERMINAL"
    GIT = "GIT"
    FILE_OPERATION = "FILE_OPERATION"
    SCREEN_ANALYSIS = "SCREEN_ANALYSIS"
    SYSTEM_MONITOR = "SYSTEM_MONITOR"
    STUDY = "STUDY"
    DSA = "DSA"
    INTERVIEW = "INTERVIEW"
    RESEARCH = "RESEARCH"
    PLANNING = "PLANNING"
    # Phase 5 additions — CSE/study/hackathon student-assistant modes
    # and the Section 7 developer/debugging mode toggle. DSA/INTERVIEW
    # route through the same CSE assistant as general programming
    # questions (assistants/cse_assistant.py); HACKATHON is its own
    # intent because Section 8's capabilities (idea generation,
    # architecture, pitch prep) are a distinct shape of request from
    # ordinary CSE Q&A, not a debugging or study task.
    # Phase 6+: SYSTEM SECURITY + STORAGE CLEANER. Two intents rather
    # than one — "scan my system for threats" and "clean junk files"
    # are different enough in shape (one queries Defender, the other
    # walks the filesystem and can delete) that a single SYSTEM intent
    # would need a second classification step anyway; keeping them
    # separate lets the router's own confidence do that work.
    HACKATHON = "HACKATHON"
    DEVELOPER_MODE = "DEVELOPER_MODE"
    SYSTEM_SECURITY = "SYSTEM_SECURITY"
    SYSTEM_STORAGE = "SYSTEM_STORAGE"


# Intents with a real subsystem behind them today. Everything else in
# the Intent enum classifies correctly but currently routes back to
# GENERAL_CHAT — see route_intent() below. Update this set as later
# phases land real handlers.
#
# Phase 3: DEBUG -> debug_mode.py's Investigation via the
# debug_investigation tool; CODE_ANALYSIS -> code_analysis.py's
# analyze_file via the analyze_code tool; CODE_EXPLANATION ->
# code_analysis.py's explain_code tool; TERMINAL -> a dedicated
# handler in main.py that extracts a command from the message and
# runs it through run_terminal_command's own permission gate (see
# main.py's _handle_phase3_intent). This does NOT bypass DEBUG — a
# "why isn't this working" question still classifies as DEBUG, not
# TERMINAL; TERMINAL is specifically for "run X" / "what does X show"
# requests that name an actual command.
#
# Phase 4 (Feature 1, landed): SCREEN_ANALYSIS -> screen_tools.py's
# analyze_screen via main.py's _handle_phase3_intent (always goes
# through the CONFIRM gate — see tool_registry.py's screen tools).
# Phase 4 (Feature 4, landed): GIT -> git_tools.py's
# status/diff/log/branch/summary/commit-message/merge-conflict
# functions — all SAFE, see git_tools.py's module docstring for why.
# Phase 5: STUDY/DSA/INTERVIEW -> assistants/cse_assistant.py or
# assistants/study_assistant.py (main.py picks based on the message);
# PLANNING -> core/task_planner.py; HACKATHON ->
# assistants/hackathon_assistant.py; DEVELOPER_MODE ->
# assistants/developer_assistant.py (mode toggle, reuses debug_mode.py
# for the actual investigation exactly as DEBUG already does).
# RESEARCH is deliberately left unimplemented — Phase 5's scope is CSE
# study support, not open-ended web research, so it still falls back
# to GENERAL_CHAT rather than pretending to do research it can't.
# PROJECT_ANALYSIS/PROJECT_MEMORY/FILE_OPERATION/SYSTEM_MONITOR still
# fall back to GENERAL_CHAT — their tools exist (Phase 2) but no
# dedicated intent-level handler routes to them yet.
# Phase 6: PROJECT_ANALYSIS -> workflow_engine.py's "project_review"
# workflow kind (workflows/project_review.py) — a bounded, read-only,
# evidence-based multi-step review, not a single tool call. See
# main.py's _handle_phase3_intent for the PROJECT_ANALYSIS branch.
_IMPLEMENTED_INTENTS = {
    Intent.GENERAL_CHAT,
    Intent.DEBUG,
    Intent.CODE_ANALYSIS,
    Intent.CODE_EXPLANATION,
    Intent.TERMINAL,
    Intent.SCREEN_ANALYSIS,
    Intent.GIT,
    Intent.STUDY,
    Intent.DSA,
    Intent.INTERVIEW,
    Intent.PLANNING,
    Intent.HACKATHON,
    Intent.DEVELOPER_MODE,
    Intent.PROJECT_ANALYSIS,
    Intent.SYSTEM_SECURITY,
    Intent.SYSTEM_STORAGE,
}

# Tool-touching intents that should be treated as requiring tool use
# downstream, distinct from ones that are pure conversation.
_TOOL_INTENTS = {
    Intent.TERMINAL,
    Intent.GIT,
    Intent.FILE_OPERATION,
    Intent.SCREEN_ANALYSIS,
    Intent.SYSTEM_MONITOR,
    Intent.PROJECT_ANALYSIS,
    Intent.PROJECT_MEMORY,
    Intent.CODE_ANALYSIS,
    Intent.SYSTEM_SECURITY,
    Intent.SYSTEM_STORAGE,
}


@dataclass
class IntentResult:
    intent: Intent
    confidence: float
    requires_tools: bool
    requires_confirmation: bool
    raw: Optional[dict] = None
    # Only meaningful when intent == CODE_EXPLANATION — one of
    # code_analysis.py's EXPLANATION_MODES (Section 4/5). None means
    # no mode was explicitly requested/detected; callers should treat
    # that as "use a sensible default", not as an error.
    mode: Optional[str] = None


_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [i.value for i in Intent],
        },
        "confidence": {"type": "number"},
        "mode": {
            "type": ["string", "null"],
            "enum": ["BEGINNER", "LINE_BY_LINE", "TECHNICAL", "INTERVIEW", "EXAM", "ELI5", None],
        },
    },
    "required": ["intent", "confidence"],
}

_SYSTEM_PROMPT = f"""You are an intent classifier for a personal AI assistant.
Classify the user's message into exactly one of these intents:
{', '.join(i.value for i in Intent)}

Guidelines:
- GENERAL_CHAT: greetings, small talk, general questions with no clear category below.
- CODE_ANALYSIS / CODE_EXPLANATION / DEBUG: about existing code, errors, or "why isn't this working".
- PROJECT_ANALYSIS / PROJECT_MEMORY: about the structure, architecture, or facts of the current project.
- TERMINAL: running or explaining shell/terminal commands.
- GIT: git status/diff/log/commits/branches.
- FILE_OPERATION: reading, listing, or modifying files.
- SCREEN_ANALYSIS: about what's currently on screen or in the active window.
- SYSTEM_MONITOR: CPU/memory/process/system health questions.
- STUDY / DSA / INTERVIEW: exam prep, data structures & algorithms, interview practice.
- RESEARCH: open-ended research on a topic.
- PLANNING: project/task planning, milestones, schedules, or multi-step preparation requests
  (e.g. "prepare my environment for the hackathon", "help me prepare for tomorrow's exam").
- HACKATHON: hackathon ideas, problem-statement analysis, architecture/tech-stack recommendations,
  MVP/task breakdown, pitch or demo prep, or judging/risk assessment for a hackathon project.
- DEVELOPER_MODE: explicitly entering/exiting a developer/debugging mode
  ("enter developer mode", "debug this", "explain the error").
- SYSTEM_SECURITY: security/virus/threat scans and Windows Security/Defender status
  ("scan my system for threats", "check my PC for viruses", "is my computer safe?",
  "run a full security scan").
- SYSTEM_STORAGE: disk space, storage analysis, junk files, large files, or cleanup
  ("analyze my storage", "what's taking up my storage?", "find junk files",
  "clean temporary files", "how much free space do I have?", "find large files").

If, and only if, the intent is CODE_EXPLANATION, also detect which explanation
mode was requested: BEGINNER, LINE_BY_LINE, TECHNICAL, INTERVIEW, EXAM, or ELI5.
If no mode is clearly implied, set mode to null. For every other intent, mode
must be null.

Respond with JSON only: {{"intent": "<ONE_OF_THE_ABOVE>", "confidence": <0.0-1.0>, "mode": <MODE_OR_null>}}"""


def _fallback_result(reason: str) -> IntentResult:
    logger.warning(f"[intent] Falling back to GENERAL_CHAT: {reason}")
    return IntentResult(
        intent=Intent.GENERAL_CHAT,
        confidence=0.0,
        requires_tools=False,
        requires_confirmation=False,
    )


def classify(message: str) -> IntentResult:
    """
    Classify a single user message. Synchronous (matches main.py's
    existing ask_ollama(), which is also sync and run via
    asyncio.to_thread by callers).
    """
    if not message or not message.strip():
        return _fallback_result("empty message")

    try:
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            format=_INTENT_SCHEMA,
            options={"temperature": 0.1, "num_predict": 60},
        )
        raw_content = response["message"]["content"]
        parsed = json.loads(raw_content)

        intent_str = parsed.get("intent", Intent.GENERAL_CHAT.value)
        try:
            intent = Intent(intent_str)
        except ValueError:
            logger.warning(f"[intent] Model returned unknown intent '{intent_str}'")
            intent = Intent.GENERAL_CHAT

        confidence = float(parsed.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        requires_tools = intent in _TOOL_INTENTS
        # Anything that would eventually touch files/terminal/git needs
        # confirmation downstream too, per the Section 8 permission
        # model — the router just flags this; permissions.py enforces it.
        requires_confirmation = intent in {
            Intent.TERMINAL, Intent.FILE_OPERATION, Intent.GIT,
        }

        # Mode only means anything for CODE_EXPLANATION — validated
        # against code_analysis.py's actual EXPLANATION_MODES rather
        # than trusted blindly, since the model could still return
        # something outside the schema's enum in principle.
        mode = None
        if intent == Intent.CODE_EXPLANATION:
            raw_mode = parsed.get("mode")
            if raw_mode:
                from code_analysis import EXPLANATION_MODES
                candidate = str(raw_mode).upper()
                if candidate in EXPLANATION_MODES:
                    mode = candidate
                else:
                    logger.warning(f"[intent] Model returned unknown explanation mode '{raw_mode}', ignoring.")

        return IntentResult(
            intent=intent,
            confidence=confidence,
            requires_tools=requires_tools,
            requires_confirmation=requires_confirmation,
            raw=parsed,
            mode=mode,
        )

    except json.JSONDecodeError as e:
        return _fallback_result(f"non-JSON response from model: {e}")
    except Exception as e:
        return _fallback_result(f"classification failed: {e}")


def route_intent(result: IntentResult) -> Intent:
    """
    Decide which subsystem should actually handle this. Right now this
    only knows about GENERAL_CHAT; anything else without a real
    handler behind it falls back to GENERAL_CHAT rather than 404ing or
    pretending to run a feature from a later phase.
    """
    if result.intent in _IMPLEMENTED_INTENTS:
        return result.intent
    logger.info(
        f"[intent] '{result.intent.value}' classified but no subsystem implemented yet — "
        f"routing to GENERAL_CHAT."
    )
    return Intent.GENERAL_CHAT
