"""
JARVIS reference resolver (Phase 5, Sections 2 & 10).

Resolves contextual commands ("open it", "explain the second one",
"the file we were working on", "try another method") against recent
conversation memory, so the LLM orchestrator gets a de-referenced
message instead of a pronoun it can't ground.

This is deliberately a *text-in, text-out plus a confidence score*
module — it does not call an LLM and does not execute anything. It
reuses memory.py's existing conversation history (via context_manager,
which already loads it) rather than introducing a second history
store. If it can't resolve a reference with reasonable confidence, it
says so honestly (low confidence) instead of guessing — callers
(main.py, core/confidence.py) decide whether that means "ask for
clarification".
"""

import re
from dataclasses import dataclass, field
from typing import Optional

# Pronouns/phrases that signal the message depends on prior context.
# Checked as whole words/phrases against a lowercased message so
# "explanation" doesn't false-positive on "it".
_REFERENCE_PATTERNS = [
    r"\bit\b", r"\bthat\b", r"\bthose\b", r"\bthem\b",
    r"\bthe (first|second|third|fourth|fifth|last|previous|next) one\b",
    r"\bthe (first|second|third|fourth|fifth) option\b",
    r"\bthe (previous|last) (one|answer|response|result|file|command)\b",
    r"\bthe (same|other) (thing|approach|method|way)\b",
    r"\banother (method|approach|way|one)\b",
    r"\bthe file we were working on\b",
    r"\bmake it faster\b",
    r"\bsearch more about that\b",
]
_REFERENCE_RE = re.compile("|".join(_REFERENCE_PATTERNS), re.IGNORECASE)

_ORDINAL_WORDS = {
    "first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4,
}

# A numbered-list line inside a previous JARVIS response, e.g.
# "1. Python decorators" or "2) Binary search".
_LIST_ITEM_RE = re.compile(r"^\s*(\d+)[.)]\s+(.+)$", re.MULTILINE)

# A bare filename mention, e.g. "intent_router.py" — used to resolve
# "the file we were working on" without needing project_memory.
_FILENAME_RE = re.compile(r"\b[\w\-]+\.(py|js|ts|tsx|jsx|java|c|cpp|h|hpp|cs|go|rs|sql|html|css|md)\b")


@dataclass
class ResolvedReference:
    original_message: str
    resolved_message: str
    was_referential: bool
    confidence: float           # 0.0 (couldn't resolve) .. 1.0 (certain)
    resolved_entity: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def _find_last_list(recent_messages: list[dict]) -> list[str]:
    """Most recent numbered list JARVIS produced, e.g. after 'search for X'."""
    for msg in reversed(recent_messages):
        if msg.get("role") != "assistant":
            continue
        items = _LIST_ITEM_RE.findall(msg.get("content") or "")
        if items:
            return [text.strip() for _, text in items]
    return []


def _find_last_filename(recent_messages: list[dict]) -> Optional[str]:
    for msg in reversed(recent_messages):
        match = _FILENAME_RE.search(msg.get("content") or "")
        if match:
            return match.group(0)
    return None


def _find_last_topic(recent_messages: list[dict]) -> Optional[str]:
    """
    Best-effort 'what were we just talking about' — the most recent
    user message that isn't itself referential, used as a fallback for
    bare 'it'/'that' when there's no numbered list or filename to
    anchor to.
    """
    for msg in reversed(recent_messages):
        if msg.get("role") != "user":
            continue
        content = (msg.get("content") or "").strip()
        if content and not _REFERENCE_RE.search(content):
            return content
    return None


def resolve(message: str, recent_messages: list[dict]) -> ResolvedReference:
    """
    recent_messages: the same shape context_manager.gather() already
    loads (list of {"role", "content", ...}), most-recent last.
    """
    if not message or not _REFERENCE_RE.search(message):
        return ResolvedReference(
            original_message=message,
            resolved_message=message,
            was_referential=False,
            confidence=1.0,
        )

    lower = message.lower()
    notes: list[str] = []

    # 1. Ordinal references ("the second one", "the third option") —
    # resolve against the most recent numbered list JARVIS produced.
    ordinal_match = re.search(r"\b(first|second|third|fourth|fifth)\b", lower)
    if ordinal_match:
        items = _find_last_list(recent_messages)
        idx = _ORDINAL_WORDS[ordinal_match.group(1)]
        if items and idx < len(items):
            entity = items[idx]
            resolved = _REFERENCE_RE.sub(entity, message, count=1)
            return ResolvedReference(
                original_message=message,
                resolved_message=resolved,
                was_referential=True,
                confidence=0.9,
                resolved_entity=entity,
                notes=[f"Resolved ordinal reference to item {idx + 1} of last list: {entity!r}"],
            )
        notes.append("Ordinal reference found but no recent numbered list to resolve it against.")

    # 2. File references ("the file we were working on", "open it"
    # when the last thing mentioned was a filename).
    if "file" in lower or re.search(r"\bopen it\b|\bclose that\b", lower):
        filename = _find_last_filename(recent_messages)
        if filename:
            resolved = _REFERENCE_RE.sub(filename, message, count=1)
            return ResolvedReference(
                original_message=message,
                resolved_message=resolved,
                was_referential=True,
                confidence=0.75,
                resolved_entity=filename,
                notes=[f"Resolved file reference to last-mentioned file: {filename!r}"],
            )
        notes.append("File reference found but no filename recently mentioned.")

    # 3. Generic 'it'/'that'/'the previous one' — fall back to the
    # last non-referential user message as the topic being referenced.
    topic = _find_last_topic(recent_messages)
    if topic:
        resolved = f"{message.strip()} (regarding: {topic})"
        return ResolvedReference(
            original_message=message,
            resolved_message=resolved,
            was_referential=True,
            confidence=0.5,
            resolved_entity=topic,
            notes=notes + [f"Resolved generic reference to last topic: {topic!r}"],
        )

    # Nothing to anchor to — honest low-confidence result rather than a guess.
    return ResolvedReference(
        original_message=message,
        resolved_message=message,
        was_referential=True,
        confidence=0.0,
        notes=notes + ["No prior context available to resolve this reference."],
    )
