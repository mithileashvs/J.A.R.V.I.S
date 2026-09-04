"""
JARVIS session context extension (Phase 5, Section 1).

context_manager.py (Phase 3) already owns conversation history, active
window, active project, and project facts — this module does NOT
duplicate any of that. It adds exactly the pieces Section 1 asks for
that nothing before Phase 5 tracked: current assistant mode
(developer/study/hackathon/cse/general), current task description, and
a small per-session "active tools used this turn" list — then exposes
the flat API Section 1 asks for (get_context / update_context /
get_recent_context / clear_context) as a thin layer over
context_manager.context_manager plus this module's own session state.

State here is per session_id (a dict keyed by session_id), unlike
context_manager's single active-project pointer — sessions can
legitimately be in different modes at once (e.g. a text session doing
exam prep while voice is mid-debug), whereas "active project" is
process-global in Phase 3 and is left exactly as-is.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from context_manager import GatheredContext, context_manager

logger = logging.getLogger("jarvis-session-context")


@dataclass
class SessionState:
    mode: str = "general"                     # general | cse | study | hackathon | developer
    current_task: Optional[str] = None
    active_tools: list[str] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionContextStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def _get_or_create(self, session_id: str) -> SessionState:
        return self._sessions.setdefault(session_id, SessionState())

    def update_context(
        self,
        session_id: str,
        *,
        mode: Optional[str] = None,
        current_task: Optional[str] = None,
        active_tool: Optional[str] = None,
    ) -> SessionState:
        state = self._get_or_create(session_id)
        if mode is not None:
            state.mode = mode
        if current_task is not None:
            state.current_task = current_task
        if active_tool is not None and active_tool not in state.active_tools:
            state.active_tools.append(active_tool)
            # Bounded — this is "what ran recently", not an audit log.
            state.active_tools = state.active_tools[-10:]
        state.updated_at = datetime.now(timezone.utc)
        return state

    def get_session_state(self, session_id: str) -> SessionState:
        return self._get_or_create(session_id)

    def clear_context(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


session_context_store = SessionContextStore()


def get_context(
    session_id: str,
    fact_query: Optional[str] = None,
) -> GatheredContext:
    """
    Full Section 1 context snapshot for a session: everything
    context_manager.gather() already assembles, plus this module's
    mode/task/active-tools additions attached as extra attributes so
    existing consumers of GatheredContext (debug_mode.py,
    code_analysis.py) are unaffected — they simply won't look at the
    new attributes, and nothing here changes GatheredContext's own
    dataclass fields or to_prompt_block() output for them.
    """
    ctx = context_manager.gather(session_id=session_id, fact_query=fact_query)
    state = session_context_store.get_session_state(session_id)
    ctx.mode = state.mode                    # type: ignore[attr-defined]
    ctx.current_task = state.current_task    # type: ignore[attr-defined]
    ctx.active_tools = list(state.active_tools)  # type: ignore[attr-defined]
    return ctx


def update_context(
    session_id: str,
    *,
    mode: Optional[str] = None,
    current_task: Optional[str] = None,
    active_tool: Optional[str] = None,
    active_project_path: Optional[str] = None,
) -> None:
    if active_project_path is not None:
        # Delegates to context_manager's own pointer rather than
        # keeping a second copy — see module docstring.
        context_manager.set_active_project(active_project_path)
    session_context_store.update_context(
        session_id, mode=mode, current_task=current_task, active_tool=active_tool,
    )


def get_recent_context(session_id: str, limit: int = 6) -> list[dict]:
    """Thin passthrough to memory.get_history — kept here so callers only need one import for Section 1's API."""
    import memory
    return memory.get_history(session_id, limit=limit)


def clear_context(session_id: str) -> None:
    session_context_store.clear_context(session_id)


def context_to_prompt_block(session_id: str, fact_query: Optional[str] = None) -> str:
    """
    Convenience for assistants: get_context() + mode/task line +
    GatheredContext.to_prompt_block(), so cse_assistant.py etc. don't
    each need to know GatheredContext's internal shape.
    """
    ctx = get_context(session_id, fact_query=fact_query)
    lines = []
    mode = getattr(ctx, "mode", "general")
    task = getattr(ctx, "current_task", None)
    if mode and mode != "general":
        lines.append(f"Current mode: {mode}")
    if task:
        lines.append(f"Current task: {task}")
    base = ctx.to_prompt_block()
    if base and base != "(no context available)":
        lines.append(base)
    return "\n".join(lines) if lines else "(no context available)"
