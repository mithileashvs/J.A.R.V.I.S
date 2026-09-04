"""
JARVIS Phase 6 — Central Workflow Engine.

This is the ONE authoritative implementation of multi-step agentic
workflow execution for JARVIS. It does not replace anything from
Phases 1-5 — it coordinates them:

  - Steps that name a tool run through tool_registry.run_tool(), which
    already enforces permissions.permission_manager (SAFE/CONFIRM/
    BLOCKED) and per-tool timeouts. This engine never calls a tool
    directly and never re-implements permission checking — Rule 2/3
    of the Phase 6 spec ("Permission Manager and Tool Registry are
    authoritative").
  - Steps that are pure, already-safe, read-only observations (project
    detection, static analysis, git status) call small handler
    functions registered per workflow "kind" — the same pattern
    debug_mode.Investigation already uses for its fixed step sequence
    (direct calls to context_manager/code_analysis/git_tools, not
    routed through the tool registry, because they're in-process reads
    with no side effects and nothing to confirm).
  - Progress is broadcast through the existing state.py StateManager
    (JarvisState.EXECUTING + a `detail` string), exactly like
    debug_mode.Investigation._emit() does — no new UI/event system.
  - Workflow "memory" (Feature 8) is stored via the EXISTING
    project_memory.py facts table, using its existing 'known_issue' /
    'previous_fix' kinds — no second database.
  - The audit log (Feature 16) is the EXISTING memory.py
    log_event()/get_recent_events() system — no second logger.

Workflow state (CREATED/PLANNING/RUNNING/...) is deliberately a
separate enum from state.JarvisState — the assistant's global
IDLE/LISTENING/THINKING/EXECUTING/SPEAKING/ERROR machine is about what
the voice pipeline is doing right now; WorkflowStatus is about the
lifecycle of one long-running background task, of which there can be
several, and which can keep existing (PAUSED, WAITING_FOR_PERMISSION)
while the assistant itself sits IDLE.

Safety (Feature 15), all enforced inside _run_loop() below:
  - max_steps / max_tool_calls / timeout_seconds — hard caps
  - repeated-action detection — the same (action_key, args) pair
    failing 3 times in a row stops the workflow rather than retrying
    forever
  - cooperative pause/cancel — checked between steps, never mid-step;
    matches debug_mode.Investigation's cancel() precedent
  - cancellation is idempotent — cancel() on an already-terminal
    workflow is a no-op, not an error

Verification (Feature 9): every WorkflowStep records an `outcome` that
is one of ATTEMPTED / VERIFIED_SUCCESS / VERIFIED_FAILURE. A step
handler that only performed an action without checking its result
must report ATTEMPTED — the engine never upgrades that to
VERIFIED_SUCCESS on its own. Workflow.to_report() below refuses to say
a workflow "succeeded" unless every step that could be verified was.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("jarvis-workflow")

BroadcastFn = Callable[[dict], Awaitable[None]]

# ── Safety defaults (Feature 15) ────────────────────────────────────
DEFAULT_MAX_STEPS = 10
DEFAULT_MAX_TOOL_CALLS = 25
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_PER_STEP_TIMEOUT_SECONDS = 60.0
_REPEATED_ACTION_LIMIT = 3   # same action+args failing this many times in a row -> stop
_MAX_RECOVERY_ATTEMPTS = 1   # Feature 17: at most one auto replan/retry per failing action


class WorkflowStatus(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    WAITING_FOR_PERMISSION = "WAITING_FOR_PERMISSION"
    RUNNING = "RUNNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    PAUSED = "PAUSED"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


_TERMINAL_STATUSES = {
    WorkflowStatus.COMPLETED, WorkflowStatus.FAILED,
    WorkflowStatus.CANCELLED, WorkflowStatus.TIMED_OUT,
}


def is_active(status: WorkflowStatus) -> bool:
    """True for any workflow status a pause/resume/cancel command could
    plausibly apply to — i.e. not yet finished one way or another.
    Exposed so callers (main.py's chat() meta-commands) don't have to
    duplicate/guess at the terminal-status set."""
    return status not in _TERMINAL_STATUSES


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class StepOutcome(str, Enum):
    """Feature 9 — never conflate 'we ran it' with 'we checked it worked'."""
    ATTEMPTED = "ATTEMPTED"
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    VERIFIED_FAILURE = "VERIFIED_FAILURE"


class AutonomyLevel(int, Enum):
    """Feature 3. Purely a UX/planning knob — see module docstring:
    it can never relax what permissions.py / tool_registry.py enforce.
    LEVEL 3 still goes through CONFIRM for every non-SAFE tool; the
    only thing autonomy level changes is how eagerly the WORKFLOW
    itself proceeds between already-permitted steps."""
    MANUAL = 0
    SAFE_ASSISTED = 1
    CONFIRM_BEFORE_CHANGES = 2
    CONTROLLED_WORKFLOW_AUTONOMY = 3


@dataclass
class StepResult:
    """What a handler function (the non-tool-registry ACT path) returns."""
    summary: str
    verified_success: Optional[bool] = None   # None -> ATTEMPTED, True/False -> VERIFIED_*
    evidence: Optional[str] = None
    error: Optional[str] = None
    # Feature 13 — set this instead of (or alongside) summary/evidence
    # to suspend the step and ask the user something. The engine stops
    # the run, surfaces this text via Workflow.pending_input, and the
    # SAME handler is invoked again once WorkflowEngine.provide_input()
    # supplies Workflow.last_user_input — the handler itself decides
    # whether last_user_input is set to tell a fresh call from a
    # resumed one.
    awaiting_input: Optional[str] = None


@dataclass
class WorkflowStep:
    description: str
    # Exactly one of these should be meaningful (or neither, for a
    # purely informational step like "report findings"):
    #  - tool_name set  -> engine executes via tool_registry.run_tool()
    #    (permission-gated, timed, exactly like every other tool call
    #    in JARVIS).
    #  - handler_key set -> engine calls the workflow kind's own
    #    registered read-only handler function directly.
    tool_name: Optional[str] = None
    tool_args: dict = field(default_factory=dict)
    handler_key: Optional[str] = None
    status: StepStatus = StepStatus.PENDING
    outcome: Optional[StepOutcome] = None
    result: Optional[str] = None
    evidence: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None

    def action_key(self) -> str:
        """Identity used for repeated-action/loop detection."""
        return f"{self.tool_name or self.handler_key or self.description}:{json.dumps(self.tool_args, sort_keys=True)}"

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "tool_name": self.tool_name,
            "handler_key": self.handler_key,
            "status": self.status.value,
            "outcome": self.outcome.value if self.outcome else None,
            "result": self.result,
            "evidence": self.evidence,
            "error": self.error,
        }


@dataclass
class Workflow:
    id: str
    kind: str
    user_request: str
    goal: str
    steps: list[WorkflowStep]
    status: WorkflowStatus = WorkflowStatus.CREATED
    current_step: int = 0
    max_steps: int = DEFAULT_MAX_STEPS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    tool_calls_made: int = 0
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    autonomy_level: AutonomyLevel = AutonomyLevel.SAFE_ASSISTED
    project_path: Optional[str] = None
    session_id: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    evidence: list[str] = field(default_factory=list)
    permissions_requested: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    retry_count: int = 0
    stopped_reason: Optional[str] = None
    cancel_requested: bool = False
    pause_requested: bool = False
    # Consecutive-failure tracking for repeated-action detection
    # (Feature 15) — (action_key -> consecutive failure count).
    _consecutive_failures: dict = field(default_factory=dict, repr=False)
    # Feature 17 — (action_key -> recovery/replan attempts already made).
    # Separate from _consecutive_failures: this caps how many times the
    # *engine itself* will try to recover a given failing step, which is
    # intentionally tighter than the repeated-action safety net.
    recovery_attempts: dict = field(default_factory=dict, repr=False)
    # Structured Approval UX / Feature 10 — how many of the upcoming
    # tool-backed steps have been pre-approved by a "approve N" chat
    # command. Consumed one at a time as each such step actually runs;
    # 0 means "ask again" (the normal WAITING_FOR_PERMISSION gate).
    # Distinct from the `auto_approved` argument to run()/start(), which
    # blanket-approves an entire run (used by "approve all").
    approvals_remaining: int = field(default=0, repr=False)
    # Feature 8 — facts recalled from project_memory.py at creation
    # time (see WorkflowEngine._recall_prior_context()), so a new
    # workflow can be informed by what a previous one already found
    # for this project. Empty when there's no project_path or nothing
    # was ever recorded for it — never fabricated.
    prior_context: list[dict] = field(default_factory=list, repr=False)
    # Feature 13 — the suspend-and-wait-for-user-answer primitive.
    # pending_input holds what's being asked (None when not waiting);
    # last_user_input holds the most recent answer supplied via
    # provide_input(), which the paused step's handler reads to tell a
    # fresh call from a resumed one.
    pending_input: Optional[dict] = field(default=None, repr=False)
    last_user_input: Optional[str] = field(default=None, repr=False)

    def prior_context_summary(self, max_items: int = 3) -> str:
        """
        Feature 8 — a short, human-readable digest of prior_context, for
        a workflow's own report step to fold in if it chooses to (see
        workflows/project_review.py's report step for the first
        consumer). Empty string when there's nothing recalled — callers
        should treat that as "say nothing," not as an error.
        """
        if not self.prior_context:
            return ""
        lines = []
        for fact in self.prior_context[:max_items]:
            try:
                payload = json.loads(fact.get("content", "{}"))
            except (ValueError, TypeError):
                payload = {}
            label = "Previously fixed" if fact.get("kind") == "previous_fix" else "Previously flagged"
            goal = payload.get("goal", fact.get("kind", "prior run"))
            lines.append(f"- {label}: {goal}")
        return "\n".join(lines)

    def to_checklist(self) -> list[dict]:
        """
        Feature 12 — user-facing itemized progress checklist, derived
        entirely from existing step/workflow state (no second source of
        truth: nothing here is tracked independently of StepStatus /
        Workflow.current_step / Workflow.status).

        Marker rules:
          [v] DONE steps (checkmark)
          [*] the step currently executing (current_step index, while
              the workflow is actively running/recovering/awaiting a
              tool permission for that same step)
          [ ] PENDING steps not yet reached
          [x] FAILED steps
          [-] SKIPPED steps, and any still-PENDING step left behind by
              a cancelled workflow (nothing will ever run it now)
        """
        checkmark, current, pending, failed, skipped = "\u2713", "\u25cf", "\u25cb", "\u2717", "\u2013"
        in_flight_statuses = (
            WorkflowStatus.RUNNING, WorkflowStatus.RECOVERING,
            WorkflowStatus.WAITING_FOR_PERMISSION, WorkflowStatus.WAITING_FOR_USER,
            WorkflowStatus.PLANNING,
        )
        items = []
        for i, step in enumerate(self.steps):
            if step.status == StepStatus.DONE:
                marker = checkmark
            elif step.status == StepStatus.FAILED:
                marker = failed
            elif step.status == StepStatus.SKIPPED:
                marker = skipped
            elif self.status == WorkflowStatus.CANCELLED:
                # Nothing pending will run now — represent it as skipped
                # rather than implying it's still "coming up".
                marker = skipped
            elif i == self.current_step and self.status in in_flight_statuses:
                marker = current
            else:
                marker = pending
            items.append({
                "index": i + 1,
                "description": step.description,
                "marker": marker,
                "status": step.status.value,
                "outcome": step.outcome.value if step.outcome else None,
            })
        return items

    def to_checklist_text(self) -> str:
        """Plain-text rendering of to_checklist(), one line per step —
        the representation main.py hands back to the user/chat UI."""
        return "\n".join(
            f"[{item['marker']}] {item['index']}. {item['description']}"
            for item in self.to_checklist()
        )

    def to_dict(self) -> dict:
        """Feature 12 — real progress, straight from workflow state, no faking."""
        return {
            "type": "workflow_progress",
            "id": self.id,
            "kind": self.kind,
            "goal": self.goal,
            "status": self.status.value,
            "current_step": self.current_step,
            "total_steps": len(self.steps),
            "max_steps": self.max_steps,
            "tool_calls_made": self.tool_calls_made,
            "max_tool_calls": self.max_tool_calls,
            "steps": [s.to_dict() for s in self.steps],
            "checklist": self.to_checklist(),
            "stopped_reason": self.stopped_reason,
            "errors": self.errors,
        }

    def to_report(self) -> str:
        """
        Feature 9 / Rule 7 — never say 'Done' without evidence. A
        workflow is only reported as succeeded if it reached
        COMPLETED *and* no step ended in VERIFIED_FAILURE or
        unverified FAILED status.
        """
        lines = [f"WORKFLOW: {self.goal}", f"Status: {self.status.value}"]
        any_unverified_failure = any(
            s.status == StepStatus.FAILED or s.outcome == StepOutcome.VERIFIED_FAILURE
            for s in self.steps
        )
        if self.status == WorkflowStatus.COMPLETED and not any_unverified_failure:
            lines.append("Result: VERIFIED SUCCESS")
        elif self.status == WorkflowStatus.COMPLETED:
            lines.append("Result: COMPLETED WITH UNRESOLVED ISSUES (see errors below)")
        else:
            lines.append(f"Result: NOT COMPLETED ({self.stopped_reason or self.status.value})")
        lines.append("")
        for i, step in enumerate(self.steps, start=1):
            marker = {
                StepStatus.PENDING: " ", StepStatus.RUNNING: ">",
                StepStatus.DONE: "x", StepStatus.FAILED: "!",
                StepStatus.SKIPPED: "-",
            }[step.status]
            outcome = f" [{step.outcome.value}]" if step.outcome else ""
            lines.append(f"  [{marker}] {i}. {step.description}{outcome}")
            if step.result:
                lines.append(f"        -> {step.result}")
            if step.error:
                lines.append(f"        ! {step.error}")
        if self.errors:
            lines.append("")
            lines.append("Errors:")
            lines.extend(f"  - {e}" for e in self.errors)
        return "\n".join(lines)


StepHandlerFn = Callable[[Workflow, WorkflowStep], Awaitable[StepResult]]


@dataclass
class WorkflowKindSpec:
    """One registered workflow 'shape' (project_review, dev_env_prep, ...).
    build_steps() is this kind's planner — mirrors core/task_planner.py's
    named-planner pattern, just returning WorkflowStep objects instead
    of TaskStep objects."""
    name: str
    build_steps: Callable[..., list[WorkflowStep]]
    handlers: dict[str, StepHandlerFn] = field(default_factory=dict)


class WorkflowEngine:
    """
    Single shared engine instance (module-level singleton below),
    matching the pattern of permission_manager / tool_registry /
    state_manager / task_manager elsewhere in this codebase.
    """

    def __init__(self) -> None:
        self._workflows: dict[str, Workflow] = {}
        self._kinds: dict[str, WorkflowKindSpec] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # session_id -> most recent workflow_id created for that session,
        # so chat-level "Jarvis, pause/continue/cancel" commands (which
        # don't name a workflow ID) know which workflow they mean —
        # mirrors how debug_mode.py's Investigation is implicitly scoped
        # to "whatever the user is currently doing in this session."
        self._latest_by_session: dict[str, str] = {}

    # ── Kind registration ───────────────────────────────────────
    def register_kind(self, spec: WorkflowKindSpec) -> None:
        if spec.name in self._kinds:
            logger.warning(f"[workflow] Overwriting existing kind registration for '{spec.name}'")
        self._kinds[spec.name] = spec

    def available_kinds(self) -> list[str]:
        return sorted(self._kinds)

    # ── Creation ─────────────────────────────────────────────────
    def create_workflow(
        self,
        kind: str,
        user_request: str,
        goal: Optional[str] = None,
        project_path: Optional[str] = None,
        session_id: Optional[str] = None,
        autonomy_level: AutonomyLevel = AutonomyLevel.SAFE_ASSISTED,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        **planner_kwargs: Any,
    ) -> Workflow:
        spec = self._kinds.get(kind)
        if spec is None:
            raise ValueError(f"No workflow kind registered for '{kind}'. Known: {self.available_kinds()}")

        steps = spec.build_steps(**planner_kwargs)
        workflow = Workflow(
            id=str(uuid.uuid4()),
            kind=kind,
            user_request=user_request,
            goal=goal or f"{kind} for {project_path or 'the current project'}",
            steps=steps,
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_seconds,
            autonomy_level=autonomy_level,
            project_path=project_path,
            session_id=session_id,
        )
        self._workflows[workflow.id] = workflow
        if session_id:
            self._latest_by_session[session_id] = workflow.id
        if project_path:
            workflow.prior_context = self._recall_prior_context(project_path)
            if workflow.prior_context:
                self._audit(
                    workflow, "workflow_recalled_context",
                    f"{len(workflow.prior_context)} prior fact(s) for {project_path}",
                )
        self._audit(workflow, "workflow_created", f"kind={kind} goal={workflow.goal}")
        return workflow

    def _recall_prior_context(self, project_path: str, limit: int = 5) -> list[dict]:
        """
        Feature 8 — the read side of workflow memory. Reuses
        project_memory.py's existing facts table (the same
        'previous_fix'/'known_issue' rows _persist_workflow_memory()
        already writes on completion) rather than a second store.
        Best-effort: a lookup failure (no DB yet, etc.) just means "no
        prior context," never a workflow-creation error.
        """
        try:
            import project_memory as _pm
            _pm.init_project_memory_db()
            issues = _pm.get_facts(project_path, kind="known_issue", limit=limit)
            fixes = _pm.get_facts(project_path, kind="previous_fix", limit=limit)
            combined = sorted(issues + fixes, key=lambda f: f.get("created_at", ""), reverse=True)
            return combined[:limit]
        except Exception as e:  # noqa: BLE001 — recall is an enhancement, never a hard dependency
            logger.warning(f"[workflow] Prior-context recall failed (continuing): {e}")
            return []

    def get(self, workflow_id: str) -> Optional[Workflow]:
        return self._workflows.get(workflow_id)

    def latest_for_session(self, session_id: str) -> Optional[Workflow]:
        """Feature 11 support — resolves an un-scoped 'pause'/'continue'/
        'cancel' chat command to the workflow it almost certainly means."""
        workflow_id = self._latest_by_session.get(session_id)
        return self._workflows.get(workflow_id) if workflow_id else None

    def list_workflows(self) -> list[Workflow]:
        return list(self._workflows.values())

    # ── Lifecycle controls (Feature 11) ─────────────────────────
    def pause(self, workflow_id: str) -> bool:
        workflow = self._workflows.get(workflow_id)
        if workflow is None or workflow.status in _TERMINAL_STATUSES:
            return False
        workflow.pause_requested = True
        return True

    def resume(self, workflow_id: str, broadcast: Optional[BroadcastFn] = None, auto_approved: bool = False) -> bool:
        workflow = self._workflows.get(workflow_id)
        if workflow is None or workflow.status != WorkflowStatus.PAUSED:
            return False
        workflow.pause_requested = False
        workflow.status = WorkflowStatus.RUNNING
        self.start(workflow_id, broadcast=broadcast, auto_approved=auto_approved)
        return True

    def cancel(self, workflow_id: str) -> bool:
        """Idempotent by design — repeated cancellation is always safe (Feature 15/11)."""
        workflow = self._workflows.get(workflow_id)
        if workflow is None:
            return False
        if workflow.status in _TERMINAL_STATUSES:
            return True  # already stopped; nothing to do, not an error
        workflow.cancel_requested = True
        task = self._tasks.get(workflow_id)
        if task is not None and not task.done():
            task.cancel()
        return True

    # ── Structured Approval UX (Feature 10) ─────────────────────────
    # Preserves the single-step WAITING_FOR_PERMISSION gate that already
    # exists (a workflow always stops there first) and adds real,
    # multi-action approval on top of it, instead of a parallel system:
    # approving/rejecting always acts on "whatever this workflow is
    # currently waiting on," the same way pause/resume/cancel already
    # resolve to workflow_engine.latest_for_session().
    def pending_steps_preview(self, workflow_id: str, max_items: int = 5) -> list[dict]:
        """The 'Recommended actions' list for a workflow currently
        WAITING_FOR_PERMISSION — the upcoming tool-backed steps, so the
        user can see what they'd be approving before saying yes."""
        workflow = self._workflows.get(workflow_id)
        if workflow is None:
            return []
        upcoming = [
            {"index": i + 1, "description": s.description, "tool_name": s.tool_name}
            for i, s in enumerate(workflow.steps[workflow.current_step:], start=1)
            if s.tool_name
        ]
        return upcoming[:max_items]

    def approve_steps(self, workflow_id: str, count: int = 1, broadcast: Optional[BroadcastFn] = None) -> bool:
        """'approve 1' / 'approve 1,2' (count=2, they're consumed in
        order) — pre-approves exactly `count` upcoming tool-backed
        steps, then the gate reasserts itself for anything after that.
        Only valid while genuinely WAITING_FOR_PERMISSION."""
        workflow = self._workflows.get(workflow_id)
        if workflow is None or workflow.status != WorkflowStatus.WAITING_FOR_PERMISSION:
            return False
        workflow.approvals_remaining = max(1, count)
        workflow.status = WorkflowStatus.RUNNING
        self.start(workflow_id, broadcast=broadcast, auto_approved=False)
        return True

    def approve_all_remaining(self, workflow_id: str, broadcast: Optional[BroadcastFn] = None) -> bool:
        """'approve all' — the existing `auto_approved=True` run-wide
        semantics, exposed as an explicit, named approval action rather
        than an internal-only argument."""
        workflow = self._workflows.get(workflow_id)
        if workflow is None or workflow.status != WorkflowStatus.WAITING_FOR_PERMISSION:
            return False
        workflow.status = WorkflowStatus.RUNNING
        self.start(workflow_id, broadcast=broadcast, auto_approved=True)
        return True

    def reject_next_step(self, workflow_id: str, broadcast: Optional[BroadcastFn] = None) -> bool:
        """'reject' — the step currently waiting on permission is
        explicitly SKIPPED (never executed) and the workflow continues
        from the next step, rather than being left stuck forever."""
        workflow = self._workflows.get(workflow_id)
        if workflow is None or workflow.status != WorkflowStatus.WAITING_FOR_PERMISSION:
            return False
        if workflow.current_step < len(workflow.steps):
            step = workflow.steps[workflow.current_step]
            step.status = StepStatus.SKIPPED
            step.result = "Rejected by user — step not executed."
            self._audit(workflow, "workflow_step_rejected", step.description)
            workflow.current_step += 1
        workflow.status = WorkflowStatus.RUNNING
        self.start(workflow_id, broadcast=broadcast, auto_approved=False)
        return True

    # ── Suspend-and-wait-for-user-input (Feature 13) ────────────────
    async def provide_input(self, workflow_id: str, user_input: str, broadcast: Optional[BroadcastFn] = None) -> Workflow:
        """
        Resumes a workflow that's WAITING_FOR_USER with the user's
        actual answer, and — unlike approve/reject above, which
        fire-and-forget via start() because the remaining work can be
        long-running (installs, etc.) — awaits the resumed run inline,
        because a conversational answer/next-question exchange belongs
        in the same chat turn. Raises ValueError if the workflow isn't
        genuinely waiting, so a caller can't silently no-op.
        """
        workflow = self._workflows.get(workflow_id)
        if workflow is None:
            raise ValueError(f"No such workflow: {workflow_id}")
        if workflow.status != WorkflowStatus.WAITING_FOR_USER:
            raise ValueError("This workflow isn't currently waiting for user input.")
        workflow.last_user_input = user_input
        workflow.pending_input = None
        return await self.run(workflow_id, broadcast=broadcast)

    # ── Execution ────────────────────────────────────────────────
    def start(self, workflow_id: str, broadcast: Optional[BroadcastFn] = None, auto_approved: bool = False) -> asyncio.Task:
        """
        Spawn the workflow's run loop as an independent asyncio Task
        so it never blocks the voice pipeline / event loop caller
        (Performance Requirements section). Safe to call again after
        WAITING_FOR_PERMISSION/PAUSED to continue from current_step.
        """
        existing = self._tasks.get(workflow_id)
        if existing is not None and not existing.done():
            return existing  # already running — don't double-launch
        task = asyncio.create_task(self._run_loop(workflow_id, broadcast, auto_approved))
        self._tasks[workflow_id] = task
        return task

    async def run(self, workflow_id: str, broadcast: Optional[BroadcastFn] = None, auto_approved: bool = False) -> Workflow:
        """Awaitable convenience wrapper — runs to completion/pause/wait in the caller's own task."""
        return await self._run_loop(workflow_id, broadcast, auto_approved)

    async def _run_loop(self, workflow_id: str, broadcast: Optional[BroadcastFn], auto_approved: bool) -> Workflow:
        workflow = self._workflows[workflow_id]
        if workflow.start_time is None:
            workflow.start_time = time.monotonic()
        workflow.status = WorkflowStatus.RUNNING
        workflow.stopped_reason = None

        try:
            while True:
                # ── Cancellation (checked between steps only) ──
                if workflow.cancel_requested:
                    workflow.status = WorkflowStatus.CANCELLED
                    workflow.stopped_reason = "Cancelled by user."
                    break

                # ── Pause ────────────────────────────────────
                if workflow.pause_requested:
                    workflow.status = WorkflowStatus.PAUSED
                    workflow.stopped_reason = "Paused by user."
                    await self._broadcast_progress(workflow, broadcast)
                    self._audit(workflow, "workflow_paused", "")
                    return workflow

                # ── Completion ───────────────────────────────
                if workflow.current_step >= len(workflow.steps):
                    # A workflow that finished its step list but left one or
                    # more steps FAILED must not report COMPLETED — that
                    # status must mean "goal reached", not just "ran out of
                    # steps" (Rule 7 — never claim unverified/false success).
                    if any(s.status == StepStatus.FAILED for s in workflow.steps):
                        workflow.status = WorkflowStatus.FAILED
                        workflow.stopped_reason = workflow.stopped_reason or (
                            "One or more steps failed; goal was not fully achieved."
                        )
                    else:
                        workflow.status = WorkflowStatus.COMPLETED
                    break

                # ── Safety limits (Feature 15) ─────────────────
                if workflow.current_step >= workflow.max_steps:
                    workflow.status = WorkflowStatus.FAILED
                    workflow.stopped_reason = f"Maximum step count ({workflow.max_steps}) reached."
                    break
                if workflow.tool_calls_made >= workflow.max_tool_calls:
                    workflow.status = WorkflowStatus.FAILED
                    workflow.stopped_reason = f"Maximum tool-call count ({workflow.max_tool_calls}) reached."
                    break
                elapsed = time.monotonic() - workflow.start_time
                if elapsed > workflow.timeout_seconds:
                    workflow.status = WorkflowStatus.TIMED_OUT
                    workflow.stopped_reason = f"Workflow timeout ({workflow.timeout_seconds}s) exceeded."
                    break

                step = workflow.steps[workflow.current_step]
                outcome = await self._execute_step(workflow, step, broadcast, auto_approved)

                # ── Error recovery: RECOVER → REPLAN (Feature 17) ──
                # Only a plain failure is eligible — WAITING_FOR_PERMISSION
                # and STOP_REPEATED already have their own, more specific
                # handling and must not be reinterpreted here.
                if outcome == "" and step.status == StepStatus.FAILED:
                    if self._recovery_allowed(workflow, step):
                        # A recovery retry can itself hit a permission gate
                        # or the repeated-action limit — _attempt_recovery
                        # returns whichever control signal the retry
                        # produced, so it's handled the same way below.
                        outcome = await self._attempt_recovery(workflow, step, broadcast, auto_approved)

                if outcome == "WAITING_FOR_PERMISSION":
                    return workflow  # caller resumes run()/start() after confirmation resolves
                if outcome == "WAITING_FOR_USER":
                    return workflow  # caller resumes via provide_input() once the user answers
                if outcome == "STOP_REPEATED":
                    workflow.status = WorkflowStatus.FAILED
                    workflow.stopped_reason = (
                        f"Repeated action detected: '{step.description}' failed "
                        f"{_REPEATED_ACTION_LIMIT} times in a row. Stopped safely."
                    )
                    break

                workflow.current_step += 1

        except asyncio.CancelledError:
            workflow.status = WorkflowStatus.CANCELLED
            workflow.stopped_reason = workflow.stopped_reason or "Cancelled."
        except Exception as e:  # noqa: BLE001 — Feature 17: never crash the loop silently
            logger.error(f"[workflow] {workflow.id} failed with unhandled error: {e}")
            workflow.status = WorkflowStatus.FAILED
            workflow.stopped_reason = f"Unhandled error: {e}"
            workflow.errors.append(str(e))

        workflow.end_time = time.monotonic()
        await self._broadcast_progress(workflow, broadcast)
        self._audit(workflow, "workflow_finished", f"status={workflow.status.value} reason={workflow.stopped_reason}")
        self._persist_workflow_memory(workflow)
        return workflow

    async def _execute_step(
        self,
        workflow: Workflow,
        step: WorkflowStep,
        broadcast: Optional[BroadcastFn],
        auto_approved: bool,
    ) -> str:
        """Runs one OBSERVE/ACT/VERIFY step. Returns a control signal:
        "" (normal), "WAITING_FOR_PERMISSION", or "STOP_REPEATED"."""
        # ── Autonomy levels (Feature 3) ─────────────────────────
        # MANUAL means the assistant plans and reports but does not
        # execute tool-backed actions on its own — those steps are left
        # for the user to run themselves. Handler steps (read-only,
        # already vetted by the workflow's own code) and purely
        # informational steps are unaffected.
        if step.tool_name and workflow.autonomy_level == AutonomyLevel.MANUAL:
            step.status = StepStatus.SKIPPED
            step.outcome = None
            step.result = (
                "Skipped — MANUAL autonomy level requires you to run "
                "tool-backed steps yourself."
            )
            step.started_at = time.monotonic()
            step.ended_at = step.started_at
            await self._broadcast_progress(workflow, broadcast, detail=step.description)
            self._audit(
                workflow, "workflow_step",
                json.dumps({"step": step.description, "status": step.status.value, "outcome": None}),
            )
            return ""

        step.status = StepStatus.RUNNING
        step.started_at = time.monotonic()
        await self._broadcast_progress(workflow, broadcast, detail=step.description)

        try:
            if step.tool_name:
                # Rule 3 — ALL tool execution goes through the existing
                # Tool Registry, which itself enforces permissions.py.
                # Structured Approval UX: a step-scoped pre-approval
                # (from an "approve N" chat command) is consumed here,
                # one step at a time — distinct from `auto_approved`,
                # which blanket-approves the whole run ("approve all").
                consumed_one_shot = False
                effective_auto_approved = auto_approved
                if not effective_auto_approved and workflow.approvals_remaining > 0:
                    effective_auto_approved = True
                    consumed_one_shot = True
                import tool_registry as _tr
                result = await _tr.tool_registry.run_tool(
                    step.tool_name, step.tool_args, broadcast=broadcast, auto_approved=effective_auto_approved,
                )
                if consumed_one_shot:
                    workflow.approvals_remaining = max(0, workflow.approvals_remaining - 1)
                workflow.tool_calls_made += 1

                if result["status"] == "pending_confirmation":
                    workflow.status = WorkflowStatus.WAITING_FOR_PERMISSION
                    workflow.permissions_requested.append(result["confirmation_id"])
                    step.status = StepStatus.PENDING  # not yet run — retried once approved
                    step.started_at = None
                    self._audit(workflow, "workflow_waiting_for_permission", result["confirmation_id"])
                    return "WAITING_FOR_PERMISSION"

                if result["status"] in ("blocked", "error"):
                    step.status = StepStatus.FAILED
                    step.outcome = StepOutcome.ATTEMPTED
                    step.error = result["message"]
                    workflow.errors.append(f"{step.description}: {result['message']}")
                else:
                    step.status = StepStatus.DONE
                    step.outcome = StepOutcome.ATTEMPTED  # tool ran; caller must still verify via a later step
                    step.result = str(result.get("result"))[:2000]
            elif step.handler_key:
                handler = self._kinds[workflow.kind].handlers.get(step.handler_key)
                if handler is None:
                    step.status = StepStatus.FAILED
                    step.error = f"No handler registered for '{step.handler_key}'."
                else:
                    step_result: StepResult = await asyncio.wait_for(
                        handler(workflow, step), timeout=DEFAULT_PER_STEP_TIMEOUT_SECONDS,
                    )
                    if step_result.awaiting_input:
                        # Feature 13 — the suspend-and-wait-for-user-
                        # answer primitive. The step is NOT marked done:
                        # it stays PENDING so the *same* handler runs
                        # again once provide_input() supplies
                        # workflow.last_user_input, the same shape
                        # WAITING_FOR_PERMISSION already uses for a
                        # step that isn't finished yet, just paused.
                        workflow.status = WorkflowStatus.WAITING_FOR_USER
                        workflow.pending_input = {"step": step.description, "prompt": step_result.awaiting_input}
                        step.status = StepStatus.PENDING
                        step.started_at = None
                        step.result = step_result.summary or step.result
                        self._audit(workflow, "workflow_waiting_for_user", step.description)
                        return "WAITING_FOR_USER"
                    step.result = step_result.summary
                    step.evidence = step_result.evidence
                    step.error = step_result.error
                    if step_result.evidence:
                        workflow.evidence.append(step_result.evidence)
                    if step_result.error:
                        step.status = StepStatus.FAILED
                        step.outcome = StepOutcome.VERIFIED_FAILURE if step_result.verified_success is False else StepOutcome.ATTEMPTED
                        workflow.errors.append(f"{step.description}: {step_result.error}")
                    else:
                        step.status = StepStatus.DONE
                        step.outcome = (
                            StepOutcome.VERIFIED_SUCCESS if step_result.verified_success is True
                            else StepOutcome.VERIFIED_FAILURE if step_result.verified_success is False
                            else StepOutcome.ATTEMPTED
                        )
            else:
                # Informational step (e.g. "report findings") — nothing to execute.
                step.status = StepStatus.DONE
                step.outcome = StepOutcome.ATTEMPTED
        except asyncio.TimeoutError:
            step.status = StepStatus.FAILED
            step.error = f"Step timed out after {DEFAULT_PER_STEP_TIMEOUT_SECONDS}s."
            workflow.errors.append(step.error)
        except Exception as e:  # noqa: BLE001
            step.status = StepStatus.FAILED
            step.error = str(e)
            workflow.errors.append(f"{step.description}: {e}")

        step.ended_at = time.monotonic()
        self._audit(
            workflow, "workflow_step",
            json.dumps({"step": step.description, "status": step.status.value, "outcome": step.outcome.value if step.outcome else None}),
        )

        # ── Repeated-action detection (Feature 15) ──────────────
        key = step.action_key()
        if step.status == StepStatus.FAILED:
            workflow._consecutive_failures[key] = workflow._consecutive_failures.get(key, 0) + 1
            if workflow._consecutive_failures[key] >= _REPEATED_ACTION_LIMIT:
                return "STOP_REPEATED"
        else:
            workflow._consecutive_failures[key] = 0

        return ""

    # ── Error recovery: RECOVER → REPLAN (Feature 17) ───────────
    def _recovery_allowed(self, workflow: Workflow, step: WorkflowStep) -> bool:
        """Whether the engine should attempt one auto replan/retry for a
        step that just failed. Genuinely bounded — never a blind/infinite
        retry — and respectful of every existing safety and autonomy
        control rather than a parallel set of rules:
          - MANUAL autonomy never auto-recovers; the user is already in
            full control of tool-backed steps at that level.
          - at most _MAX_RECOVERY_ATTEMPTS per distinct action_key.
          - never pushes past the step/tool-call/timeout ceilings that
            would already stop a fresh step from running.
        """
        if workflow.autonomy_level == AutonomyLevel.MANUAL:
            return False
        key = step.action_key()
        if workflow.recovery_attempts.get(key, 0) >= _MAX_RECOVERY_ATTEMPTS:
            return False
        if workflow.current_step >= workflow.max_steps:
            return False
        if workflow.tool_calls_made >= workflow.max_tool_calls:
            return False
        if workflow.start_time is not None:
            elapsed = time.monotonic() - workflow.start_time
            if elapsed > workflow.timeout_seconds:
                return False
        return True

    async def _attempt_recovery(
        self,
        workflow: Workflow,
        step: WorkflowStep,
        broadcast: Optional[BroadcastFn],
        auto_approved: bool,
    ) -> str:
        """
        REPLAN + retry a single failed step, once. This is genuine
        recovery, not theater:
          - it does NOT mark the step successful — it re-runs the exact
            same OBSERVE/ACT/VERIFY path used for a fresh step, so the
            result is judged on its own merits;
          - it does NOT retry forever — capped by _recovery_allowed()
            and layered on top of (not instead of) repeated-action
            detection, so a step that keeps failing still trips
            STOP_REPEATED;
          - a permission gate hit during the retry is honoured exactly
            like a first attempt (returns WAITING_FOR_PERMISSION rather
            than being swallowed).
        Returns the same control-signal vocabulary as _execute_step().
        """
        key = step.action_key()
        workflow.recovery_attempts[key] = workflow.recovery_attempts.get(key, 0) + 1
        workflow.status = WorkflowStatus.RECOVERING
        await self._broadcast_progress(workflow, broadcast, detail=f"Recovering: {step.description}")
        self._audit(
            workflow, "workflow_recovery_attempt",
            json.dumps({"step": step.description, "attempt": workflow.recovery_attempts[key]}),
        )

        # Reset the step so the retry is a clean OBSERVE/ACT/VERIFY pass,
        # not a continuation of the failed one.
        step.error = None
        step.result = None
        step.outcome = None

        signal = await self._execute_step(workflow, step, broadcast, auto_approved)

        if step.status == StepStatus.DONE:
            self._audit(workflow, "workflow_recovery_success", step.description)
        elif signal not in ("WAITING_FOR_PERMISSION",):
            self._audit(
                workflow, "workflow_recovery_failed",
                f"{step.description}: {step.error or 'still failing after replan/retry'}",
            )

        # WAITING_FOR_PERMISSION during recovery must stay WAITING_FOR_PERMISSION
        # for the caller; any other case resumes normal RUNNING bookkeeping.
        if signal != "WAITING_FOR_PERMISSION":
            workflow.status = WorkflowStatus.RUNNING
        return signal

    # ── Progress / audit / memory (Features 12, 16, 8) ──────────
    async def _broadcast_progress(self, workflow: Workflow, broadcast: Optional[BroadcastFn], detail: Optional[str] = None) -> None:
        try:
            import state as _state_mod
            await _state_mod.state_manager.set_state(
                _state_mod.JarvisState.EXECUTING,
                broadcast,
                detail=f"{workflow.goal}: step {workflow.current_step + 1}/{len(workflow.steps)}" + (f" — {detail}" if detail else ""),
            )
        except Exception as e:
            logger.warning(f"[workflow] Progress broadcast failed (continuing): {e}")
        if broadcast is not None:
            try:
                await broadcast(workflow.to_dict())
            except Exception as e:
                logger.warning(f"[workflow] Progress event broadcast failed (continuing): {e}")

    def _audit(self, workflow: Workflow, event_type: str, message: str) -> None:
        """Feature 16 — reuses memory.py's existing system_events log; no new logging system."""
        try:
            import memory as _memory
            _memory.log_event(f"workflow:{event_type}", f"[{workflow.id[:8]}] {message}"[:2000])
        except Exception as e:
            logger.warning(f"[workflow] Audit log write failed (continuing): {e}")

    def _persist_workflow_memory(self, workflow: Workflow) -> None:
        """
        Feature 8 — reuses project_memory.py's existing facts table
        (kind='previous_fix' on success, 'known_issue' when the
        workflow didn't reach a verified resolution) rather than a
        second workflow database. Only stored when a project is known
        and there's evidence worth keeping — an empty/no-op workflow
        isn't worth a row.
        """
        if not workflow.project_path or not workflow.evidence:
            return
        try:
            import project_memory as _pm
            _pm.init_project_memory_db()
            project = _pm.upsert_project(workflow.project_path, name=workflow.project_path.rstrip("/\\").split("/")[-1])
            resolved = workflow.status == WorkflowStatus.COMPLETED and not workflow.errors
            payload = json.dumps({
                "goal": workflow.goal,
                "evidence": workflow.evidence[-5:],
                "outcome": workflow.status.value,
                "errors": workflow.errors[-5:],
            })
            _pm.save_fact(
                project["path"] if isinstance(project, dict) else workflow.project_path,
                kind="previous_fix" if resolved else "known_issue",
                content=payload,
            )
        except Exception as e:
            logger.warning(f"[workflow] Workflow-memory persist failed (continuing): {e}")


# Single shared instance, matching permission_manager/tool_registry/state_manager.
workflow_engine = WorkflowEngine()
