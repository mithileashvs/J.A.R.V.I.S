"""
JARVIS debug mode (Phase 3, Sections 9-13).

This is the integration layer — it does not implement code analysis,
terminal execution, or context gathering itself. It orchestrates the
three existing Phase 3 subsystems (context_manager.py, code_analysis.py,
terminal_tools.py) plus Phase 2's project_memory.py, in a bounded,
observable, cancellable sequence.

Terminal integration: this reads terminal_tools.get_last_result() —
the cached result of whatever command run_terminal_command most
recently executed. It does NOT capture or poll an external terminal
window; that would violate Section 15's "do not continuously capture
the screen" requirement and isn't feasible without OCR anyway. In
practice this means Debug Mode sees terminal evidence when the user
(or JARVIS itself, earlier in the conversation) actually ran a command
through JARVIS — which matches how the rest of Phase 3 is scoped: SEE
CONTEXT means what JARVIS can honestly gather through its own tools,
not passive surveillance of everything on screen.

Bounding (Section 12 — "do not create an uncontrolled autonomous
loop"):
  - MAX_STEPS hard cap on investigation steps
  - a wall-clock timeout for the whole investigation
  - a cancellation flag any caller can set, checked between every step
  - every step's outcome is recorded, so a cancelled/timed-out/failed
    investigation still returns whatever it found, never nothing

State visibility (Section 16): each step calls the provided
broadcast_state callback with a JarvisState.EXECUTING transition and a
`detail` string describing the current step — no new state-machine
enum values, per the smaller-change decision this module was built
under. If no callback is provided, investigation still runs; the
detail strings are simply not broadcast anywhere (useful for tests).

File modification (Section 14): this module NEVER edits a file. It
can recommend a fix in its diagnosis; applying one is a separate,
explicit, permission-gated action outside this module's scope.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("jarvis-debug")

MAX_STEPS = 10              # raised from Section 12's 8-step example to leave room for the
                             # optional environment/port-check steps Feature 2 adds — still
                             # conservative, and every extra step is conditional (skipped
                             # entirely when there's no evidence suggesting it's needed).
DEFAULT_TIMEOUT_SECONDS = 60.0

BroadcastFn = Callable[[dict], Awaitable[None]]


class Confidence(str, Enum):
    CONFIRMED = "Confirmed"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


# Ordering for "strongest evidence first" — used both to rank
# Hypothesis objects and (via to_incomplete_text) to pick which
# hypotheses to surface when an investigation stops early.
_CONFIDENCE_RANK = {
    Confidence.CONFIRMED: 0,
    Confidence.HIGH: 1,
    Confidence.MEDIUM: 2,
    Confidence.LOW: 3,
}


class HypothesisStatus(str, Enum):
    """
    Phase 4 Feature 2 — "do not assume the first error is the root
    cause." Every candidate explanation for the failure is tracked as
    a Hypothesis with one of these statuses rather than the
    investigation silently committing to whichever evidence source
    happened to run first.
    """
    UNTESTED = "UNTESTED"   # identified but not yet checked against evidence
    TESTING = "TESTING"     # a diagnostic step is actively evaluating it (transient — not
                             # observed on a finished Hypothesis, only meaningful mid-step)
    SUPPORTED = "SUPPORTED"  # some evidence points this way, not yet conclusive
    CONFIRMED = "CONFIRMED"  # evidence directly establishes this as the cause
    REJECTED = "REJECTED"   # evidence actively rules this out


@dataclass
class Hypothesis:
    id: str
    description: str
    confidence: Confidence
    status: HypothesisStatus
    evidence: list[str] = field(default_factory=list)
    test_action: Optional[str] = None    # what would/did confirm or reject this
    test_result: Optional[str] = None

    def to_text(self) -> str:
        lines = [
            f"HYPOTHESIS: {self.description}",
            f"Confidence: {self.confidence.value}",
            f"Status: {self.status.value}",
        ]
        if self.evidence:
            lines.append("Evidence:")
            lines += [f"- {e}" for e in self.evidence]
        if self.test_action:
            lines.append(f"Test: {self.test_action}")
        if self.test_result:
            lines.append(f"Result: {self.test_result}")
        return "\n".join(lines)


@dataclass
class InvestigationStep:
    step_number: int
    name: str
    finding: str
    evidence: Optional[str] = None


@dataclass
class Diagnosis:
    diagnosis: str
    evidence: str
    root_cause: Optional[str]
    confidence: Confidence
    symptom: Optional[str] = None       # the observed effect ("app won't start"), distinct
                                          # from root_cause (the underlying reason) — Section
                                          # "Separate: SYMPTOM / ROOT CAUSE / SECONDARY ERRORS"
    secondary_issues: list[str] = field(default_factory=list)
    recommended_fix: Optional[str] = None
    next_step: Optional[str] = None

    def to_text(self) -> str:
        """Section 11's exact requested output shape."""
        lines = [
            "DIAGNOSIS", self.diagnosis, "",
            "EVIDENCE", self.evidence, "",
        ]
        if self.symptom:
            lines += ["SYMPTOM", self.symptom, ""]
        lines += [
            "ROOT CAUSE", self.root_cause or "Not determined.", "",
            "CONFIDENCE", self.confidence.value,
        ]
        if self.secondary_issues:
            lines += ["", "SECONDARY ISSUES"] + [f"- {i}" for i in self.secondary_issues]
        if self.recommended_fix:
            lines += ["", "RECOMMENDED FIX", self.recommended_fix]
        if self.next_step:
            lines += ["", "NEXT STEP", self.next_step]
        return "\n".join(lines)


@dataclass
class InvestigationResult:
    steps: list[InvestigationStep] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    diagnosis: Optional[Diagnosis] = None
    cancelled: bool = False
    timed_out: bool = False
    stopped_reason: Optional[str] = None  # human-readable summary of why it stopped, whatever the reason

    def to_incomplete_text(self) -> str:
        """
        The exact "INVESTIGATION INCOMPLETE" report shape the Phase 4
        brief asks for, used when the investigation was cut off by the
        step/time budget (or cancelled) before it could reach a real
        diagnosis. Never fabricates a root cause — this is explicitly
        the "here's what we actually found, here's what to try next"
        report, not a downgraded Diagnosis.
        """
        lines = ["INVESTIGATION INCOMPLETE", ""]
        lines.append("Evidence collected:")
        if self.steps:
            for s in self.steps:
                lines.append(f"- [{s.name}] {s.finding}")
        else:
            lines.append("- None.")
        lines.append("")
        lines.append("Most likely causes:")
        ranked = sorted(
            self.hypotheses,
            key=lambda h: _CONFIDENCE_RANK.get(h.confidence, 99),
        )
        if ranked:
            for i, h in enumerate(ranked[:3], start=1):
                lines.append(f"{i}. {h.description} (confidence: {h.confidence.value})")
        else:
            lines.append("1. Not enough evidence was gathered to form even a tentative hypothesis.")
        lines.append("")
        lines.append("Recommended next action:")
        lines.append(
            self.stopped_reason
            or "Try again with a more specific file or error message to narrow the investigation."
        )
        return "\n".join(lines)


def guess_target_file(user_message: str, ctx) -> Optional[str]:
    """
    Best-effort, evidence-based only — never a guess dressed up as
    certainty. Looks for an explicit path-like token in the user's
    message first (most reliable), then falls back to a file
    mentioned in gathered context's relevant_file_paths.

    Module-level (not a method) because both Investigation and
    main.py's CODE_ANALYSIS/CODE_EXPLANATION chat handlers need the
    same "which file is the user talking about" logic — pulling it out
    here avoids main.py reaching into a private Investigation method,
    or duplicating the regex in two places.
    """
    path_pattern = re.compile(r'[\w./\\-]+\.\w{1,5}')
    matches = path_pattern.findall(user_message)
    candidates = [m for m in matches if "." in m and len(m) > 4]
    if candidates:
        return candidates[0]
    if ctx.relevant_file_paths:
        return ctx.relevant_file_paths[0]
    return None


class Investigation:
    """
    One bounded debugging investigation. Not reused across calls —
    create a fresh instance per "Jarvis, debug this" request. The
    cancel() flag is checked between steps (not preemptively
    interrupting a step already in progress — a terminal command
    mid-execution still respects ITS OWN timeout via terminal_tools.py,
    this is a separate, coarser control over the overall sequence).
    """

    def __init__(
        self,
        max_steps: int = MAX_STEPS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self._cancelled = False
        self._start_time: Optional[float] = None
        # Repeated-action detection (Section: INVESTIGATION SAFETY
        # CONTROLS). Tracks which diagnostic *keys* (not step numbers —
        # a key like "env:python" or "port:3000") have already been
        # run this investigation, so a later step that would ask for
        # the exact same diagnostic is skipped and reported as such
        # rather than silently re-run. This is deliberately a static
        # set checked by fixed steps, not an open-ended "let the model
        # decide what to check next" loop — Section 12 explicitly asks
        # NOT to build an uncontrolled autonomous loop, so the fixed
        # step sequence in run() stays the actual control flow; this
        # only guards the few steps that are conditional on evidence
        # (environment check, port check) against firing twice.
        self._actions_taken: set[str] = set()

    def cancel(self) -> None:
        self._cancelled = True

    def _mark_action(self, key: str) -> bool:
        """Returns True if this is the first time `key` has been requested this investigation, False if it's a repeat."""
        if key in self._actions_taken:
            return False
        self._actions_taken.add(key)
        return True

    def _time_remaining(self) -> float:
        if self._start_time is None:
            return self.timeout_seconds
        return self.timeout_seconds - (time.monotonic() - self._start_time)

    async def _emit(
        self,
        broadcast_state: Optional[BroadcastFn],
        step_number: int,
        detail: str,
    ) -> None:
        if broadcast_state is None:
            return
        try:
            import state as _state_mod
            await _state_mod.state_manager.set_state(
                _state_mod.JarvisState.EXECUTING,
                broadcast_state,
                detail=f"Debug step {step_number}/{self.max_steps}: {detail}",
            )
        except Exception as e:
            logger.warning(f"[debug] State broadcast failed (continuing investigation): {e}")

    async def run(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        broadcast_state: Optional[BroadcastFn] = None,
        screen_context=None,
    ) -> InvestigationResult:
        """
        Execute the bounded investigation. Follows Section 12's
        example sequence, adapted to what's actually available:

            1. Gather context (active project, window, prior facts)
            2. Identify a target file, if the user/context named one
            3. Check the most recent terminal output for an error
               (terminal_tools.get_last_result() + extract_errors() —
               see module docstring on why this is a cache read, not
               live terminal capture)
            4. Analyze the target file, if one was found
            5. Cross-reference project memory for a matching known issue
            6. Form and rank hypotheses from gathered evidence, produce
               the diagnosis

        screen_context (Phase 4 Feature 1, optional): a pre-captured
        screen_tools.ScreenContext, supplied by the CALLER only — this
        method never captures the screen itself. Screen capture always
        requires explicit confirmation (see tool_registry.py's
        analyze_screen tool), so a caller that wants screen evidence
        included in an investigation must go through that confirmation
        gate first and hand the resulting ScreenContext in here. This
        keeps "Debug Mode determines screen evidence is needed" true in
        spirit (it's used when offered) without ever letting an
        investigation silently trigger a capture on its own.

        A live terminal error (step 3) is treated as the strongest
        evidence when present — see _form_diagnosis() — but doesn't
        discard other evidence; known-issue and static-analysis
        findings are folded in as corroborating detail either way.

        Steps that don't apply (e.g. no target file identifiable, no
        recent terminal output) are recorded as such rather than
        skipped silently — Section 13 wants a session that can later
        answer "what did you check?" honestly, including the negative
        results.
        """
        self._start_time = time.monotonic()
        result = InvestigationResult()

        import context_manager as _cm
        import code_analysis as _ca
        import terminal_tools as _tt
        import project_memory as _pm

        step_num = 0

        def _budget_exhausted() -> bool:
            return step_num >= self.max_steps or self._time_remaining() <= 0

        # ── Step 1: gather context ──────────────────────────────
        step_num += 1
        if self._cancelled:
            result.cancelled = True
            result.stopped_reason = f"Cancelled before step {step_num}."
            return result
        await self._emit(broadcast_state, step_num, "gathering context")
        ctx = _cm.context_manager.gather(session_id=session_id, fact_query=user_message)
        finding = (
            f"Active project: {ctx.active_project['name']}" if ctx.active_project
            else "No active project set."
        )
        result.steps.append(InvestigationStep(step_num, "Gather context", finding, ctx.to_prompt_block()))

        if _budget_exhausted():
            result.stopped_reason = f"Step/time budget exhausted after step {step_num}."
            result.diagnosis = self._inconclusive_diagnosis(result)
            return result

        # ── Step 2: identify target file ────────────────────────
        step_num += 1
        if self._cancelled:
            result.cancelled = True
            result.stopped_reason = f"Cancelled before step {step_num}."
            return result
        await self._emit(broadcast_state, step_num, "identifying target file")
        target_file = self._guess_target_file(user_message, ctx)
        finding = f"Target file: {target_file}" if target_file else "No specific file identified from the request or context."
        result.steps.append(InvestigationStep(step_num, "Identify target file", finding))

        if _budget_exhausted():
            result.stopped_reason = f"Step/time budget exhausted after step {step_num}."
            result.diagnosis = self._inconclusive_diagnosis(result)
            return result

        # ── Step 3: check recent terminal output for an error ───
        terminal_error = None
        step_num += 1
        if self._cancelled:
            result.cancelled = True
            result.stopped_reason = f"Cancelled before step {step_num}."
            return result
        await self._emit(broadcast_state, step_num, "checking recent terminal output")
        last_terminal_result = _tt.get_last_result()
        if last_terminal_result is not None and (
            last_terminal_result.stderr or last_terminal_result.exit_code not in (0, None)
        ):
            # Only treat a command's output as error evidence when it
            # actually failed (nonzero exit / stderr) — a clean,
            # successful command (e.g. `python --version`) can still
            # print text that a naive "last line" heuristic might
            # otherwise mistake for an error. Matches the same gate
            # run_terminal_command's own handler uses when deciding
            # whether to attach extracted_error.
            combined_output = f"{last_terminal_result.stdout}\n{last_terminal_result.stderr}"
            extracted = _tt.extract_errors(combined_output)
            if extracted.primary_error:
                terminal_error = extracted
                finding = f"Recent command '{last_terminal_result.command}' shows: {extracted.primary_error}"
                evidence = (
                    f"Command: {last_terminal_result.command}\n"
                    f"Exit code: {last_terminal_result.exit_code}\n"
                    f"Primary error: {extracted.primary_error}\n"
                    + (f"Likely root cause: {extracted.likely_root_cause}\n" if extracted.likely_root_cause else "")
                )
            else:
                finding = f"Recent command '{last_terminal_result.command}' failed, but no recognizable error pattern was found in its output."
                evidence = None
        elif last_terminal_result is not None:
            finding = f"Recent command '{last_terminal_result.command}' ran successfully (exit code {last_terminal_result.exit_code}); no error to investigate there."
            evidence = None
        else:
            finding = "No recent terminal output available (no command run recently through JARVIS)."
            evidence = None
        result.steps.append(InvestigationStep(step_num, "Check terminal output", finding, evidence))

        if _budget_exhausted():
            result.stopped_reason = f"Step/time budget exhausted after step {step_num}."
            result.diagnosis = self._inconclusive_diagnosis(result)
            return result

        # ── Step 3b: check environment/dependencies, if the terminal ──
        # error suggests it's relevant (Section 11's own worked example
        # — distinguishing "package genuinely missing" from "wrong
        # interpreter/environment active" without the model guessing).
        # Conditional and skipped entirely when there's no evidence
        # pointing at an environment problem — this is what keeps
        # Feature 2's extra steps from turning every investigation into
        # a fixed 10-step slog regardless of relevance.
        environment_check = None
        if terminal_error is not None and terminal_error.error_type == "python_traceback" and ctx.active_project:
            technologies = (ctx.active_project or {}).get("technologies", [])
            env_target = "python" if "Python" in technologies else ("node" if "Node.js" in technologies else None)
            if env_target and self._mark_action(f"env:{env_target}"):
                step_num += 1
                if self._cancelled:
                    result.cancelled = True
                    result.stopped_reason = f"Cancelled before step {step_num}."
                    return result
                await self._emit(broadcast_state, step_num, f"checking {env_target} environment/dependencies")
                environment_check = await _tt.inspect_environment(env_target, cwd=ctx.active_project_path)
                if environment_check.get("available"):
                    finding = f"Inspected active {env_target} environment (version + installed packages)."
                else:
                    finding = f"Could not inspect {env_target} environment: {environment_check.get('reason')}"
                result.steps.append(InvestigationStep(step_num, "Check environment/dependencies", finding))

                if _budget_exhausted():
                    result.stopped_reason = f"Step/time budget exhausted after step {step_num}."
                    result.diagnosis = self._inconclusive_diagnosis(result)
                    return result

        # ── Step 3c: check port usage, if the terminal error is a ──
        # port conflict — same conditional-and-skippable pattern as
        # the environment check above.
        port_check = None
        if terminal_error is not None and terminal_error.error_type == "port_conflict":
            port_match = re.search(r"port (\d+)", terminal_error.primary_error or "", re.IGNORECASE)
            if port_match and self._mark_action(f"port:{port_match.group(1)}"):
                step_num += 1
                if self._cancelled:
                    result.cancelled = True
                    result.stopped_reason = f"Cancelled before step {step_num}."
                    return result
                port_num = int(port_match.group(1))
                await self._emit(broadcast_state, step_num, f"checking what's using port {port_num}")
                import awareness as _aw
                port_check = _aw.check_port_usage(port_num)
                if port_check.get("available") and port_check.get("in_use"):
                    proc = port_check.get("process_name") or f"PID {port_check.get('pid')}"
                    finding = f"Port {port_num} is in use by {proc}."
                elif port_check.get("available"):
                    finding = f"Port {port_num} is not currently in use (may have freed up already)."
                else:
                    finding = f"Could not check port {port_num}: {port_check.get('reason')}"
                result.steps.append(InvestigationStep(step_num, "Check port usage", finding))

                if _budget_exhausted():
                    result.stopped_reason = f"Step/time budget exhausted after step {step_num}."
                    result.diagnosis = self._inconclusive_diagnosis(result)
                    return result

        # ── Step 4: analyze target file, if found ───────────────
        analysis_result = None
        step_num += 1
        if self._cancelled:
            result.cancelled = True
            result.stopped_reason = f"Cancelled before step {step_num}."
            return result
        await self._emit(broadcast_state, step_num, "analyzing code" if target_file else "skipping code analysis (no target file)")
        if target_file:
            try:
                analysis_result = _ca.analyze_file(target_file)
                finding = f"{len(analysis_result.issues)} issue(s) found by static analysis."
                evidence = analysis_result.to_text()
            except (FileNotFoundError, ValueError) as e:
                finding = f"Could not analyze '{target_file}': {e}"
                evidence = None
            result.steps.append(InvestigationStep(step_num, "Analyze code", finding, evidence))
        else:
            result.steps.append(InvestigationStep(step_num, "Analyze code", "Skipped — no target file."))

        if _budget_exhausted():
            result.stopped_reason = f"Step/time budget exhausted after step {step_num}."
            result.diagnosis = self._inconclusive_diagnosis(result)
            return result

        # ── Step 4b: incorporate pre-confirmed screen evidence, if any ──
        # Never captured here — see run()'s docstring. Only recorded
        # as a step (and later used in diagnosis) when the caller
        # already went through analyze_screen's confirmation gate and
        # handed the result in.
        if screen_context is not None:
            step_num += 1
            if self._cancelled:
                result.cancelled = True
                result.stopped_reason = f"Cancelled before step {step_num}."
                return result
            await self._emit(broadcast_state, step_num, "incorporating screen evidence")
            if screen_context.available and screen_context.detected_errors:
                finding = f"Screen shows: {screen_context.detected_errors[0]}"
                evidence = "\n".join(screen_context.detected_errors[:5])
            elif screen_context.available:
                finding = f"Active window ({screen_context.application_type}) captured, no clear error text found."
                evidence = None
            else:
                finding = f"Screen evidence unavailable: {screen_context.reason}"
                evidence = None
            result.steps.append(InvestigationStep(step_num, "Check screen evidence", finding, evidence))

            if _budget_exhausted():
                result.stopped_reason = f"Step/time budget exhausted after step {step_num}."
                result.diagnosis = self._inconclusive_diagnosis(result)
                return result

        # ── Step 5: check project memory for a matching known issue ──
        step_num += 1
        if self._cancelled:
            result.cancelled = True
            result.stopped_reason = f"Cancelled before step {step_num}."
            return result
        await self._emit(broadcast_state, step_num, "checking project memory for known issues")
        known_issue = None
        if ctx.active_project_path:
            matches = _pm.search_facts(ctx.active_project_path, user_message, limit=3)
            known_matches = [m for m in matches if m["kind"] in ("known_issue", "previous_fix")]
            if known_matches:
                known_issue = known_matches[0]
        finding = f"Matching known issue: {known_issue['content']}" if known_issue else "No matching known issue in project memory."
        result.steps.append(InvestigationStep(step_num, "Check project memory", finding))

        if _budget_exhausted():
            result.stopped_reason = f"Step/time budget exhausted after step {step_num}."
            result.diagnosis = self._inconclusive_diagnosis(result)
            return result

        # ── Step 6: build and rank hypotheses ───────────────────
        step_num += 1
        if self._cancelled:
            result.cancelled = True
            result.stopped_reason = f"Cancelled before step {step_num}."
            return result
        await self._emit(broadcast_state, step_num, "ranking hypotheses")
        result.hypotheses = self._build_hypotheses(
            terminal_error, known_issue, analysis_result, target_file,
            screen_context, environment_check, port_check,
        )
        if result.hypotheses:
            finding = f"{len(result.hypotheses)} hypothesis/hypotheses formed, strongest: {result.hypotheses[0].description}"
        else:
            finding = "No hypotheses could be formed from the evidence gathered so far."
        result.steps.append(InvestigationStep(step_num, "Rank hypotheses", finding))

        if _budget_exhausted():
            result.stopped_reason = f"Step/time budget exhausted after step {step_num}."
            result.diagnosis = self._inconclusive_diagnosis(result)
            return result

        # ── Step 7: form diagnosis from the top-ranked hypothesis ──
        step_num += 1
        await self._emit(broadcast_state, step_num, "forming diagnosis")
        result.diagnosis = self._form_diagnosis(result, target_file)
        result.steps.append(InvestigationStep(step_num, "Form diagnosis", "Diagnosis produced from available evidence."))

        return result

    def _guess_target_file(self, user_message: str, ctx) -> Optional[str]:
        return guess_target_file(user_message, ctx)

    def _build_hypotheses(
        self, terminal_error, known_issue, analysis_result, target_file,
        screen_context, environment_check, port_check,
    ) -> list[Hypothesis]:
        """
        Turn each independent evidence source into a Hypothesis,
        appended in the same "strongest evidence first" priority order
        the investigation always followed:

          1. A live terminal error — real, current proof something is
             actually failing right now, stronger than either project
             memory or static analysis, both of which describe the
             code in general rather than the live failure.
          2. A matching known issue in project memory — this exact
             problem was diagnosed before, but it's not live evidence.
          3. Static analysis findings.
          4. Screen evidence (OCR) — always the weakest tier: it's the
             least precise source available (see screen_tools.py).

        This is a priority ordering by evidence *kind*, not a raw sort
        by each hypothesis's own confidence value — a MEDIUM-confidence
        live terminal error is still more trustworthy than a
        CONFIRMED-confidence static-analysis finding about an unrelated
        part of the code, because it's evidence of what's actually
        happening right now.
        """
        hypotheses: list[Hypothesis] = []

        if terminal_error is not None and terminal_error.primary_error:
            confidence = Confidence.CONFIRMED if terminal_error.error_type not in (None, "generic") else Confidence.MEDIUM
            evidence = [f"Primary error: {terminal_error.primary_error}"]
            if terminal_error.relevant_files:
                evidence.append(f"Files referenced in the error: {', '.join(terminal_error.relevant_files)}")
            if environment_check is not None and environment_check.get("available"):
                evidence.append(f"Active {environment_check.get('target')} environment inspected — see that step's evidence for versions/packages.")
            if port_check is not None and port_check.get("in_use"):
                proc = port_check.get("process_name") or f"PID {port_check.get('pid')}"
                evidence.append(f"Port {port_check.get('port')} is currently in use by {proc}.")
            hypotheses.append(Hypothesis(
                id="terminal_error",
                description=terminal_error.likely_root_cause or terminal_error.primary_error,
                confidence=confidence,
                status=HypothesisStatus.CONFIRMED if confidence == Confidence.CONFIRMED else HypothesisStatus.SUPPORTED,
                evidence=evidence,
                test_action="Re-run the command after applying a fix to confirm the error is gone.",
            ))

        if known_issue:
            hypotheses.append(Hypothesis(
                id="known_issue",
                description=known_issue["content"],
                confidence=Confidence.HIGH,
                status=HypothesisStatus.SUPPORTED,
                evidence=[f"Found in project memory (kind={known_issue['kind']})."],
                test_action="Verify this is still the cause before reapplying the previous fix.",
            ))

        if analysis_result and analysis_result.issues:
            severity_rank = {"error": 0, "warning": 1, "note": 2}

            def _issue_rank(issue):
                base = severity_rank.get(issue.severity, 3)
                if "undefined name" in issue.message.lower():
                    base -= 0.5
                return base

            ranked_issues = sorted(analysis_result.issues, key=_issue_rank)
            top_issues = [i.message for i in ranked_issues[:3]]
            confirmed_count = sum(1 for i in analysis_result.issues if i.confidence == "Confirmed")
            hypotheses.append(Hypothesis(
                id="static_analysis",
                description=top_issues[0] if top_issues else f"{len(analysis_result.issues)} static analysis issue(s) in '{target_file}'",
                confidence=Confidence.CONFIRMED if confirmed_count == len(analysis_result.issues) else Confidence.HIGH,
                status=HypothesisStatus.CONFIRMED if confirmed_count == len(analysis_result.issues) else HypothesisStatus.SUPPORTED,
                evidence=top_issues,
                test_action="Review the flagged lines; run analyze_code again after a fix to confirm it's resolved.",
            ))

        if screen_context is not None and screen_context.available and screen_context.detected_errors:
            hypotheses.append(Hypothesis(
                id="screen_evidence",
                description=screen_context.detected_errors[0],
                confidence=Confidence.LOW,  # OCR text is inherently imprecise — never claim higher
                status=HypothesisStatus.SUPPORTED,
                evidence=screen_context.detected_errors[:5],
                test_action="Confirm this is the actual error (OCR can misread text) and provide the exact message if possible.",
            ))

        return hypotheses

    def _form_diagnosis(self, result: InvestigationResult, target_file) -> Diagnosis:
        """
        Build the final Diagnosis from result.hypotheses (already
        ranked by _build_hypotheses — see that method's docstring for
        the priority rationale). Falls back to the same "no evidence
        at all" messages the investigation always gave when there are
        no hypotheses to report.
        """
        hypotheses = result.hypotheses

        if hypotheses:
            top = hypotheses[0]
            secondary = [h.description for h in hypotheses[1:]]
            symptom = None
            if top.id == "terminal_error" and top.evidence:
                raw_symptom = top.evidence[0].removeprefix("Primary error: ")
                if raw_symptom != top.description:
                    symptom = raw_symptom
            return Diagnosis(
                diagnosis=(f"A recent command produced an error: {top.evidence[0].removeprefix('Primary error: ')}"
                           if top.id == "terminal_error" else top.description),
                evidence="\n".join(top.evidence) if top.evidence else "No direct evidence recorded.",
                root_cause=top.description,
                confidence=top.confidence,
                symptom=symptom,
                secondary_issues=secondary,
                next_step=top.test_action,
            )

        if not target_file:
            return Diagnosis(
                diagnosis="Could not identify a specific file to investigate.",
                evidence="No file path was found in the request, and no relevant file was in gathered context.",
                root_cause=None,
                confidence=Confidence.LOW,
                next_step="Name the file, or open it so it's the active window, and try again.",
            )

        return Diagnosis(
            diagnosis=f"'{target_file}' was analyzed but no issues were found by static analysis.",
            evidence="Static analysis (pyflakes for Python) reported zero issues.",
            root_cause=None,
            confidence=Confidence.LOW,
            next_step="The problem may be a runtime/logic issue static analysis can't catch — "
                       "describe the actual symptom (error message, unexpected output) for further help.",
        )

    def _inconclusive_diagnosis(self, result: InvestigationResult) -> Diagnosis:
        completed = [s.name for s in result.steps]
        return Diagnosis(
            diagnosis="Investigation stopped before completion (step or time limit reached).",
            evidence=f"Completed steps: {', '.join(completed) if completed else 'none'}.",
            root_cause=None,
            confidence=Confidence.LOW,
            next_step="Try again with a more specific file or error message to narrow the investigation.",
        )
