"""
JARVIS task planner (Phase 5, Section 4).

Lightweight, structured multi-step planning — deliberately NOT an
"agent that improvises shell commands." Plans are built from a small
set of named planners (one per known goal shape: exam prep, hackathon
environment prep, generic study/task breakdown). Each TaskStep names a
concrete tool (if any) rather than free text, so execution — when the
user approves a plan — goes through the existing tool_registry +
permission_manager exactly like any other tool call. This module never
executes anything itself.

Section 4's example objects are implemented literally:

    TaskPlan(goal="Prepare coding environment", steps=[...], requires_confirmation=True)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class StepStatus(str, Enum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class TaskStep:
    description: str
    # Name of a tool_registry tool this step would run, if any. None
    # means the step is informational/LLM-only (e.g. "ask what subject
    # if unknown") and never reaches the permission system at all.
    tool_name: Optional[str] = None
    tool_args: dict = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    result: Optional[str] = None


@dataclass
class TaskPlan:
    goal: str
    steps: list[TaskStep]
    # Section 4: "The planner must NOT execute dangerous or
    # irreversible actions without permission." Any plan containing a
    # step that maps to a non-SAFE tool must have this set True — see
    # _plan_requires_confirmation() below, which derives this rather
    # than trusting a caller-supplied flag, so a planner author can't
    # forget to set it.
    requires_confirmation: bool = True
    notes: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [f"PLAN: {self.goal}", ""]
        for i, step in enumerate(self.steps, start=1):
            marker = {
                StepStatus.PENDING: " ",
                StepStatus.DONE: "x",
                StepStatus.FAILED: "!",
                StepStatus.SKIPPED: "-",
            }[step.status]
            lines.append(f"  [{marker}] {i}. {step.description}")
            if step.result:
                lines.append(f"        -> {step.result}")
        if self.notes:
            lines.append("")
            lines.extend(f"Note: {n}" for n in self.notes)
        return "\n".join(lines)


# Tools that are known-SAFE and read-only enough that a plan made
# entirely of them doesn't need up-front confirmation of the whole
# plan (individual CONFIRM/BLOCKED steps still go through
# permission_manager exactly as normal when the step actually runs —
# this only affects whether the *plan itself* is presented as
# "I'll do this" vs "here's what I'd do, shall I proceed?").
_SAFE_PLANNING_TOOLS = {
    None, "inspect_environment", "read_terminal_output", "analyze_code",
    "gather_context", "get_weather", "search_web",
}


def _plan_requires_confirmation(steps: list[TaskStep]) -> bool:
    return any(step.tool_name not in _SAFE_PLANNING_TOOLS for step in steps)


def plan_hackathon_environment(project_name: Optional[str] = None) -> TaskPlan:
    """Section 4's literal example: 'Prepare my coding environment for the hackathon.'"""
    target = project_name or "the current project"
    steps = [
        TaskStep(f"Identify current project ({target})", tool_name="gather_context"),
        TaskStep("Check required files are present", tool_name="inspect_environment"),
        TaskStep("Check Python/Node environment is available", tool_name="inspect_environment"),
        TaskStep("Check Git status for uncommitted changes", tool_name="run_terminal_command",
                  tool_args={"command": "git status"}),
        TaskStep("Check dependencies are installed", tool_name="run_tests"),
        TaskStep("Report any problems found"),
    ]
    return TaskPlan(
        goal=f"Prepare coding environment for the hackathon ({target})",
        steps=steps,
        requires_confirmation=_plan_requires_confirmation(steps),
    )


def plan_exam_prep(subject: Optional[str] = None) -> TaskPlan:
    """Section 4's other literal example: 'Help me prepare for tomorrow's exam.'"""
    steps: list[TaskStep] = []
    if not subject:
        steps.append(TaskStep("Ask the user which subject the exam covers"))
    steps += [
        TaskStep(f"Check available study material for {subject or 'the subject'}"),
        TaskStep("Generate a study plan"),
        TaskStep("Create a revision timetable"),
        TaskStep("Offer revision questions / a quiz"),
    ]
    return TaskPlan(
        goal=f"Prepare for exam{f' ({subject})' if subject else ''}",
        steps=steps,
        requires_confirmation=_plan_requires_confirmation(steps),
    )


def plan_debug_investigation(target_file: Optional[str] = None) -> TaskPlan:
    """
    Mirrors debug_mode.py's own step sequence as a TaskPlan so it can
    be previewed/described the same way other plans are, without
    duplicating Investigation's actual execution logic — this planner
    is descriptive only; debug_mode.Investigation remains the single
    authoritative implementation that actually runs these steps.
    """
    steps = [
        TaskStep("Gather context", tool_name="gather_context"),
        TaskStep(f"Identify target file{f' ({target_file})' if target_file else ''}"),
        TaskStep("Check terminal output for a live error", tool_name="read_terminal_output"),
        TaskStep("Analyze code", tool_name="analyze_code",
                  tool_args={"file_path": target_file} if target_file else {}),
        TaskStep("Check project memory for known issues"),
        TaskStep("Form diagnosis"),
    ]
    return TaskPlan(
        goal=f"Debug{f' {target_file}' if target_file else ''}",
        steps=steps,
        requires_confirmation=_plan_requires_confirmation(steps),
    )


# Registry of named planners, keyed by a short id used by
# create_plan(). Each planner is a plain callable(**kwargs) -> TaskPlan
# so new goal shapes can be added without touching create_plan().
_PLANNERS: dict[str, Callable[..., TaskPlan]] = {
    "hackathon_environment": plan_hackathon_environment,
    "exam_prep": plan_exam_prep,
    "debug_investigation": plan_debug_investigation,
}


def create_plan(kind: str, **kwargs) -> TaskPlan:
    """
    Look up a named planner and build a TaskPlan. Raises ValueError for
    an unknown kind rather than silently returning an empty plan —
    callers (main.py) should catch this and fall back to a plain LLM
    response instead of pretending a plan exists.
    """
    planner = _PLANNERS.get(kind)
    if planner is None:
        raise ValueError(f"No planner registered for '{kind}'. Known: {sorted(_PLANNERS)}")
    return planner(**kwargs)


def available_plans() -> list[str]:
    return sorted(_PLANNERS)


def advance_step(plan: TaskPlan, index: int, status: StepStatus, result: Optional[str] = None) -> TaskPlan:
    """Mutates and returns the plan — used by a caller driving execution step by step."""
    if index < 0 or index >= len(plan.steps):
        raise IndexError(f"Step index {index} out of range for plan with {len(plan.steps)} steps.")
    plan.steps[index].status = status
    plan.steps[index].result = result
    return plan
