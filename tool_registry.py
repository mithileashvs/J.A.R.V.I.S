"""
JARVIS tool registry.

Wraps the existing tools in tools.py with permission metadata (name,
description, permission level, confirmation requirement) without
touching their implementations. This is deliberately additive:
tools.py's @function_tool()-decorated functions are still exactly what
gets handed to the LiveKit AgentSession in agent.py — that voice
pipeline is untouched.

This registry exists for the non-voice paths (the future intent
router, any REST/tool-calling surface added later) that need to know,
before running a tool, whether it's SAFE / CONFIRM / BLOCKED, and to
actually enforce that via permission_manager.

Every tool referenced in your Section 7 tool list either exists here
already (mapped to its real tools.py implementation) or is registered
as NOT_IMPLEMENTED so the registry is honest about what's real right
now vs. planned for a later phase, instead of silently pretending
missing tools exist.
"""

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from permissions import PermissionLevel, permission_manager

logger = logging.getLogger("jarvis-tools")

BroadcastFn = Callable[[dict], Awaitable[None]]


@dataclass
class ToolSpec:
    name: str
    description: str
    permission: PermissionLevel
    # The actual callable. For tools.py's LiveKit-wrapped functions
    # this is the underlying async function; call via .fn if the
    # @function_tool() wrapper doesn't expose a plain call, otherwise
    # call directly — see run_tool() below for the actual dispatch.
    handler: Optional[Callable[..., Any]] = None
    # Human-readable reason shown in the confirmation prompt.
    confirm_reason: str = ""
    # tools.py's functions all take a leading `context: RunContext`
    # positional arg (LiveKit's calling convention) that none of them
    # actually read internally. Newer tools registered directly against
    # project_memory.py/project_detector.py/awareness.py don't have
    # that arg at all. This flag tells run_tool() which calling
    # convention to use instead of guessing from the callable's shape.
    takes_context: bool = False
    implemented: bool = True
    # Wall-clock ceiling for this tool's execution, enforced by
    # run_tool() via asyncio.wait_for(). None means no limit — fine for
    # fast in-process calls (project memory lookups), but Section 7/12
    # of the Phase 3 spec requires this for anything that can hang:
    # terminal commands, multi-step debug investigation steps.
    timeout_seconds: Optional[float] = None
    # For tools whose real risk depends on the arguments passed at
    # call time — run_terminal_command is "SAFE" for `git status` but
    # "DANGEROUS" for `rm -rf` — a single static `permission` above
    # can't capture that. When set, run_tool() calls this with the
    # tool's args BEFORE the static permission check; it must return
    # (PermissionLevel, reason_string), and that verdict governs for
    # that one call instead of `permission` — see run_tool() for
    # exactly how the two combine (the classifier can only make things
    # MORE restrictive than `permission`, never less).
    dynamic_classifier: Optional[Callable[[dict], tuple[PermissionLevel, str]]] = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            logger.warning(f"[tools] Overwriting existing registration for '{spec.name}'")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    async def run_tool(
        self,
        name: str,
        args: dict,
        broadcast: Optional[BroadcastFn] = None,
        auto_approved: bool = False,
    ) -> dict:
        """
        Central entry point for running a tool outside the voice
        pipeline (agent.py calls tools.py's functions directly via
        LiveKit's own function-calling — this path is for everything
        else: REST endpoints, the intent router, future terminal/git
        tools).

        Returns a dict describing what happened rather than raising,
        so callers (e.g. a REST handler) can always produce a clean
        response instead of a 500.
        """
        spec = self.get(name)
        if spec is None:
            return {"status": "error", "message": f"Unknown tool '{name}'"}

        if not spec.implemented:
            return {"status": "error", "message": f"'{name}' is registered but not yet implemented."}

        # Effective permission for THIS call: the dynamic classifier
        # (if the tool has one) decides based on the actual args, e.g.
        # run_terminal_command classifying `git status` as SAFE but
        # `rm -rf /` as BLOCKED. spec.permission is only consulted
        # when there's no classifier, or as a floor the classifier
        # can't relax below — a classifier can escalate restriction
        # but never grant more freedom than the tool's own static
        # permission allows, so a tool registered as CONFIRM can't
        # have its classifier wave something through as SAFE.
        effective_permission = spec.permission
        classifier_reason = None
        if spec.dynamic_classifier is not None:
            try:
                classified_level, classifier_reason = spec.dynamic_classifier(args)
            except Exception as e:
                logger.error(f"[tools] '{name}' dynamic_classifier raised: {e}")
                return {"status": "error", "message": f"Could not classify arguments for '{name}': {e}"}

            _RESTRICTIVENESS = {PermissionLevel.SAFE: 0, PermissionLevel.CONFIRM: 1, PermissionLevel.BLOCKED: 2}
            if _RESTRICTIVENESS[classified_level] < _RESTRICTIVENESS[spec.permission]:
                # Classifier tried to be MORE permissive than the
                # tool's registered ceiling — not allowed; fall back
                # to the tool's own static permission instead.
                effective_permission = spec.permission
            else:
                effective_permission = classified_level

        if effective_permission == PermissionLevel.BLOCKED:
            logger.warning(f"[tools] Refused to run BLOCKED '{name}': {classifier_reason or 'blocked tool'}")
            return {
                "status": "blocked",
                "message": classifier_reason or f"'{name}' is a blocked tool and cannot be executed under any circumstance.",
            }

        if effective_permission == PermissionLevel.CONFIRM and not auto_approved:
            confirmation = await permission_manager.request_confirmation(
                tool_name=name,
                args=args,
                reason=classifier_reason or spec.confirm_reason or f"JARVIS wants to run '{name}'.",
                broadcast=broadcast,
            )
            return {
                "status": "pending_confirmation",
                "confirmation_id": confirmation.id,
                "message": confirmation.reason,
            }

        if spec.handler is None:
            return {"status": "error", "message": f"'{name}' has no handler wired up."}

        try:
            if spec.takes_context:
                # tools.py's functions all take a leading `context:
                # RunContext` positional arg (LiveKit's calling
                # convention) that none of them actually read
                # internally — confirmed by inspecting tools.py
                # directly. None is a safe stand-in when calling them
                # from this non-voice path.
                call_args = (None,)
            else:
                call_args = ()

            if spec.timeout_seconds is not None:
                # asyncio.wait_for only cancels *coroutines* — a sync
                # function already running on the event loop thread
                # can't be interrupted mid-call by wait_for alone (it
                # would stop waiting, not stop the call, and the loop
                # stays blocked until the sync call itself returns).
                # Running sync handlers in a thread pool via
                # to_thread makes them genuinely cancellable/timeout-
                # able, which matters once real blocking calls
                # (subprocess.run for terminal commands) land here.
                if inspect.iscoroutinefunction(spec.handler):
                    call_result = spec.handler(*call_args, **args)
                    result = await asyncio.wait_for(call_result, timeout=spec.timeout_seconds)
                else:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(spec.handler, *call_args, **args),
                        timeout=spec.timeout_seconds,
                    )
            else:
                call_result = spec.handler(*call_args, **args)
                # Handle both async (tools.py's coroutines) and sync
                # (project_memory.py/project_detector.py/awareness.py
                # are plain functions) handlers uniformly, rather than
                # assuming every registered tool is async.
                if inspect.isawaitable(call_result):
                    result = await call_result
                else:
                    result = call_result

            return {"status": "ok", "result": result}
        except asyncio.TimeoutError:
            logger.warning(f"[tools] '{name}' timed out after {spec.timeout_seconds}s")
            return {
                "status": "error",
                "message": f"'{name}' timed out after {spec.timeout_seconds} seconds.",
            }
        except Exception as e:
            logger.error(f"[tools] '{name}' raised: {e}")
            return {"status": "error", "message": str(e)}


# Capture + OCR is the slowest SAFE-adjacent operation in the registry
# (screenshot I/O + a full Tesseract pass) — generous relative to
# analyze_code's 15s, but still bounded so a stuck OCR call can't hang
# a chat turn indefinitely.
_ANALYSIS_TOOL_TIMEOUT = 25.0

tool_registry = ToolRegistry()


def _build_default_registry() -> None:
    """
    Register the real tools from tools.py plus the SAFE inspection
    tools this project already effectively has via existing code paths
    (project/file reads), and stub out the not-yet-built tools from
    Section 7 so the registry can report accurately on what's missing.

    tools.py's functions are decorated with @function_tool() (LiveKit's
    decorator). That wrapper exposes the original coroutine, so we call
    through to it directly rather than duplicating logic here.
    """
    import tools as t

    def _unwrap(fn):
        # LiveKit's function_tool() wraps the coroutine but keeps the
        # original callable reachable; fall back to the wrapped object
        # itself if there's nothing to unwrap, so this stays correct
        # even if the wrapper's internals change.
        return getattr(fn, "__wrapped__", fn)

    tool_registry.register(ToolSpec(
        name="get_weather",
        description="Get current weather for a city.",
        permission=PermissionLevel.SAFE,
        handler=_unwrap(t.get_weather),
        takes_context=True,
    ))
    tool_registry.register(ToolSpec(
        name="search_web",
        description="Search the web via DuckDuckGo.",
        permission=PermissionLevel.SAFE,
        handler=_unwrap(t.search_web),
        takes_context=True,
    ))
    tool_registry.register(ToolSpec(
        name="open_website",
        description="Open a website in the default browser.",
        permission=PermissionLevel.SAFE,
        handler=_unwrap(t.open_website),
        takes_context=True,
    ))
    tool_registry.register(ToolSpec(
        name="open_application",
        description="Launch a desktop application.",
        permission=PermissionLevel.CONFIRM,
        handler=_unwrap(t.open_application),
        confirm_reason="JARVIS wants to launch a desktop application.",
        takes_context=True,
    ))
    tool_registry.register(ToolSpec(
        name="send_email",
        description="Send an email via the configured Gmail account.",
        permission=PermissionLevel.CONFIRM,
        handler=_unwrap(t.send_email),
        confirm_reason="JARVIS wants to send an email on your behalf.",
        takes_context=True,
    ))

    # ── Phase 2: project detection/memory + awareness (all SAFE — ────
    # read-only, no writes outside the project directory, no
    # continuous/background execution; see each module's docstring). ──
    import project_detector as _pd
    import project_memory as _pm
    import awareness as _aw

    tool_registry.register(ToolSpec(
        name="inspect_project",
        description="Scan a project directory and summarize its stack, structure, and important files.",
        permission=PermissionLevel.SAFE,
        handler=_pd.detect_and_save,
    ))
    tool_registry.register(ToolSpec(
        name="project_memory_search",
        description="Search stored facts (decisions, TODOs, known issues, etc.) for a project.",
        permission=PermissionLevel.SAFE,
        handler=_pm.search_facts,
    ))
    tool_registry.register(ToolSpec(
        name="get_active_window",
        description="Get the title of the current foreground window (Windows only).",
        permission=PermissionLevel.SAFE,
        handler=_aw.get_active_window,
    ))
    tool_registry.register(ToolSpec(
        name="system_monitor",
        description="Get current CPU/memory/disk load.",
        permission=PermissionLevel.SAFE,
        handler=_aw.get_system_load,
    ))
    tool_registry.register(ToolSpec(
        name="list_processes",
        description="List running processes sorted by memory usage.",
        permission=PermissionLevel.SAFE,
        handler=_aw.list_running_processes,
    ))
    tool_registry.register(ToolSpec(
        name="save_project_memory",
        description="Persist a new project-memory fact (decision, TODO, known issue, etc.).",
        permission=PermissionLevel.CONFIRM,
        handler=_pm.save_fact,
        confirm_reason="JARVIS wants to save a new fact to project memory.",
    ))

    # ── Phase 3: context manager ─────────────────────────────────
    # SAFE + read-only: gather_context never writes anything, only
    # aggregates conversation/window/project data that's already
    # readable through other SAFE tools above. set_active_project is
    # CONFIRM-free too — it only changes an in-memory pointer inside
    # this backend process, not project state itself.
    from context_manager import context_manager as _ctx

    def _gather_context(session_id: str = None, fact_query: str = None) -> dict:
        result = _ctx.gather(session_id=session_id, fact_query=fact_query)
        return {
            "prompt_block": result.to_prompt_block(),
            "active_project_path": result.active_project_path,
            "active_project": result.active_project,
            "warnings": result.warnings,
        }

    def _set_active_project(path: str) -> dict:
        _ctx.set_active_project(path)
        return {"active_project_path": path}

    tool_registry.register(ToolSpec(
        name="gather_context",
        description="Aggregate conversation history, active window, and project memory into one context bundle.",
        permission=PermissionLevel.SAFE,
        handler=_gather_context,
        timeout_seconds=10.0,
    ))
    tool_registry.register(ToolSpec(
        name="set_active_project",
        description="Mark a project path as the one currently being worked on.",
        permission=PermissionLevel.SAFE,
        handler=_set_active_project,
    ))

    # ── Phase 3: code analysis + explanation modes ──────────────
    # SAFE + read-only: analyze_file only reads the target file (bounded
    # by _MAX_FILE_BYTES) and runs pyflakes in-process — no writes, no
    # network, no shell. explain_code likewise only extracts and frames
    # code text; it does not call an LLM itself (see code_analysis.py's
    # docstring), so it's just as safe to run automatically.
    from code_analysis import analyze_file as _analyze_file, extract_unit as _extract_unit, build_explanation_prompt as _build_explanation_prompt

    def _analyze_code(file_path: str) -> dict:
        result = _analyze_file(file_path)
        return {
            "file_path": result.file_path,
            "language": result.language,
            "line_count": result.line_count,
            "analysis_depth": result.analysis_depth,
            "summary": result.summary,
            "issues": [
                {"severity": i.severity, "confidence": i.confidence, "message": i.message, "line": i.line}
                for i in result.issues
            ],
            "text": result.to_text(),
        }

    def _explain_code(file_path: str, mode: str = "TECHNICAL", unit_name: str = None) -> dict:
        unit = _extract_unit(file_path, unit_name)
        prompt = _build_explanation_prompt(unit, mode)
        return {
            "unit_kind": unit.kind,
            "unit_name": unit.name,
            "start_line": unit.start_line,
            "end_line": unit.end_line,
            "prompt": prompt,   # caller feeds this to the LLM — see code_analysis.py docstring
        }

    tool_registry.register(ToolSpec(
        name="analyze_code",
        description="Run static analysis on a code file (real detection for Python via pyflakes; structural checks for other languages).",
        permission=PermissionLevel.SAFE,
        handler=_analyze_code,
        timeout_seconds=15.0,
    ))
    tool_registry.register(ToolSpec(
        name="explain_code",
        description="Extract a file/function/class and build an explanation prompt in a given mode (BEGINNER/LINE_BY_LINE/TECHNICAL/INTERVIEW/EXAM/ELI5).",
        permission=PermissionLevel.SAFE,
        handler=_explain_code,
        timeout_seconds=10.0,
    ))

    # ── Phase 3: terminal tool ───────────────────────────────────
    # run_terminal_command's real risk depends entirely on the command
    # string ("git status" vs "rm -rf /"), not on which tool was
    # called — so its permission is decided per-call by
    # _classify_terminal_command (a dynamic_classifier — see
    # ToolSpec), not by a single static level. Registered as SAFE
    # here only as the ceiling the classifier is allowed to grant —
    # actual enforcement happens entirely inside the classifier, which
    # defaults any unrecognized command to CONFIRM (never SAFE) and
    # hard-blocks command-chaining/injection attempts regardless of
    # what program name they start with. See terminal_tools.py's
    # injection-attempt test coverage for why that boundary is the one
    # actually being relied on, not this registration line.
    import terminal_tools as _tt

    def _classify_terminal_command(args: dict) -> tuple:
        command = args.get("command", "")
        result = _tt.classify_command(command)
        level_map = {
            "SAFE": PermissionLevel.SAFE,
            "CONFIRM": PermissionLevel.CONFIRM,
            "DANGEROUS": PermissionLevel.CONFIRM,  # still confirmable, just with a stronger prompt reason
            "REJECTED": PermissionLevel.BLOCKED,   # never runs — see terminal_tools.py's injection tests
        }
        prefix = "⚠ DANGEROUS: " if result.level == "DANGEROUS" else ""
        return level_map[result.level], f"{prefix}{result.reason}"

    async def _run_terminal_command(command: str, cwd: str = None, timeout: float = 30.0) -> dict:
        result = await _tt.run_command(command, cwd=cwd, timeout=timeout)
        response = {
            "command": result.command,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "execution_time": result.execution_time,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        }
        # Surface extracted error structure whenever the command
        # produced stderr or a nonzero exit — Section 8's requirement,
        # done here so every caller gets it for free instead of having
        # to remember to call extract_errors() themselves.
        if result.stderr or (result.exit_code not in (0, None)):
            combined = result.stdout + "\n" + result.stderr
            extracted = _tt.extract_errors(combined)
            response["extracted_error"] = {
                "primary_error": extracted.primary_error,
                "likely_root_cause": extracted.likely_root_cause,
                "error_type": extracted.error_type,
                "relevant_files": extracted.relevant_files,
            }
        return response

    tool_registry.register(ToolSpec(
        name="run_terminal_command",
        description="Run a single shell command (no chaining/piping). Permission is decided per-command: read-only commands run immediately, state-changing commands need confirmation, destructive commands are blocked or require explicit confirmation.",
        permission=PermissionLevel.SAFE,
        handler=_run_terminal_command,
        dynamic_classifier=_classify_terminal_command,
        confirm_reason="JARVIS wants to run a terminal command.",
        timeout_seconds=35.0,
    ))

    # read_terminal_output: SAFE, read-only — surfaces the cached
    # result of the last run_terminal_command call (see
    # terminal_tools.get_last_result's docstring for why a cache is
    # the honest way to do this without continuously capturing the
    # screen, per Section 15's privacy requirement).
    def _read_terminal_output(max_age_seconds: float = 600.0) -> dict:
        result = _tt.get_last_result(max_age_seconds=max_age_seconds)
        if result is None:
            return {
                "available": False,
                "reason": "No recent terminal command output available. Run a command via "
                          "run_terminal_command first, or it's older than the requested window.",
            }
        response = {
            "available": True,
            "command": result.command,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
        }
        if result.stderr or (result.exit_code not in (0, None)):
            extracted = _tt.extract_errors(result.stdout + "\n" + result.stderr)
            response["extracted_error"] = {
                "primary_error": extracted.primary_error,
                "likely_root_cause": extracted.likely_root_cause,
                "error_type": extracted.error_type,
                "relevant_files": extracted.relevant_files,
            }
        return response

    tool_registry.register(ToolSpec(
        name="read_terminal_output",
        description="Read the output of the most recently run terminal command, with errors extracted if any occurred.",
        permission=PermissionLevel.SAFE,
        handler=_read_terminal_output,
    ))

    # inspect_environment: SAFE — only ever runs a fixed, hardcoded set
    # of read-only version/package-listing commands (see
    # terminal_tools._ENVIRONMENT_COMMANDS), never user-supplied
    # strings, so this is safe to auto-run despite going through the
    # same subprocess path as run_terminal_command.
    async def _inspect_environment(target: str, cwd: str = None) -> dict:
        return await _tt.inspect_environment(target, cwd=cwd)

    tool_registry.register(ToolSpec(
        name="inspect_environment",
        description="Inspect the active Python or Node environment (version + installed packages) via fixed, read-only commands.",
        permission=PermissionLevel.SAFE,
        handler=_inspect_environment,
        timeout_seconds=20.0,
    ))

    # run_tests: always CONFIRM (Section 7 explicitly lists `pytest`/
    # `npm run ...` as CONFIRM-tier) — this is a convenience wrapper
    # that picks a sensible default test command from the active
    # project's detected stack rather than the caller having to know
    # which runner to invoke, but it still goes through the exact same
    # confirmation gate run_terminal_command does.
    async def _run_tests(project_path: str = None, extra_args: str = "") -> dict:
        path = project_path or _ctx.get_active_project_path()
        if not path:
            return {"status": "error", "message": "No project path given and no active project set."}

        import project_memory as _pm
        project = _pm.get_project(path)
        technologies = project.get("technologies", []) if project else []

        if "Python" in technologies:
            command = f"pytest {extra_args}".strip()
        elif "Node.js" in technologies:
            command = f"npm test {extra_args}".strip()
        else:
            return {
                "status": "error",
                "message": f"Could not determine a test runner for '{path}' (detected stack: {technologies or 'unknown'}). "
                            f"Run inspect_project first, or specify the command directly via run_terminal_command.",
            }

        result = await _tt.run_command(command, cwd=path, timeout=120.0)
        response = {
            "command": result.command, "exit_code": result.exit_code,
            "stdout": result.stdout, "stderr": result.stderr,
            "execution_time": result.execution_time, "timed_out": result.timed_out,
        }
        if result.exit_code not in (0, None):
            extracted = _tt.extract_errors(result.stdout + "\n" + result.stderr)
            response["extracted_error"] = {
                "primary_error": extracted.primary_error,
                "likely_root_cause": extracted.likely_root_cause,
                "error_type": extracted.error_type,
                "relevant_files": extracted.relevant_files,
            }
        return response

    tool_registry.register(ToolSpec(
        name="run_tests",
        description="Run the project's test suite (pytest for Python projects, npm test for Node projects), detected from project memory.",
        permission=PermissionLevel.CONFIRM,
        handler=_run_tests,
        confirm_reason="JARVIS wants to run the project's test suite.",
        timeout_seconds=125.0,
    ))

    # check_port_usage: SAFE, read-only — psutil connection-table
    # snapshot, nothing opened/closed/killed.
    from awareness import check_port_usage as _check_port_usage
    tool_registry.register(ToolSpec(
        name="check_port_usage",
        description="Check whether a given port currently has a process listening on it.",
        permission=PermissionLevel.SAFE,
        handler=_check_port_usage,
    ))

    # read_relevant_file: SAFE, read-only — bounded by
    # code_analysis._MAX_FILE_BYTES same as analyze_code; supports an
    # optional line range so a targeted read doesn't have to pull an
    # entire large file just to look at the lines around a traceback.
    from code_analysis import read_relevant_file as _read_relevant_file
    tool_registry.register(ToolSpec(
        name="read_relevant_file",
        description="Read a file (optionally a specific line range) for context — bounded by size, does not scan the whole project.",
        permission=PermissionLevel.SAFE,
        handler=_read_relevant_file,
        timeout_seconds=10.0,
    ))

    # find_code_reference: SAFE, read-only — bounded plain-text search
    # across the project (see project_detector.find_references's
    # docstring for the file/match caps that keep this from becoming
    # an unbounded scan).
    from project_detector import find_references as _find_references
    tool_registry.register(ToolSpec(
        name="find_code_reference",
        description="Search a project's source files for a literal symbol/string, returning matching file+line locations (bounded, not a full-project dump).",
        permission=PermissionLevel.SAFE,
        handler=_find_references,
        timeout_seconds=15.0,
    ))

    # apply_fix: CONFIRM, always — Section 14's "APPLY FIX" step. Never
    # SAFE under any circumstance: this is the one Phase 3 tool that
    # writes to a file, so it goes through the exact same
    # confirmation gate as send_email/open_application, with the
    # proposed diff shown as the confirmation reason so the user sees
    # precisely what will change before approving.
    from file_ops import apply_fix as _apply_fix_impl, build_diff_preview as _build_diff_preview

    def _classify_apply_fix(args: dict) -> tuple:
        file_path = args.get("file_path", "")
        new_content = args.get("new_content", "")
        try:
            diff = _build_diff_preview(file_path, new_content)
        except (FileNotFoundError, ValueError) as e:
            # Can't even preview it — still CONFIRM (never silently
            # SAFE), the human sees the raw error as the reason.
            return PermissionLevel.CONFIRM, f"Could not preview changes to '{file_path}': {e}"
        if not diff.strip():
            return PermissionLevel.CONFIRM, f"No effective changes to '{file_path}' (proposed content is identical)."
        preview = diff if len(diff) <= 2000 else diff[:2000] + "\n... [diff truncated for the confirmation prompt]"
        return PermissionLevel.CONFIRM, f"JARVIS wants to modify '{file_path}':\n{preview}"

    def _apply_fix(file_path: str, new_content: str, description: str = "") -> dict:
        result = _apply_fix_impl(file_path, new_content, description=description)
        return {
            "file_path": result.file_path,
            "backup_path": result.backup_path,
            "bytes_written": result.bytes_written,
            "verification": result.verification,
        }

    tool_registry.register(ToolSpec(
        name="apply_fix",
        description="Apply a proposed fix to an existing file (backs up the original first, verifies the result). Always requires explicit confirmation.",
        permission=PermissionLevel.CONFIRM,
        handler=_apply_fix,
        dynamic_classifier=_classify_apply_fix,
        confirm_reason="JARVIS wants to modify a file.",
        timeout_seconds=15.0,
    ))

    # ── Phase 3: debug mode ──────────────────────────────────────
    # SAFE: the investigation itself only reads (context, files,
    # project memory) — it never runs a terminal command or edits a
    # file directly (Section 14: fixes are proposed, never applied,
    # by this module). If a step along the way needs to run something
    # CONFIRM/DANGEROUS, that goes through run_terminal_command's own
    # gate above, not through debug_investigation's permission.
    import debug_mode as _dm

    async def _debug_investigation(user_message: str, session_id: str = None, max_steps: int = None, timeout_seconds: float = None) -> dict:
        kwargs = {}
        if max_steps is not None:
            kwargs["max_steps"] = max_steps
        if timeout_seconds is not None:
            kwargs["timeout_seconds"] = timeout_seconds
        investigation = _dm.Investigation(**kwargs)
        result = await investigation.run(user_message, session_id=session_id)
        return {
            "steps": [
                {"step_number": s.step_number, "name": s.name, "finding": s.finding, "evidence": s.evidence}
                for s in result.steps
            ],
            "diagnosis": result.diagnosis.to_text() if result.diagnosis else None,
            "cancelled": result.cancelled,
            "stopped_reason": result.stopped_reason,
        }

    tool_registry.register(ToolSpec(
        name="debug_investigation",
        description="Run a bounded, multi-step debugging investigation (gathers context, analyzes code, checks project memory, produces a ranked diagnosis).",
        permission=PermissionLevel.SAFE,
        handler=_debug_investigation,
        # Ceiling above Investigation's own DEFAULT_TIMEOUT_SECONDS
        # (60s) so the outer tool-registry timeout isn't racing the
        # investigation's own internal budget — same reasoning as
        # run_terminal_command's 35s vs run_command's 30s default.
        timeout_seconds=70.0,
    ))

    # ── Phase 4: screen-aware error analysis ─────────────────────
    # All three tools are CONFIRM, not SAFE, despite being read-only —
    # unlike get_active_window (a window TITLE, Phase 2, still SAFE),
    # these actually capture pixel content off the user's screen and
    # run OCR over it, which can pick up far more than the error
    # message being asked about (anything else visible in that
    # window). Requiring explicit confirmation before every capture is
    # what "no screen capture without a reason" (Section: SCREEN
    # PRIVACY REQUIREMENTS) means in practice — the reason is shown to
    # the user before the capture happens, not just logged after.
    # Never invoked on a timer/loop/background thread anywhere in this
    # codebase — see screen_tools.py's module docstring.
    import screen_tools as _st

    def _analyze_screen(save_screenshot: bool = False) -> dict:
        result = _st.analyze_screen(save_screenshot=save_screenshot)
        return {
            "available": result.available,
            "reason": result.reason,
            "application_type": result.application_type,
            "window_title": result.window_title,
            "extracted_text": result.extracted_text,
            "truncated": result.truncated,
            "detected_errors": result.detected_errors,
            "file_references": result.file_references,
            "line_references": result.line_references,
            "screenshot_path": result.screenshot_path,
        }

    tool_registry.register(ToolSpec(
        name="analyze_screen",
        description="Capture the active window (targeted region only, not the full desktop), OCR it, "
                     "and extract error messages / file / line references. Nothing is captured until confirmed.",
        permission=PermissionLevel.CONFIRM,
        handler=_analyze_screen,
        confirm_reason="JARVIS wants to capture and read your active window to see what's on screen.",
        timeout_seconds=_ANALYSIS_TOOL_TIMEOUT,
    ))

    def _capture_active_window(save_screenshot: bool = True) -> dict:
        # Same underlying pipeline as analyze_screen, but framed for a
        # plain "take a screenshot" request — defaults to keeping the
        # file (the point of asking for a screenshot is usually to
        # keep it) rather than analyze_screen's discard-by-default.
        result = _st.analyze_screen(save_screenshot=save_screenshot)
        return {
            "available": result.available,
            "reason": result.reason,
            "window_title": result.window_title,
            "screenshot_path": result.screenshot_path,
        }

    tool_registry.register(ToolSpec(
        name="capture_active_window",
        description="Take a screenshot of just the active window and save it locally.",
        permission=PermissionLevel.CONFIRM,
        handler=_capture_active_window,
        confirm_reason="JARVIS wants to take a screenshot of your active window.",
        timeout_seconds=_ANALYSIS_TOOL_TIMEOUT,
    ))

    def _extract_screen_text() -> dict:
        result = _st.analyze_screen(save_screenshot=False)
        if not result.available:
            return {"available": False, "reason": result.reason}
        return {"available": True, "text": result.extracted_text, "truncated": result.truncated}

    tool_registry.register(ToolSpec(
        name="extract_screen_text",
        description="Capture the active window and return only the raw OCR'd text (no error/app classification).",
        permission=PermissionLevel.CONFIRM,
        handler=_extract_screen_text,
        confirm_reason="JARVIS wants to capture and read the text on your active window.",
        timeout_seconds=_ANALYSIS_TOOL_TIMEOUT,
    ))

    # ── Phase 4: background build/error monitoring ──────────────
    # Same reasoning as run_terminal_command: what's actually risky
    # here is the *command* being launched, not the fact that it's
    # launched as a background task — so start_background_task reuses
    # terminal_tools.classify_command via a dynamic_classifier rather
    # than a second, parallel classification scheme. Monitoring itself
    # (draining output, detecting failure) is never a source of risk,
    # so status/stop get their own fixed levels below.
    import background_tasks as _bt

    def _classify_background_command(args: dict) -> tuple:
        command = args.get("command", "")
        result = _tt.classify_command(command)
        level_map = {
            "SAFE": PermissionLevel.SAFE,
            "CONFIRM": PermissionLevel.CONFIRM,
            "DANGEROUS": PermissionLevel.CONFIRM,
            "REJECTED": PermissionLevel.BLOCKED,
        }
        prefix = "⚠ DANGEROUS: " if result.level == "DANGEROUS" else ""
        return level_map[result.level], f"{prefix}{result.reason} (will run in the background and be monitored)"

    async def _start_background_task(name: str, command: str, project: str = None, cwd: str = None) -> dict:
        try:
            task = await _bt.task_manager.start_task(name, command, project=project, cwd=cwd)
        except RuntimeError as e:
            return {"started": False, "reason": str(e)}
        return {"started": True, "task": task.to_dict()}

    tool_registry.register(ToolSpec(
        name="start_background_task",
        description="Launch and monitor a long-running command (dev server, build, test run) in the background. "
                     "JARVIS tracks its status and notifies on failure — does not monitor anything not explicitly started this way.",
        permission=PermissionLevel.SAFE,
        handler=_start_background_task,
        dynamic_classifier=_classify_background_command,
        confirm_reason="JARVIS wants to start and monitor a background process.",
        timeout_seconds=10.0,  # only covers the launch itself — the process keeps running after this returns
    ))

    def _get_background_task_status(task_id: str = None) -> dict:
        if task_id:
            task = _bt.task_manager.get_task(task_id)
            if task is None:
                return {"found": False, "reason": f"No background task with id '{task_id}'."}
            return {"found": True, "task": task.to_dict()}
        return {"found": True, "tasks": [t.to_dict() for t in _bt.task_manager.list_tasks()]}

    tool_registry.register(ToolSpec(
        name="get_background_task_status",
        description="Check the status of a background task by id, or list all tracked background tasks if no id is given.",
        permission=PermissionLevel.SAFE,
        handler=_get_background_task_status,
        timeout_seconds=5.0,
    ))

    # monitor_background_task: an explicit alias for status-checking —
    # monitoring itself starts automatically the moment
    # start_background_task launches the process (Section: "The user
    # or JARVIS workflow must explicitly start/register a monitored
    # task" — that registration IS the monitoring start). This tool
    # exists for callers that think in terms of "check on the build I
    # asked you to watch" rather than "get task status".
    tool_registry.register(ToolSpec(
        name="monitor_background_task",
        description="Check on a background task JARVIS is already monitoring (same as get_background_task_status).",
        permission=PermissionLevel.SAFE,
        handler=_get_background_task_status,
        timeout_seconds=5.0,
    ))

    async def _stop_background_task(task_id: str) -> dict:
        task = await _bt.task_manager.stop_task(task_id)
        return {"task": task.to_dict()}

    tool_registry.register(ToolSpec(
        name="stop_background_task",
        description="Stop a running background task.",
        permission=PermissionLevel.CONFIRM,
        handler=_stop_background_task,
        confirm_reason="JARVIS wants to stop a background task it's monitoring.",
        timeout_seconds=10.0,
    ))

    # ── Phase 4: Git Assistant ────────────────────────────────────
    # All SAFE — git_tools.py only ever runs read-only git subcommands
    # (status/diff/log/branch), the exact set terminal_tools.py's own
    # classifier already treats as SAFE for `git`. Nothing here can
    # stage, commit, push, or merge — see git_tools.py's module
    # docstring for why that's a deliberate scope boundary, not an
    # oversight. Commit-message/change-summary generation only ever
    # return text; git add/commit itself stays behind
    # run_terminal_command's own CONFIRM gate, same as any other
    # state-changing command.
    import git_tools as _gt

    async def _git_status(cwd: str = None) -> dict:
        result = await _gt.git_status(cwd)
        return {
            "available": result.available, "reason": result.reason, "branch": result.branch,
            "entries": [{"path": e.path, "status": e.status, "staged": e.staged} for e in result.entries],
            "text": result.to_text(),
        }

    tool_registry.register(ToolSpec(
        name="git_status",
        description="Show the current branch and modified/added/deleted/untracked files.",
        permission=PermissionLevel.SAFE,
        handler=_git_status,
        timeout_seconds=10.0,
    ))

    async def _git_diff(cwd: str = None, staged: bool = False, file: str = None) -> dict:
        result = await _gt.git_diff(cwd, staged=staged, file=file)
        return {
            "available": result.available, "reason": result.reason,
            "stat_summary": result.stat_summary, "diff_text": result.diff_text,
            "truncated": result.truncated,
        }

    tool_registry.register(ToolSpec(
        name="git_diff",
        description="Show the diff (optionally staged-only, optionally for one file).",
        permission=PermissionLevel.SAFE,
        handler=_git_diff,
        timeout_seconds=15.0,
    ))

    tool_registry.register(ToolSpec(
        name="git_log",
        description="Show recent commits (hash, author, when, message).",
        permission=PermissionLevel.SAFE,
        handler=_gt.git_log,
        timeout_seconds=10.0,
    ))

    tool_registry.register(ToolSpec(
        name="git_branch",
        description="List branches and show which one is currently checked out.",
        permission=PermissionLevel.SAFE,
        handler=_gt.git_branch,
        timeout_seconds=10.0,
    ))

    tool_registry.register(ToolSpec(
        name="generate_commit_summary",
        description="Summarize working-tree changes grouped by file, with any dependency-pinning concerns flagged.",
        permission=PermissionLevel.SAFE,
        handler=_gt.generate_change_summary,
        timeout_seconds=15.0,
    ))

    tool_registry.register(ToolSpec(
        name="generate_commit_message",
        description="Propose a conventional-commit-style message from staged (or unstaged, if nothing is staged) changes. Never runs git commit.",
        permission=PermissionLevel.SAFE,
        handler=_gt.generate_commit_message,
        timeout_seconds=15.0,
    ))

    async def _analyze_merge_conflict(file_path: str) -> dict:
        result = await _gt.analyze_merge_conflict(file_path)
        return {
            "available": result.available, "reason": result.reason, "file": result.file,
            "conflict_count": len(result.blocks), "text": result.to_text(),
        }

    tool_registry.register(ToolSpec(
        name="analyze_merge_conflict",
        description="Explain both sides of a merge conflict in a file (read-only — never resolves it automatically).",
        permission=PermissionLevel.SAFE,
        handler=_analyze_merge_conflict,
        timeout_seconds=10.0,
    ))

    # ── System Security + Storage Cleaner ────────────────────────
    # security/storage status + analysis are SAFE (read-only, bounded —
    # see system_health.py's own time budgets). A quick Defender scan
    # is SAFE too (Section 2: lightweight, bounded, doesn't hammer
    # CPU/GPU); a full scan is CONFIRM (Section 3: "may take
    # significantly longer" — worth asking first) via the dynamic
    # classifier below, which reads the scan_type arg the same way
    # run_terminal_command's classifier reads the command arg.
    # clean_junk is ALWAYS CONFIRM, no matter what — Section 13's "all
    # destructive operations require explicit confirmation" is
    # absolute, so this is the one tool in this group with no
    # classifier that could ever relax it to SAFE.
    import system_health as _sh

    def _classify_security_scan(args: dict) -> tuple[PermissionLevel, str]:
        scan_type = (args.get("scan_type") or "quick").strip().lower()
        if scan_type == "full":
            return PermissionLevel.CONFIRM, (
                "JARVIS wants to run a full system security scan — this can take a significant "
                "amount of time and will use noticeable CPU."
            )
        return PermissionLevel.SAFE, ""

    tool_registry.register(ToolSpec(
        name="system_security_status",
        description="Get real Windows Security (Microsoft Defender) protection status.",
        permission=PermissionLevel.SAFE,
        handler=_sh.get_security_status,
    ))
    tool_registry.register(ToolSpec(
        name="system_threat_detections",
        description="Get real recent threat detections reported by Windows Security.",
        permission=PermissionLevel.SAFE,
        handler=_sh.get_threat_detections,
    ))
    tool_registry.register(ToolSpec(
        name="system_security_scan_status",
        description="Check whether a Windows Security scan is currently running.",
        permission=PermissionLevel.SAFE,
        handler=_sh.is_scan_running,
    ))
    tool_registry.register(ToolSpec(
        name="system_security_scan",
        description="Start a real Windows Security scan (quick or full).",
        permission=PermissionLevel.SAFE,  # ceiling; classifier escalates 'full' to CONFIRM
        dynamic_classifier=_classify_security_scan,
        handler=_sh.start_scan,
        confirm_reason="JARVIS wants to run a full system security scan.",
        timeout_seconds=15.0,  # start_scan() itself only launches the scan and returns
    ))
    tool_registry.register(ToolSpec(
        name="system_storage_summary",
        description="Get real total/used/free storage for the system drive.",
        permission=PermissionLevel.SAFE,
        handler=_sh.get_storage_summary,
    ))
    tool_registry.register(ToolSpec(
        name="system_storage_analyze",
        description="Scan allowlisted junk-file locations (temp, cache, Recycle Bin, crash dumps, etc.) and report real reclaimable space.",
        permission=PermissionLevel.SAFE,
        handler=_sh.analyze_storage,
        timeout_seconds=45.0,
    ))
    tool_registry.register(ToolSpec(
        name="system_find_large_files",
        description="Find real large files under a directory (defaults to the user's home folder).",
        permission=PermissionLevel.SAFE,
        handler=_sh.find_large_files,
        timeout_seconds=15.0,
    ))
    def _classify_clean_junk(args: dict) -> tuple[PermissionLevel, str]:
        # dry_run (Section 8's default "scan-only" mode) is read-only —
        # SAFE. Only an explicit dry_run=False (Section 6/7's actual
        # deletion) needs confirmation; this mirrors the quick/full
        # scan split above; the two branches are otherwise the same
        # underlying clean_junk() call.
        if args.get("dry_run", True):
            return PermissionLevel.SAFE, ""
        categories = ", ".join(args.get("category_keys") or [])
        return PermissionLevel.CONFIRM, (
            f"JARVIS wants to permanently remove junk files in: {categories or '(no categories specified)'}."
        )

    tool_registry.register(ToolSpec(
        name="system_clean_junk",
        description="Delete files from SAFE_TO_CLEAN junk categories identified by system_storage_analyze. Always requires confirmation unless dry_run.",
        permission=PermissionLevel.SAFE,  # ceiling; classifier escalates dry_run=False to CONFIRM
        dynamic_classifier=_classify_clean_junk,
        handler=_sh.clean_junk,
        confirm_reason="JARVIS wants to permanently remove the junk files it found.",
        timeout_seconds=60.0,
    ))

    # ── Planned tools — not yet implemented ──────────────────────
    # Registered so /tools (or any future listing endpoint) reports
    # them honestly as "not implemented" instead of the registry
    # simply not knowing they were ever planned.
    #
    # Everything else originally listed here is now real and
    # registered above: read_relevant_file, find_code_reference,
    # read_terminal_output, inspect_environment, run_tests, apply_fix.
    # git_status/git_diff/git_log are deliberately NOT re-added as
    # separate tools — they're already reachable, SAFE, through
    # run_terminal_command's own classifier (see
    # terminal_tools._SAFE_COMMANDS's git entry), so a dedicated tool
    # would just duplicate that path.
    #
    # list_directory remains genuinely unimplemented: project_detector
    # gives a bounded top-level structure via inspect_project, but
    # there's no on-demand "list this specific subdirectory" tool yet.
    _planned_safe = [
        ("list_directory", "List a directory in the current project."),
    ]
    _planned_confirm: list[tuple[str, str]] = []
    for name, desc in _planned_safe:
        tool_registry.register(ToolSpec(
            name=name, description=desc,
            permission=PermissionLevel.SAFE, implemented=False,
        ))
    for name, desc in _planned_confirm:
        tool_registry.register(ToolSpec(
            name=name, description=desc,
            permission=PermissionLevel.CONFIRM, implemented=False,
            confirm_reason=f"JARVIS wants to run '{name}'.",
        ))


_build_default_registry()
