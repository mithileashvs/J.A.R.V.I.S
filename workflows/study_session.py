"""
Phase 6 Feature 13 — guided study session workflow.

This is the piece the original exam_prep.py pass deliberately left
outside the engine: the interactive teach -> quiz -> evaluate ->
adjust-difficulty loop. It's possible now because workflow_engine.py
gained a real suspend-and-wait-for-user-answer primitive
(StepResult.awaiting_input / Workflow.pending_input /
Workflow.last_user_input / WorkflowEngine.provide_input()) — see that
module's docstrings for the mechanism. This workflow is its first (and
so far only) consumer.

Structure: teach once, then `rounds` repetitions of
(quiz -> evaluate), then a summary. Each quiz step suspends after
generating a question (StepResult.awaiting_input=the question text);
main.py's chat() routes the student's next raw message back in via
provide_input(), which resumes the SAME step — the handler tells a
fresh call from a resumed one by checking Workflow.last_user_input.
The following evaluate step grades the answer with a real LLM call and
sets whether the *next* round's question should be harder or not —
the "adjust difficulty" part of the loop — genuinely, not simulated.

This does NOT replace assistants/study_assistant.py's existing
per-message STUDY path ("teach me X", "quiz me on X", "quiz me
harder", flashcards, viva, revision plan) — those are still answered
exactly as before. This workflow only starts on an explicit, distinct
"study session" request; see main.py's STUDY routing comment.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from workflow_engine import StepResult, Workflow, WorkflowKindSpec, WorkflowStep, workflow_engine

_LLM_FAILURE_MARKER = "I ran into trouble reaching my language model"


def _build_steps(topic: Optional[str] = None, rounds: int = 3, **_kwargs) -> list[WorkflowStep]:
    label = topic or "the topic"
    rounds = max(1, min(int(rounds), 10))  # sane bounds — this also caps how long a session can run
    steps = [WorkflowStep(f"Teach {label}", handler_key="teach", tool_args={"topic": label, "level": "BEGINNER"})]
    for i in range(1, rounds + 1):
        steps.append(WorkflowStep(f"Quiz round {i}", handler_key="quiz", tool_args={"topic": label, "round": i}))
        steps.append(WorkflowStep(f"Evaluate round {i} answer", handler_key="evaluate", tool_args={"topic": label, "round": i}))
    steps.append(WorkflowStep("Summarize study session", handler_key="summarize", tool_args={}))
    return steps


async def _run_llm(system_prompt: str, user_request: str) -> str:
    from core import llm_orchestrator
    return await asyncio.to_thread(llm_orchestrator.run, system_prompt, user_request, [])


def _data(workflow: Workflow) -> dict:
    return workflow.__dict__.setdefault("_study_data", {"qa": [], "results": [], "level": "BEGINNER", "harder": False})


async def _teach(workflow: Workflow, step: WorkflowStep) -> StepResult:
    from assistants import study_assistant
    topic = step.tool_args["topic"]
    level = step.tool_args.get("level", "BEGINNER")
    prompt = study_assistant.teach_prompt(topic, level=level)
    text = await _run_llm(prompt, workflow.user_request)
    if _LLM_FAILURE_MARKER in text:
        return StepResult(summary=text, verified_success=False, error="Could not reach the language model for the lesson.")
    _data(workflow)["level"] = level
    return StepResult(summary=text, verified_success=None, evidence=f"Taught {topic} at {level} level.")


async def _quiz(workflow: Workflow, step: WorkflowStep) -> StepResult:
    """First call: generates a question and suspends for the student's
    answer. Resumed call (workflow.last_user_input set by
    provide_input()): records the answer and finishes the step."""
    from assistants import study_assistant
    data = _data(workflow)
    topic = step.tool_args["topic"]
    round_idx = step.tool_args["round"]

    if workflow.last_user_input is not None:
        answer = workflow.last_user_input
        workflow.last_user_input = None  # consumed — never leak into a later round
        question = data.get("_pending_question", "")
        data["qa"].append({"round": round_idx, "question": question, "answer": answer})
        return StepResult(
            summary=question, verified_success=True,
            evidence=f"Round {round_idx}: question asked and answered.",
        )

    prompt = study_assistant.quiz_prompt(topic, level=data.get("level", "BEGINNER"), harder=data.get("harder", False))
    question = await _run_llm(prompt, workflow.user_request)
    if _LLM_FAILURE_MARKER in question:
        return StepResult(summary=question, verified_success=False, error="Could not reach the language model to generate the quiz question.")
    data["_pending_question"] = question
    return StepResult(summary=question, awaiting_input=question)


async def _evaluate(workflow: Workflow, step: WorkflowStep) -> StepResult:
    from assistants import study_assistant
    data = _data(workflow)
    topic = step.tool_args["topic"]
    round_idx = step.tool_args["round"]
    qa = next((x for x in data["qa"] if x["round"] == round_idx), None)
    if qa is None:
        return StepResult(summary="", verified_success=False, error="No recorded answer for this round.")

    prompt = study_assistant.grade_and_explain_prompt(topic, qa["question"], qa["answer"])
    feedback = await _run_llm(prompt, workflow.user_request)
    if _LLM_FAILURE_MARKER in feedback:
        return StepResult(summary=feedback, verified_success=False, error="Could not reach the language model to grade the answer.")

    correct = feedback.strip().upper().startswith("CORRECT")
    data["harder"] = correct  # adjust-difficulty: next round is harder only after a correct answer
    data["results"].append({"round": round_idx, "correct": correct})
    return StepResult(
        summary=feedback, verified_success=True,
        evidence=f"Round {round_idx}: graded {'correct' if correct else 'incorrect'}.",
    )


async def _summarize(workflow: Workflow, step: WorkflowStep) -> StepResult:
    data = _data(workflow)
    results = data.get("results", [])
    correct_count = sum(1 for r in results if r["correct"])
    lines = ["STUDY SESSION SUMMARY", "", f"Score: {correct_count}/{len(results)} correct.", ""]
    for r in results:
        lines.append(f"Round {r['round']}: {'Correct' if r['correct'] else 'Incorrect'}")
    text = "\n".join(lines)
    return StepResult(summary=text, verified_success=True, evidence="Study session summary compiled.")


workflow_engine.register_kind(WorkflowKindSpec(
    name="study_session",
    build_steps=_build_steps,
    handlers={
        "teach": _teach,
        "quiz": _quiz,
        "evaluate": _evaluate,
        "summarize": _summarize,
    },
))
