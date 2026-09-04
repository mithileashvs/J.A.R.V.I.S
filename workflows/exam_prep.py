"""
Phase 6 Feature 13 — CSE Exam Prep workflow.

Wires an actual `exam_prep` workflow kind into the workflow engine (see
workflows/project_review.py for the reference implementation this
follows). Two LLM-backed steps — build a revision plan, generate
practice questions — plus one purely informational step that compiles
both into a single report, exactly the "generation" pieces the spec
calls for.

Deliberately NOT covered by this workflow (see main.py's PLANNING
routing comment and PHASE6_IMPLEMENTATION_REPORT.md for the full
rationale): the interactive teach -> quiz -> evaluate -> adjust-
difficulty loop. That loop needs to pause mid-workflow, hand control
back to the user, read their actual answer, and resume — a suspend-
and-wait-for-user-answer primitive the engine does not have (it can
already wait for a tool permission via WAITING_FOR_PERMISSION, but
that is a yes/no gate on a step the engine itself is about to run, not
a way to receive freeform user input mid-step and hand it to a
handler). Forcing that loop through this engine today would mean
faking the suspension with something like a step that blocks or
polls, which is exactly the kind of fake implementation Rule 4
prohibits. STUDY intent's existing teach_prompt/quiz_prompt/
explain_wrong_answer_prompt path in assistants/study_assistant.py
already does this correctly outside the engine and is left untouched.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from workflow_engine import StepResult, Workflow, WorkflowKindSpec, WorkflowStep, workflow_engine

# core/llm_orchestrator.run() never raises — on a failed call it returns
# this apology text (see core/llm_orchestrator.py) rather than an
# exception, so that's the boundary this workflow checks to tell a
# real LLM failure apart from a normal generated answer.
_LLM_FAILURE_MARKER = "I ran into trouble reaching my language model"


def _build_steps(subject: Optional[str] = None, **_kwargs) -> list[WorkflowStep]:
    label = subject or "the exam"
    return [
        WorkflowStep(
            f"Build a revision plan for {label}",
            handler_key="build_revision_plan",
            tool_args={"subject": subject},
        ),
        WorkflowStep(
            f"Generate practice questions for {label}",
            handler_key="generate_practice_questions",
            tool_args={"subject": subject},
        ),
        WorkflowStep("Compile exam prep report", handler_key="compile_report"),
    ]


async def _run_llm(system_prompt: str, user_request: str) -> str:
    from core import llm_orchestrator
    return await asyncio.to_thread(llm_orchestrator.run, system_prompt, user_request, [])


async def _build_revision_plan(workflow: Workflow, step: WorkflowStep) -> StepResult:
    from assistants import study_assistant

    subject = step.tool_args.get("subject") or "the exam"
    prompt = study_assistant.revision_plan_prompt(subject)
    text = await _run_llm(prompt, workflow.user_request)

    data = workflow.__dict__.setdefault("_exam_data", {})
    if _LLM_FAILURE_MARKER in text:
        return StepResult(
            summary=text, verified_success=False,
            error="Could not reach the language model to build the revision plan.",
        )
    data["revision_plan"] = text
    return StepResult(
        summary=text, verified_success=None,
        evidence=f"Revision plan generated for '{subject}'.",
    )


async def _generate_practice_questions(workflow: Workflow, step: WorkflowStep) -> StepResult:
    from assistants import study_assistant

    subject = step.tool_args.get("subject") or "the exam"
    prompt = study_assistant.practice_questions_prompt(subject)
    text = await _run_llm(prompt, workflow.user_request)

    data = workflow.__dict__.setdefault("_exam_data", {})
    if _LLM_FAILURE_MARKER in text:
        return StepResult(
            summary=text, verified_success=False,
            error="Could not reach the language model to generate practice questions.",
        )
    data["practice_questions"] = text
    return StepResult(
        summary=text, verified_success=None,
        evidence=f"Practice questions generated for '{subject}'.",
    )


async def _compile_report(workflow: Workflow, step: WorkflowStep) -> StepResult:
    """
    Purely informational — no LLM call, no tool call. Just assembles
    whatever the two steps above actually produced (Rule 7: only ever
    report what really happened; a step that failed above is reported
    as missing here, not papered over).
    """
    data = workflow.__dict__.get("_exam_data", {})
    lines = ["EXAM PREP", ""]
    lines.append("Revision plan:")
    lines.append(data.get("revision_plan") or "(not generated — see earlier step's error)")
    lines.append("")
    lines.append("Practice questions:")
    lines.append(data.get("practice_questions") or "(not generated — see earlier step's error)")
    text = "\n".join(lines)
    return StepResult(summary=text, verified_success=True, evidence="Exam prep report compiled.")


workflow_engine.register_kind(WorkflowKindSpec(
    name="exam_prep",
    build_steps=_build_steps,
    handlers={
        "build_revision_plan": _build_revision_plan,
        "generate_practice_questions": _generate_practice_questions,
        "compile_report": _compile_report,
    },
))
