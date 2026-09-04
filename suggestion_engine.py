"""
Phase 6 Feature 7 — Proactive Suggestion Engine.

Genuinely proactive on the GENERATION side: a Suggestion is created
automatically, without the user asking, whenever the automatic health
monitor (Feature 6's `project_health._auto_loop`) notices something
new or changed worth flagging. Retrieval is on-demand (a "suggestions"
chat command) — the same on-demand-report shape Feature 6 itself
already uses for its non-automatic half. Injecting unsolicited text
into the middle of an arbitrary chat reply was deliberately avoided:
it would be surprising mid-conversation, and would silently change the
reply content asserted by every existing intent handler's tests.

Not a second logging system and not a second inference engine: the
only input is project_health.py's own `ProjectHealthReport.attention`
list (already-computed signals, Feature 6), and every suggestion
raised is also written through the existing `memory.log_event()`
(Feature 16) — nothing new is independently detected or logged here.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Suggestion:
    id: str
    project_path: str
    text: str
    created_at: float = field(default_factory=time.monotonic)
    dismissed: bool = False


def _actionable(attention_item: str) -> str:
    """Turn a passive health-report line into a suggested next action.
    Purely a phrasing transform on text the health monitor already
    produced — it doesn't add any signal of its own."""
    lowered = attention_item.lower()
    if "missing dependenc" in lowered:
        return f"{attention_item} Want me to install them?"
    if "uncommitted change" in lowered:
        return f"{attention_item} Want me to walk through what's changed?"
    if "failed" in lowered and "time" in lowered:
        return f"{attention_item} Want me to look into why?"
    return attention_item


class SuggestionEngine:
    def __init__(self) -> None:
        self._suggestions: dict[str, list[Suggestion]] = {}  # project_path -> suggestions
        self._MAX_PER_PROJECT = 20  # bounded on purpose — the audit log is the history; this is "what's current"

    def record_health_alert(self, project_path: str, attention: list[str]) -> list[Suggestion]:
        """Called by project_health.py's automatic monitor whenever it
        raises a new/changed alert (already de-duped there — see
        ProjectHealthMonitor._check_and_notify). One Suggestion per
        attention line."""
        if not attention:
            return []
        bucket = self._suggestions.setdefault(project_path, [])
        created = [
            Suggestion(id=str(uuid.uuid4())[:8], project_path=project_path, text=_actionable(item))
            for item in attention
        ]
        bucket.extend(created)
        if len(bucket) > self._MAX_PER_PROJECT:
            del bucket[: len(bucket) - self._MAX_PER_PROJECT]

        import memory
        for s in created:
            memory.log_event("suggestion:raised", f"[{project_path}] {s.text}"[:2000])
        return created

    def get_pending(self, project_path: str) -> list[Suggestion]:
        return [s for s in self._suggestions.get(project_path, []) if not s.dismissed]

    def dismiss_all(self, project_path: str) -> int:
        """Explicit user action, mirroring Feature 16's "clear logs"
        shape — returns the count dismissed, 0 for none pending."""
        pending = self.get_pending(project_path)
        for s in pending:
            s.dismissed = True
        return len(pending)


suggestion_engine = SuggestionEngine()
