"""
JARVIS context manager (Phase 3, Section 2).

The one piece Phase 1/2 didn't build: an aggregator that pulls
together what's needed to answer "why isn't this working?" without
the user re-explaining everything each time. This module does not
duplicate any existing system — it only calls into them and shapes
the result:

    conversation history  -> memory.py
    active window          -> awareness.py
    active project         -> project_memory.py (+ a new "current
                               project" pointer this module owns,
                               since nothing before Phase 3 tracked
                               that concept)
    project facts           -> project_memory.py
    relevant files           -> project_detector.py's structure scan

Deliberately NOT built here: code analysis, terminal reading, git
inspection. Those are Phase 3's other subsystems (code_analysis.py,
terminal_tools.py) and this module will call into them once they
exist, the same way it calls into Phase 1/2's systems now — it does
not reimplement them.

Context-gathering is bounded on purpose (Section 2: "do not
automatically read the entire project for every request"):
  - conversation history capped at a small recent window
  - project facts capped and, where possible, filtered by relevance
    to the current message rather than dumped in full
  - no file *contents* are read here at all — only metadata (paths,
    structure) — reading actual file contents is code_analysis.py's
    job, invoked deliberately, not as a side effect of building context
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("jarvis-context")

# Bounds — see module docstring on why these exist. Numbers are
# starting points, not tuned against real usage yet; flagged here so
# they're easy to find and adjust later rather than buried inline.
_CONVERSATION_HISTORY_LIMIT = 6
_PROJECT_FACTS_LIMIT = 8


@dataclass
class GatheredContext:
    """
    Everything context_manager could assemble for one request. Every
    field is Optional/empty-default rather than required, because
    Section 2 explicitly expects partial context to be normal (no
    active project detected, no window info on non-Windows, etc.) —
    downstream consumers (debug_mode.py, code_analysis.py) must handle
    missing pieces rather than assume this is always fully populated.
    """
    session_id: Optional[str] = None
    recent_messages: list[dict] = field(default_factory=list)

    active_window_title: Optional[str] = None
    active_window_available: bool = False

    active_project_path: Optional[str] = None
    active_project: Optional[dict] = None          # project_memory.get_project() result
    project_facts: list[dict] = field(default_factory=list)

    # Populated by callers that already did a targeted lookup (e.g.
    # debug_mode.py after inspecting a traceback) rather than by this
    # module itself — context_manager never reads file contents.
    relevant_file_paths: list[str] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)  # what couldn't be gathered and why

    def to_prompt_block(self) -> str:
        """
        Render as a compact text block suitable for inclusion in an
        LLM prompt. Keeps it short deliberately — this is context to
        ground a response, not a full dump of everything gathered.
        """
        lines = []

        if self.active_project:
            techs = ", ".join(self.active_project.get("technologies", [])) or "unknown stack"
            lines.append(f"Active project: {self.active_project['name']} ({techs})")
        elif self.active_project_path:
            lines.append(f"Active project path (not yet indexed): {self.active_project_path}")

        if self.active_window_available and self.active_window_title:
            lines.append(f"Active window: {self.active_window_title}")

        if self.project_facts:
            lines.append("Known project facts:")
            for f in self.project_facts:
                lines.append(f"  - [{f['kind']}] {f['content']}")

        if self.relevant_file_paths:
            lines.append("Relevant files: " + ", ".join(self.relevant_file_paths))

        if self.recent_messages:
            lines.append("Recent conversation:")
            for m in self.recent_messages[-_CONVERSATION_HISTORY_LIMIT:]:
                role = m.get("role", "?")
                content = (m.get("content") or "")[:200]
                lines.append(f"  {role}: {content}")

        return "\n".join(lines) if lines else "(no context available)"


class ContextManager:
    """
    Tracks the "current project" pointer (a concept nothing before
    Phase 3 owned) and gathers GatheredContext on demand. Stateless
    beyond that one pointer — everything else is fetched fresh from
    memory.py/project_memory.py/awareness.py each call, so this never
    goes stale by holding a cached copy of something that changed
    elsewhere.
    """

    def __init__(self) -> None:
        self._active_project_path: Optional[str] = None

    def set_active_project(self, path: Optional[str]) -> None:
        """
        Explicitly mark a project as the one currently being worked
        on. Nothing infers this automatically yet (that would need
        active-window -> path resolution, which isn't reliable without
        IDE-specific integration) — for now this is set by whatever
        tool call establishes it, e.g. after inspect_project runs
        against a path the user named.
        """
        self._active_project_path = path

    def get_active_project_path(self) -> Optional[str]:
        return self._active_project_path

    def gather(
        self,
        session_id: Optional[str] = None,
        include_window: bool = True,
        include_project_facts: bool = True,
        fact_query: Optional[str] = None,
    ) -> GatheredContext:
        """
        Assemble a GatheredContext. Every sub-gather is wrapped
        individually so one failing system (e.g. awareness.py on an
        unsupported platform) doesn't prevent gathering the rest —
        failures land in .warnings instead of raising.
        """
        ctx = GatheredContext(session_id=session_id)

        if session_id:
            try:
                import memory
                ctx.recent_messages = memory.get_history(session_id, limit=_CONVERSATION_HISTORY_LIMIT)
            except Exception as e:
                ctx.warnings.append(f"Could not load conversation history: {e}")

        if include_window:
            try:
                import awareness
                window = awareness.get_active_window()
                ctx.active_window_available = window.get("available", False)
                ctx.active_window_title = window.get("title")
                if not ctx.active_window_available:
                    ctx.warnings.append(window.get("reason", "Active window unavailable."))
            except Exception as e:
                ctx.warnings.append(f"Could not read active window: {e}")

        if self._active_project_path:
            ctx.active_project_path = self._active_project_path
            try:
                import project_memory as pm
                ctx.active_project = pm.get_project(self._active_project_path)
                if ctx.active_project is None:
                    ctx.warnings.append(
                        f"Project path '{self._active_project_path}' is set but not yet indexed "
                        f"— run inspect_project on it first."
                    )
                elif include_project_facts:
                    if fact_query:
                        ctx.project_facts = pm.search_facts(
                            self._active_project_path, fact_query, limit=_PROJECT_FACTS_LIMIT
                        )
                    else:
                        ctx.project_facts = pm.get_facts(
                            self._active_project_path, limit=_PROJECT_FACTS_LIMIT
                        )
            except Exception as e:
                ctx.warnings.append(f"Could not load project memory: {e}")
        else:
            ctx.warnings.append("No active project set.")

        return ctx


# Single shared instance, matching state_manager/permission_manager/
# tool_registry's pattern elsewhere in the codebase.
context_manager = ContextManager()
