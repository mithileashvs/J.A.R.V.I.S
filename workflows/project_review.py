"""
Phase 6 Feature 4 — Project Review workflow.

"Jarvis, review my project." Read-only by construction: every step
below either calls an existing read-only function directly
(project_detector.detect_project, code_analysis.analyze_file,
git_tools.git_status — none of these write anything) or, for the one
step that could conceivably need a shell command (dependency/test
detection), a SAFE-classified tool_registry entry. Nothing in this
workflow can reach a CONFIRM or BLOCKED tool, so it never needs
permission — matching the spec's "the review must be read-only by
default."

Every reported issue traces back to a concrete evidence string
recorded on the step that found it (Rule 6 — evidence first, never
invent a finding).
"""

from __future__ import annotations

import os

from workflow_engine import StepResult, Workflow, WorkflowKindSpec, WorkflowStep, workflow_engine

_ENTRY_POINT_NAMES = (
    "main.py", "app.py", "index.js", "index.ts", "server.py",
    "manage.py", "__main__.py",
)


def _build_steps(**_kwargs) -> list[WorkflowStep]:
    # Steps don't need project_path themselves — every handler reads it
    # from workflow.project_path (set once on the Workflow object by
    # workflow_engine.create_workflow()), the same way debug_mode's
    # Investigation reads context rather than threading a path through
    # each step.
    return [
        WorkflowStep("Detect project structure", handler_key="detect_structure"),
        WorkflowStep("Identify entry points", handler_key="find_entry_points"),
        WorkflowStep("Check dependency manifests", handler_key="check_dependencies"),
        WorkflowStep("Analyze entry-point code quality", handler_key="analyze_entry_points"),
        WorkflowStep("Check git status", handler_key="check_git_status"),
        WorkflowStep("Generate project health report", handler_key="generate_report"),
    ]


async def _detect_structure(workflow: Workflow, step: WorkflowStep) -> StepResult:
    import project_detector
    if not workflow.project_path or not os.path.isdir(workflow.project_path):
        return StepResult(
            summary="No valid project path was provided.",
            verified_success=False,
            error=f"'{workflow.project_path}' is not a directory.",
        )
    summary = project_detector.detect_project(workflow.project_path)
    text = (
        f"{summary.name}: {len(summary.technologies)} technolog{'y' if len(summary.technologies) == 1 else 'ies'} "
        f"detected ({', '.join(summary.technologies) or 'none recognized'}), "
        f"{len(summary.structure)} top-level area(s)."
    )
    step.result = text
    # Stash for later steps via a workflow-scoped attribute.
    workflow.__dict__.setdefault("_review_data", {})["project_summary"] = summary
    return StepResult(summary=text, verified_success=True, evidence=text)


async def _find_entry_points(workflow: Workflow, step: WorkflowStep) -> StepResult:
    data = workflow.__dict__.get("_review_data", {})
    summary = data.get("project_summary")
    found = []
    if summary is not None:
        for candidate in _ENTRY_POINT_NAMES:
            if candidate in summary.important_files or os.path.isfile(os.path.join(workflow.project_path, candidate)):
                found.append(candidate)
    data["entry_points"] = found
    if found:
        text = f"Entry point(s) found: {', '.join(found)}."
        return StepResult(summary=text, verified_success=True, evidence=text)
    text = "No recognizable entry-point file found at the project root."
    return StepResult(summary=text, verified_success=True, evidence=text)


async def _check_dependencies(workflow: Workflow, step: WorkflowStep) -> StepResult:
    data = workflow.__dict__.get("_review_data", {})
    summary = data.get("project_summary")
    manifests = summary.dependency_manifests if summary is not None else []
    if manifests:
        text = f"Dependency manifest(s) present: {', '.join(manifests)}."
        return StepResult(summary=text, verified_success=True, evidence=text)
    text = "No dependency manifest (requirements.txt, package.json, etc.) found at the project root."
    return StepResult(summary=text, verified_success=True, evidence=text)


async def _analyze_entry_points(workflow: Workflow, step: WorkflowStep) -> StepResult:
    import code_analysis
    data = workflow.__dict__.get("_review_data", {})
    entry_points = data.get("entry_points", [])
    if not entry_points:
        return StepResult(summary="No entry point to analyze — skipped.", verified_success=None)

    issues_total = 0
    per_file_notes = []
    for name in entry_points[:3]:  # bounded — a review isn't a full-repo lint pass
        path = os.path.join(workflow.project_path, name)
        try:
            result = code_analysis.analyze_file(path)
        except (FileNotFoundError, ValueError) as e:
            per_file_notes.append(f"{name}: could not analyze ({e})")
            continue
        issues_total += len(result.issues)
        if result.issues:
            per_file_notes.append(f"{name}: {len(result.issues)} issue(s) ({result.analysis_depth} analysis)")
        else:
            per_file_notes.append(f"{name}: no issues found ({result.analysis_depth} analysis)")
    data["entry_point_issue_count"] = issues_total
    text = "; ".join(per_file_notes) if per_file_notes else "No entry points analyzed."
    return StepResult(summary=text, verified_success=True, evidence=text)


async def _check_git_status(workflow: Workflow, step: WorkflowStep) -> StepResult:
    import git_tools
    result = await git_tools.git_status(cwd=workflow.project_path)
    data = workflow.__dict__.get("_review_data", {})
    data["git_result"] = result
    text = result.to_text()
    return StepResult(summary=text, verified_success=result.available, evidence=text)


async def _generate_report(workflow: Workflow, step: WorkflowStep) -> StepResult:
    """
    Builds the spec's exact PROJECT HEALTH REPORT shape from only what
    prior steps actually found — nothing here is invented (Rule 6).
    """
    data = workflow.__dict__.get("_review_data", {})
    critical: list[str] = []
    warnings: list[str] = []
    quick_wins: list[str] = []

    entry_points = data.get("entry_points", [])
    if not entry_points:
        critical.append("No recognizable entry point found at the project root.")

    if not data.get("project_summary") or not data["project_summary"].dependency_manifests:
        warnings.append("No dependency manifest found — dependencies may be undeclared.")

    issue_count = data.get("entry_point_issue_count", 0)
    if issue_count:
        warnings.append(f"Static analysis found {issue_count} issue(s) in entry-point file(s).")

    git_result = data.get("git_result")
    if git_result is not None and git_result.available and git_result.entries:
        quick_wins.append(f"{len(git_result.entries)} uncommitted change(s) — consider committing or stashing.")

    if critical:
        overall = "CRITICAL"
    elif warnings:
        overall = "WARNING"
    else:
        overall = "GOOD"

    lines = ["PROJECT HEALTH REPORT", "", f"Overall Status: {overall}", ""]
    lines.append("Critical Issues:")
    lines.extend(f"  {i+1}. {c}" for i, c in enumerate(critical)) if critical else lines.append("  None found.")
    lines.append("")
    lines.append("Warnings:")
    lines.extend(f"  {i+1}. {w}" for i, w in enumerate(warnings)) if warnings else lines.append("  None found.")
    lines.append("")
    lines.append("Quick Wins:")
    lines.extend(f"  {i+1}. {q}" for i, q in enumerate(quick_wins)) if quick_wins else lines.append("  None identified.")
    lines.append("")
    lines.append(
        "Suggested Next Action: "
        + (critical[0] if critical else (warnings[0] if warnings else "No action needed — project looks healthy."))
    )
    # Feature 8 — informed by what a *previous* review/dev-env-prep run
    # already found for this project, if anything (via
    # workflow_engine.py's create_workflow() -> _recall_prior_context()
    # -> project_memory.get_facts()). Only added when there's genuinely
    # something recalled; never a fabricated section.
    prior_summary = workflow.prior_context_summary()
    if prior_summary:
        lines.append("")
        lines.append("Recalled from previous runs:")
        lines.append(prior_summary)
    report_text = "\n".join(lines)
    return StepResult(summary=report_text, verified_success=True, evidence=report_text)


workflow_engine.register_kind(WorkflowKindSpec(
    name="project_review",
    build_steps=_build_steps,
    handlers={
        "detect_structure": _detect_structure,
        "find_entry_points": _find_entry_points,
        "check_dependencies": _check_dependencies,
        "analyze_entry_points": _analyze_entry_points,
        "check_git_status": _check_git_status,
        "generate_report": _generate_report,
    },
))
