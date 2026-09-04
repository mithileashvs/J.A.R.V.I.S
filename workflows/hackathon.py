"""
Phase 6 Feature 14 — Hackathon project workflow.

A genuine multi-step, engine-backed pipeline for "plan out a hackathon
project end to end" requests — distinct from the existing per-message
capability dispatch in assistants/hackathon_assistant.py (used for
one-off asks like "give me hackathon ideas" or "what tech stack should
I use", which stay exactly as they were; see main.py's HACKATHON
routing comment for how the two are told apart).

Six real LLM-backed steps, each feeding the previous step's actual
output forward as context (no user selection mid-workflow — like
exam_prep.py, anything needing a suspend-and-wait-for-user-input
primitive is out of scope; here the pipeline instead treats "whatever
the ideas step produced" as the working project description for every
step downstream), plus a report-compilation step. Mirrors
workflows/exam_prep.py's structure and error-handling: a failed LLM
call is reported as a real failure, never faked as success, and the
final report only includes what genuinely got generated.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from workflow_engine import StepResult, Workflow, WorkflowKindSpec, WorkflowStep, workflow_engine

# core/llm_orchestrator.run() never raises — on a failed call it returns
# this apology text rather than an exception, so that's the boundary
# this workflow checks to tell a real LLM failure apart from a normal
# generated answer (same convention as workflows/exam_prep.py).
_LLM_FAILURE_MARKER = "I ran into trouble reaching my language model"

_STEP_ORDER = [
    "ideas", "architecture", "tech_stack", "mvp", "task_breakdown", "pitch",
]
_STEP_LABELS = {
    "ideas": "Generate hackathon project ideas",
    "architecture": "Design a system architecture",
    "tech_stack": "Recommend a tech stack",
    "mvp": "Break the idea into an MVP",
    "task_breakdown": "Break work into team tasks",
    "pitch": "Draft a pitch",
}


def _build_steps(theme: Optional[str] = None, team_size: int = 3, **_kwargs) -> list[WorkflowStep]:
    return [
        WorkflowStep(
            _STEP_LABELS[key],
            handler_key=key,
            tool_args={"theme": theme, "team_size": team_size},
        )
        for key in _STEP_ORDER
    ] + [WorkflowStep("Compile hackathon project plan", handler_key="compile_report")]


async def _run_llm(system_prompt: str, user_request: str) -> str:
    from core import llm_orchestrator
    return await asyncio.to_thread(llm_orchestrator.run, system_prompt, user_request, [])


def _previous_output(workflow: Workflow, step_key: str) -> Optional[str]:
    """The most recently generated real output before `step_key`, used
    as the "project so far" context for the next step — feed-forward,
    not independent generation per step."""
    data = workflow.__dict__.get("_hackathon_data", {})
    idx = _STEP_ORDER.index(step_key)
    for key in reversed(_STEP_ORDER[:idx]):
        if data.get(key):
            return data[key]
    return None


async def _run_generation_step(workflow: Workflow, step: WorkflowStep, prompt: str, key: str) -> StepResult:
    text = await _run_llm(prompt, workflow.user_request)
    data = workflow.__dict__.setdefault("_hackathon_data", {})
    if _LLM_FAILURE_MARKER in text:
        return StepResult(
            summary=text, verified_success=False,
            error=f"Could not reach the language model for the {key.replace('_', ' ')} step.",
        )
    data[key] = text
    return StepResult(summary=text, verified_success=None, evidence=f"{_STEP_LABELS[key]}: generated.")


async def _ideas(workflow: Workflow, step: WorkflowStep) -> StepResult:
    from assistants import hackathon_assistant
    theme = step.tool_args.get("theme")
    prompt = hackathon_assistant.idea_generation_prompt(theme=theme)
    return await _run_generation_step(workflow, step, prompt, "ideas")


async def _architecture(workflow: Workflow, step: WorkflowStep) -> StepResult:
    from assistants import hackathon_assistant
    desc = _previous_output(workflow, "architecture") or workflow.user_request
    prompt = hackathon_assistant.architecture_prompt(desc)
    return await _run_generation_step(workflow, step, prompt, "architecture")


async def _tech_stack(workflow: Workflow, step: WorkflowStep) -> StepResult:
    from assistants import hackathon_assistant
    desc = _previous_output(workflow, "tech_stack") or workflow.user_request
    prompt = hackathon_assistant.tech_stack_prompt(desc)
    return await _run_generation_step(workflow, step, prompt, "tech_stack")


async def _mvp(workflow: Workflow, step: WorkflowStep) -> StepResult:
    from assistants import hackathon_assistant
    desc = _previous_output(workflow, "mvp") or workflow.user_request
    prompt = hackathon_assistant.mvp_breakdown_prompt(desc)
    return await _run_generation_step(workflow, step, prompt, "mvp")


async def _task_breakdown(workflow: Workflow, step: WorkflowStep) -> StepResult:
    from assistants import hackathon_assistant
    desc = _previous_output(workflow, "task_breakdown") or workflow.user_request
    team_size = step.tool_args.get("team_size") or 3
    prompt = hackathon_assistant.task_breakdown_prompt(desc, team_size)
    return await _run_generation_step(workflow, step, prompt, "task_breakdown")


async def _pitch(workflow: Workflow, step: WorkflowStep) -> StepResult:
    from assistants import hackathon_assistant
    desc = _previous_output(workflow, "pitch") or workflow.user_request
    prompt = hackathon_assistant.pitch_prompt(desc)
    return await _run_generation_step(workflow, step, prompt, "pitch")


async def _compile_report(workflow: Workflow, step: WorkflowStep) -> StepResult:
    """Purely informational — assembles whatever the steps above
    actually produced. A step that failed is reported as missing here,
    not papered over (Rule 7)."""
    data = workflow.__dict__.get("_hackathon_data", {})
    lines = ["HACKATHON PROJECT PLAN", ""]
    for key in _STEP_ORDER:
        lines.append(f"{_STEP_LABELS[key]}:")
        lines.append(data.get(key) or "(not generated — see earlier step's error)")
        lines.append("")
    text = "\n".join(lines).rstrip()
    return StepResult(summary=text, verified_success=True, evidence="Hackathon project plan compiled.")


workflow_engine.register_kind(WorkflowKindSpec(
    name="hackathon_project",
    build_steps=_build_steps,
    handlers={
        "ideas": _ideas,
        "architecture": _architecture,
        "tech_stack": _tech_stack,
        "mvp": _mvp,
        "task_breakdown": _task_breakdown,
        "pitch": _pitch,
        "compile_report": _compile_report,
    },
))
