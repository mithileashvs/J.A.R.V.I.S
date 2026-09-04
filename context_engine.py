"""
JARVIS CONTEXT AWARENESS — "What am I doing?"

Adds exactly the layer nothing before this existed: a single
CURRENT_CONTEXT object answering "what is the user currently working
with on the computer" (never "what are they thinking" — Section 23).

This module explicitly does NOT duplicate anything already built:

    conversation history + active window + active project + project
    facts                                   -> context_manager.py (Phase 3)
    assistant mode + current task            -> core/session_context.py (Phase 5)
    screen capture + OCR + error extraction  -> screen_tools.py (Phase 4)
    project structure detection              -> project_detector.py / project_memory.py
    text-based "it"/"that"/ordinal resolution -> core/reference_resolver.py

ContextEngine only ADDS: active_application (a human name, not just
context_manager's raw window title), project-name<->window-title
matching, current-file extraction, recent-error/recent-action tracking
with staleness, and a *second* reference-resolution pass for the
words core/reference_resolver.py deliberately doesn't cover ("this",
"here", "check this", "this file", "this screen" — see
_COMPUTER_CONTEXT_REFERENCE_RE below) that need the computer context
rather than conversation text to resolve.

── PIPELINE (Section 1) ────────────────────────────────────────────

    COMPUTER -> ACTIVE WINDOW -> APPLICATION -> PROJECT -> CURRENT FILE
    -> SCREEN CONTEXT (only if asked) -> RECENT ACTIVITY -> CONVERSATION
    -> MODE -> CURRENT_CONTEXT

Every stage is wrapped so one failing source doesn't block the rest —
same pattern as context_manager.gather().

── NEVER FABRICATE (Section 2/23) ──────────────────────────────────

Every field defaults to None/empty/UNKNOWN. A field is only populated
when a real signal was actually read this call. Nothing here infers
private thoughts, intent, or emotion — only what's observably active
on the computer.

── PERFORMANCE (Section 19) ────────────────────────────────────────

gather() is cheap by default: one active-window read + whatever
context_manager/session_context already do (no filesystem scan, no
screen capture, no LLM call). include_screen=True is opt-in and only
set by main.py when the message itself asks for visual context (see
wants_screen_context()) — this is the ONLY path that triggers a
screenshot from this module, and it's exactly one on-demand call to
the existing screen_tools.analyze_screen(), not a new capture
implementation.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("jarvis-context-engine")

# Section 15 — don't keep stale context forever.
_ERROR_STALE_SECONDS = 30 * 60
_ACTION_STALE_SECONDS = 30 * 60
_RECENT_ACTIONS_KEPT = 10

# ── Application display-name detection (Section 3) ──────────────────
# Same substring-match approach as screen_tools._classify_application,
# but returns a human display name ("Visual Studio Code") instead of a
# coarse category ("IDE") — the two serve different purposes and
# deliberately aren't merged: screen_tools needs "is this the kind of
# window worth OCR'ing", this needs "what should JARVIS call it out
# loud". None (not a guess) when nothing matches.
_APP_DISPLAY_NAMES = [
    ("Visual Studio Code", ("visual studio code", "vscode")),
    ("PyCharm", ("pycharm",)),
    ("IntelliJ IDEA", ("intellij",)),
    ("WebStorm", ("webstorm",)),
    ("Sublime Text", ("sublime text",)),
    ("Android Studio", ("android studio",)),
    ("Eclipse", ("eclipse",)),
    ("Rider", ("rider",)),
    ("Windows Terminal", ("windows terminal",)),
    ("PowerShell", ("powershell",)),
    ("Command Prompt", ("cmd.exe", "command prompt")),
    ("WSL", ("wsl",)),
    ("Git Bash", ("git bash",)),
    ("Google Chrome", ("google chrome",)),
    ("Mozilla Firefox", ("mozilla firefox",)),
    ("Microsoft Edge", ("microsoft edge",)),
    ("Brave", (" - brave",)),
    ("Safari", ("safari",)),
    ("File Explorer", ("file explorer",)),
    ("Notepad++", ("notepad++",)),
    ("Notepad", ("notepad",)),
]

_IDE_APPS = {"Visual Studio Code", "PyCharm", "IntelliJ IDEA", "WebStorm", "Sublime Text", "Android Studio", "Eclipse", "Rider", "Notepad++"}

# Common window-title separators across apps/OS versions.
_TITLE_SEPARATORS_RE = re.compile(r"\s+[—–-]\s+")

# Section 11 — contextual words core/reference_resolver.py deliberately
# doesn't handle (it's conversation-text-only; these need computer
# context instead). Deliberately does NOT include bare "this"/"here"/
# "there" as standalone triggers — those appear in huge numbers of
# completely unrelated sentences ("explain this concept", "come here
# often") and would turn Section 12's "ask rather than guess" into
# constant, wrong interruptions. Every pattern below is a specific
# phrase/word-pair that plausibly points at the computer/screen.
_COMPUTER_CONTEXT_REFERENCE_RE = re.compile(
    r"\bcheck this\b|\blook at this\b|\bopen this\b|"
    r"\bwhat'?s wrong here\b|\bwhy is this failing\b|\bwhat am i looking at\b|"
    r"\bthis (error|file|project|screen|code)\b|\bthat code\b|"
    r"\bthe previous error\b|\bthe last result\b|\brun it\b|\bfix it\b|\bcheck it\b|"
    r"\bexplain this screen\b|\bwhat am i working on\b",
    re.IGNORECASE,
)


def _detect_active_application(window_title: str) -> Optional[str]:
    lower = window_title.lower()
    for display, sigs in _APP_DISPLAY_NAMES:
        if any(s in lower for s in sigs):
            return display
    return None


def _split_title_segments(window_title: str) -> list[str]:
    return [s.strip() for s in _TITLE_SEPARATORS_RE.split(window_title) if s.strip()]


def _looks_like_filename(segment: str) -> bool:
    # A dot followed by 1-5 word characters near the end, and no
    # spaces around the dot — cheap, deliberately conservative (a
    # false negative just means current_file stays Unknown, which is
    # the safe failure mode per Section 6/2).
    return bool(re.search(r"\w\.\w{1,5}$", segment.strip().lstrip("●* ")))


@dataclass
class CurrentContext:
    active_application: Optional[str] = None
    active_window: Optional[str] = None
    process_name: Optional[str] = None
    project_name: Optional[str] = None
    project_path: Optional[str] = None
    working_directory: Optional[str] = None
    current_file: Optional[str] = None
    current_file_confidence: str = "UNKNOWN"  # HIGH | MEDIUM | LOW | UNKNOWN
    visible_screen_available: bool = False
    screen_summary: Optional[str] = None       # only set when include_screen=True was actually used
    recent_error: Optional[dict] = None
    recent_action: Optional[dict] = None
    current_mode: str = "general"
    conversation_context: Optional[str] = None
    active_attachment: Optional[str] = None
    project_confidence: str = "UNKNOWN"        # HIGH | MEDIUM | LOW | UNKNOWN — Section 12
    ambiguous_projects: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "active_application": self.active_application,
            "active_window": self.active_window,
            "process_name": self.process_name,
            "project_name": self.project_name,
            "project_path": self.project_path,
            "working_directory": self.working_directory,
            "current_file": self.current_file,
            "current_file_confidence": self.current_file_confidence,
            "visible_screen_available": self.visible_screen_available,
            "screen_summary": self.screen_summary,
            "recent_error": self.recent_error,
            "recent_action": self.recent_action,
            "current_mode": self.current_mode,
            "conversation_context": self.conversation_context,
            "active_attachment": self.active_attachment,
            "project_confidence": self.project_confidence,
            "ambiguous_projects": self.ambiguous_projects,
            "warnings": self.warnings,
            "timestamp": self.timestamp,
        }

    def to_debug_text(self) -> str:
        """Section 21 — the exact CONTEXT DEBUG report shape."""
        def _v(x):
            return x if x not in (None, "", []) else "Unknown"

        age = f"{int(time.time() - self.timestamp)}s ago"
        lines = [
            "CONTEXT DEBUG", "",
            f"Application:\n{_v(self.active_application)}", "",
            f"Window:\n{_v(self.active_window)}", "",
            f"Project:\n{_v(self.project_name)} (confidence: {self.project_confidence})", "",
            f"File:\n{_v(self.current_file)} (confidence: {self.current_file_confidence})", "",
            f"Recent Error:\n{_v((self.recent_error or {}).get('text') if self.recent_error else None)}", "",
            f"Recent Action:\n{_v((self.recent_action or {}).get('description') if self.recent_action else None)}", "",
            f"Mode:\n{self.current_mode.upper()}", "",
            f"Screen:\n{'Available' if self.visible_screen_available else 'Not captured this turn'}", "",
        ]
        if self.ambiguous_projects:
            lines.append(f"Ambiguous project candidates:\n{', '.join(self.ambiguous_projects)}")
            lines.append("")
        if self.warnings:
            lines.append("Warnings:\n" + "\n".join(f"  - {w}" for w in self.warnings))
            lines.append("")
        lines.append(f"Timestamp:\n{age}")
        return "\n".join(lines)


@dataclass
class ContextReference:
    """Result of resolving a computer-context word ("this", "here",
    "check this") against CurrentContext — separate from, and meant to
    be tried alongside, core/reference_resolver.py's text-based
    ResolvedReference."""
    was_referential: bool
    resolved_entity: Optional[str] = None   # what "this"/"it" most likely refers to
    resolved_kind: Optional[str] = None     # "file" | "project" | "error" | "screen" | "action"
    confidence: float = 0.0
    wants_screen: bool = False
    notes: list[str] = field(default_factory=list)


class ContextEngine:
    def __init__(self) -> None:
        self._recent_errors: dict[str, dict] = {}    # session_id -> {text, source, file, line, timestamp}
        self._recent_actions: dict[str, list[dict]] = {}  # session_id -> [{description, timestamp}, ...]

    # ── recording (Section 8/9) ─────────────────────────────────────
    def record_error(self, session_id: str, text: str, source: str, file: Optional[str] = None, line: Optional[int] = None) -> None:
        if not text:
            return
        self._recent_errors[session_id] = {
            "text": text[:500], "source": source, "file": file, "line": line, "timestamp": time.time(),
        }

    def clear_error(self, session_id: str) -> None:
        self._recent_errors.pop(session_id, None)

    def record_action(self, session_id: str, description: str) -> None:
        if not description:
            return
        actions = self._recent_actions.setdefault(session_id, [])
        actions.append({"description": description, "timestamp": time.time()})
        self._recent_actions[session_id] = actions[-_RECENT_ACTIONS_KEPT:]

    def _get_recent_error(self, session_id: str) -> Optional[dict]:
        entry = self._recent_errors.get(session_id)
        if entry and (time.time() - entry["timestamp"]) > _ERROR_STALE_SECONDS:
            self._recent_errors.pop(session_id, None)
            return None
        return entry

    def _get_recent_action(self, session_id: str) -> Optional[dict]:
        actions = self._recent_actions.get(session_id)
        if not actions:
            return None
        latest = actions[-1]
        if (time.time() - latest["timestamp"]) > _ACTION_STALE_SECONDS:
            return None
        return latest

    # ── project/file detection from the active window (Section 5/6) ─
    def _detect_project_and_file(self, window_title: str, application: Optional[str]) -> tuple[
        Optional[str], Optional[str], str, Optional[str], str, list[str],
    ]:
        """Returns (project_name, project_path, project_confidence,
        current_file, file_confidence, ambiguous_project_candidates).
        Only ever matches against ALREADY-KNOWN projects
        (project_memory.list_projects()) or the currently-set
        context_manager active-project pointer — never scans the
        filesystem (Section 5/19)."""
        segments = _split_title_segments(window_title)
        project_name = None
        project_path = None
        project_confidence = "UNKNOWN"
        current_file = None
        file_confidence = "UNKNOWN"
        ambiguous: list[str] = []

        is_ide = application in _IDE_APPS
        if is_ide and segments:
            # Typical IDE title shape: "filename — projectname — App"
            if _looks_like_filename(segments[0]):
                current_file = segments[0].lstrip("●* ").strip()
                file_confidence = "HIGH"

            candidate_names = segments[1:-1] if len(segments) >= 3 else segments[1:]
            if candidate_names:
                try:
                    import project_memory as pm
                    known = pm.list_projects()
                except Exception as e:
                    known = []
                    logger.warning(f"[context_engine] Could not list known projects: {e}")

                matches = []
                for cand in candidate_names:
                    for proj in known:
                        if proj.get("name", "").lower() == cand.lower():
                            matches.append(proj)

                if len(matches) == 1:
                    project_name = matches[0]["name"]
                    project_path = matches[0]["path"]
                    project_confidence = "HIGH"
                elif len(matches) > 1:
                    # Same name matched more than one known project path —
                    # genuinely ambiguous, never silently pick one (Section 12).
                    ambiguous = [m["path"] for m in matches]
                    project_name = matches[0]["name"]
                    project_confidence = "LOW"
                elif candidate_names:
                    # A plausible project-ish segment exists in the title
                    # but doesn't match anything JARVIS has indexed yet —
                    # report the raw name, no path (never fabricate one).
                    project_name = candidate_names[0]
                    project_confidence = "MEDIUM"

        return project_name, project_path, project_confidence, current_file, file_confidence, ambiguous

    # ── main pipeline (Section 1) ───────────────────────────────────
    def gather(
        self,
        session_id: str,
        include_screen: bool = False,
        active_attachment: Optional[str] = None,
    ) -> CurrentContext:
        ctx = CurrentContext()

        # COMPUTER -> ACTIVE WINDOW -> APPLICATION
        try:
            import awareness
            window = awareness.get_active_window()
            if window.get("available"):
                ctx.active_window = window.get("title")
                ctx.active_application = _detect_active_application(ctx.active_window or "")
                if ctx.active_application is None:
                    ctx.warnings.append("Active window detected but application type unrecognized.")
            else:
                ctx.warnings.append(window.get("reason", "Active window unavailable."))
        except Exception as e:
            ctx.warnings.append(f"Could not read active window: {e}")

        # PROJECT / CURRENT FILE — from the window title + already-
        # known projects (no filesystem scan here — Section 5/19).
        # Falls back to context_manager's own explicitly-set active
        # project pointer when window-title parsing found nothing.
        try:
            import context_manager as cm
            if ctx.active_window:
                (
                    ctx.project_name, ctx.project_path, ctx.project_confidence,
                    ctx.current_file, ctx.current_file_confidence, ctx.ambiguous_projects,
                ) = self._detect_project_and_file(ctx.active_window, ctx.active_application)

            if ctx.project_path is None:
                pointer = cm.context_manager.get_active_project_path()
                if pointer:
                    try:
                        import project_memory as pm
                        proj = pm.get_project(pointer)
                    except Exception:
                        proj = None
                    ctx.project_path = pointer
                    ctx.project_name = proj["name"] if proj else ctx.project_name
                    ctx.working_directory = pointer
                    if ctx.project_confidence == "UNKNOWN":
                        ctx.project_confidence = "HIGH" if proj else "MEDIUM"
            else:
                ctx.working_directory = ctx.project_path
        except Exception as e:
            ctx.warnings.append(f"Could not resolve project context: {e}")

        # SCREEN CONTEXT — only when explicitly requested (Section 7/19).
        if include_screen:
            try:
                import screen_tools
                screen = screen_tools.analyze_screen()
                ctx.visible_screen_available = bool(screen.available)
                if screen.available:
                    summary_parts = []
                    if screen.detected_errors:
                        summary_parts.append(f"{len(screen.detected_errors)} possible error line(s) visible")
                        self.record_error(
                            session_id, screen.detected_errors[0], source="screen",
                            file=screen.file_references[0] if screen.file_references else None,
                            line=screen.line_references[0] if screen.line_references else None,
                        )
                    if screen.file_references and not ctx.current_file:
                        ctx.current_file = screen.file_references[0]
                        ctx.current_file_confidence = "MEDIUM"
                    if summary_parts:
                        ctx.screen_summary = "; ".join(summary_parts)
                    elif screen.extracted_text:
                        ctx.screen_summary = screen.extracted_text.strip()[:200]
                else:
                    ctx.warnings.append(screen.reason or "Screen context unavailable.")
            except Exception as e:
                ctx.warnings.append(f"Could not analyze screen: {e}")

        # RECENT ACTIVITY (Section 8/9)
        ctx.recent_error = self._get_recent_error(session_id)
        ctx.recent_action = self._get_recent_action(session_id)

        # CONVERSATION CONTEXT + MODE (Section 10/17) — reuses
        # core/session_context.py entirely, no duplication.
        try:
            import core.session_context as session_context
            ctx.conversation_context = session_context.context_to_prompt_block(session_id)
            state = session_context.session_context_store.get_session_state(session_id)
            ctx.current_mode = state.mode
        except Exception as e:
            ctx.warnings.append(f"Could not load conversation/mode context: {e}")

        ctx.active_attachment = active_attachment
        return ctx

    # ── contextual reference resolution (Section 11/12/13) ──────────
    def wants_screen_context(self, message: str) -> bool:
        lower = (message or "").lower()
        return bool(re.search(
            r"\bcheck this\b|\blook at this\b|\bwhat'?s wrong (here|with this)\b|"
            r"\bwhy is this failing\b|\bwhat am i looking at\b|\bthis screen\b|"
            r"\bwhat do you see\b|\bwhat'?s on (my|the) screen\b",
            lower,
        ))

    def resolve(self, message: str, current_context: CurrentContext) -> ContextReference:
        """
        Section 11/13 priority: explicit message content already wins
        by construction (we only get called when the text resolver
        found nothing usable — see main.py), then active
        application/window, then project, then file, then screen, then
        recent action/error. Only returns a resolution when there is
        genuinely nothing ambiguous about it (Section 12) — otherwise
        was_referential=True with confidence 0 and a note, so the
        caller can ask rather than guess.
        """
        if not message or not _COMPUTER_CONTEXT_REFERENCE_RE.search(message):
            return ContextReference(was_referential=False)

        lower = message.lower()
        wants_screen = self.wants_screen_context(message)

        # "fix it" / "run it" / "the last result" → most recent action or error
        if re.search(r"\brun it\b|\bthe last result\b", lower) and current_context.recent_action:
            return ContextReference(
                was_referential=True, resolved_entity=current_context.recent_action["description"],
                resolved_kind="action", confidence=0.7, wants_screen=wants_screen,
                notes=[f"Resolved to the most recent JARVIS action: {current_context.recent_action['description']!r}"],
            )

        if re.search(r"\bfix it\b|\bcheck it\b|\bthis error\b|\bthe previous error\b|\bwhy did it fail\b", lower) and current_context.recent_error:
            return ContextReference(
                was_referential=True, resolved_entity=current_context.recent_error["text"],
                resolved_kind="error", confidence=0.8, wants_screen=wants_screen,
                notes=[f"Resolved to the most recent recorded error: {current_context.recent_error['text'][:80]!r}"],
            )

        if re.search(r"\bthis file\b|\bthis code\b|\bthat code\b|\bopen this\b", lower):
            if current_context.current_file and current_context.current_file_confidence in ("HIGH", "MEDIUM"):
                return ContextReference(
                    was_referential=True, resolved_entity=current_context.current_file,
                    resolved_kind="file", confidence=0.9 if current_context.current_file_confidence == "HIGH" else 0.6,
                    wants_screen=wants_screen,
                    notes=[f"Resolved to the current file: {current_context.current_file!r}"],
                )
            return ContextReference(was_referential=True, resolved_kind="file", confidence=0.0, wants_screen=wants_screen,
                                     notes=["'this file' referenced but no current file could be reliably determined."])

        if re.search(r"\bthis project\b", lower):
            if current_context.ambiguous_projects:
                return ContextReference(
                    was_referential=True, resolved_kind="project", confidence=0.0, wants_screen=wants_screen,
                    notes=["Multiple projects matched — ambiguous, needs clarification."],
                )
            if current_context.project_name and current_context.project_confidence in ("HIGH", "MEDIUM"):
                return ContextReference(
                    was_referential=True, resolved_entity=current_context.project_name, resolved_kind="project",
                    confidence=0.9 if current_context.project_confidence == "HIGH" else 0.6, wants_screen=wants_screen,
                    notes=[f"Resolved to the current project: {current_context.project_name!r}"],
                )
            return ContextReference(was_referential=True, resolved_kind="project", confidence=0.0, wants_screen=wants_screen,
                                     notes=["'this project' referenced but no current project could be reliably determined."])

        # Bare "this"/"here"/"check this" with no more specific pattern
        # matched above — screen-relevant requests get routed to the
        # screen pipeline by main.py regardless of this method's
        # confidence (see wants_screen), so a low-confidence result
        # here is still useful signal, not a dead end.
        if wants_screen:
            return ContextReference(
                was_referential=True, resolved_kind="screen", confidence=0.5 if current_context.active_window else 0.0,
                wants_screen=True,
                notes=["Generic screen-directed reference ('check this'/'look at this') — screen capture requested."],
            )

        # Every branch above either returned or fell through because a
        # matched phrase's specific evidence (file/project/error/
        # action) wasn't available — genuinely nothing reliable to act
        # on, not a signal to interrupt with a clarification question
        # (that's reserved for the specific-phrase branches above,
        # where the user clearly meant *something* concrete).
        return ContextReference(was_referential=False, notes=["Contextual phrase matched but no corresponding evidence was available."])


context_engine = ContextEngine()
