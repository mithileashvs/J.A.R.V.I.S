"""
Phase 6 Feature 5 — Development Environment Agent.

"Jarvis, prepare this Python project." Detection and inspection steps
call read-only, already-SAFE checks (`pip list`/`npm list` are in
terminal_tools.py's `_SAFE_COMMANDS`, so they run without confirmation,
same as `git status`). The one step that could actually change the
environment — installing missing dependencies — is deliberately left
with no tool_name until (and unless) a prior step determines something
is actually missing; when it does, that step's tool_name/tool_args are
set to a CONFIRM-level command (`pip install`/`npm install` are in
`_CONFIRM_COMMANDS`), which the engine then routes through
tool_registry.run_tool() exactly like every other tool call — the
workflow lands in WAITING_FOR_PERMISSION and stops there rather than
installing anything silently. This is the same
"conditionally set the NEXT step's tool_name from an earlier step's
handler" technique as generate_report-style late binding; nothing here
bypasses or duplicates the Permission Manager.
"""

from __future__ import annotations

import os

from workflow_engine import StepResult, Workflow, WorkflowKindSpec, WorkflowStep, workflow_engine

# language -> (runtime version check, installed-packages check, manifest filename(s), install command)
_LANGUAGE_PROFILES = {
    "Python": {
        "version_cmd": "python3 --version",
        "list_cmd": "pip list",
        "manifests": ("requirements.txt",),
        "install_cmd": "pip install -r requirements.txt",
        "venv_dirs": (".venv", "venv", "env"),
    },
    "Node.js": {
        "version_cmd": "node --version",
        "list_cmd": "npm list",
        "manifests": ("package.json",),
        "install_cmd": "npm install",
        "venv_dirs": ("node_modules",),
    },
}


def _build_steps(**_kwargs) -> list[WorkflowStep]:
    return [
        WorkflowStep("Detect project language and runtime", handler_key="detect_language"),
        WorkflowStep("Check for a virtual environment / local install dir", handler_key="check_venv"),
        WorkflowStep("Inspect dependency manifest", handler_key="inspect_manifest"),
        WorkflowStep("Check installed dependencies", handler_key="check_installed"),
        WorkflowStep("Generate setup plan", handler_key="generate_plan"),
        # Deliberately no tool_name/handler_key yet — set dynamically
        # by generate_plan's handler only if something actually needs
        # installing. If nothing does, this step stays purely
        # informational (engine treats no tool_name/handler_key as a
        # no-op DONE step) — "do not blindly install anything."
        WorkflowStep("Install missing dependencies (requires your permission)"),
    ]


def _data(workflow: Workflow) -> dict:
    return workflow.__dict__.setdefault("_env_data", {})


async def _detect_language(workflow: Workflow, step: WorkflowStep) -> StepResult:
    import project_detector
    if not workflow.project_path or not os.path.isdir(workflow.project_path):
        return StepResult(
            summary="No valid project path was provided.", verified_success=False,
            error=f"'{workflow.project_path}' is not a directory.",
        )
    summary = project_detector.detect_project(workflow.project_path)
    language = next((t for t in summary.technologies if t in _LANGUAGE_PROFILES), None)
    _data(workflow)["project_summary"] = summary
    _data(workflow)["language"] = language
    if language is None:
        text = f"No supported language detected ({', '.join(summary.technologies) or 'nothing recognized'})."
        return StepResult(summary=text, verified_success=True, evidence=text)

    profile = _LANGUAGE_PROFILES[language]
    import tool_registry as _tr
    outcome = await _tr.tool_registry.run_tool(
        "run_terminal_command", {"command": profile["version_cmd"], "cwd": workflow.project_path},
        auto_approved=True,
    )
    workflow.tool_calls_made += 1
    version_text = "unknown"
    if outcome["status"] == "ok":
        version_text = (outcome["result"].get("stdout") or "").strip() or "unknown"
    text = f"Detected {language}. Runtime: {version_text}"
    return StepResult(summary=text, verified_success=True, evidence=text)


async def _check_venv(workflow: Workflow, step: WorkflowStep) -> StepResult:
    language = _data(workflow).get("language")
    if language is None:
        return StepResult(summary="No language detected — skipped.", verified_success=None)
    profile = _LANGUAGE_PROFILES[language]
    found = [d for d in profile["venv_dirs"] if os.path.isdir(os.path.join(workflow.project_path, d))]
    _data(workflow)["venv_found"] = bool(found)
    text = f"Found: {', '.join(found)}." if found else "No local virtual environment / install directory found."
    return StepResult(summary=text, verified_success=True, evidence=text)


async def _inspect_manifest(workflow: Workflow, step: WorkflowStep) -> StepResult:
    language = _data(workflow).get("language")
    if language is None:
        return StepResult(summary="No language detected — skipped.", verified_success=None)
    profile = _LANGUAGE_PROFILES[language]
    manifest_path = None
    for name in profile["manifests"]:
        candidate = os.path.join(workflow.project_path, name)
        if os.path.isfile(candidate):
            manifest_path = candidate
            break
    if manifest_path is None:
        text = f"No {'/'.join(profile['manifests'])} found at the project root."
        _data(workflow)["required_packages"] = []
        return StepResult(summary=text, verified_success=True, evidence=text)

    required: list[str] = []
    try:
        if language == "Python":
            with open(manifest_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    pkg = line.split("==")[0].split(">=")[0].split("<=")[0].split("[")[0].strip()
                    if pkg:
                        required.append(pkg.lower())
        elif language == "Node.js":
            import json
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            required = sorted(set(data.get("dependencies", {})) | set(data.get("devDependencies", {})))
    except (OSError, ValueError) as e:
        return StepResult(summary=f"Could not read {manifest_path}.", verified_success=False, error=str(e))

    _data(workflow)["required_packages"] = required
    text = f"{len(required)} declared package(s) in {os.path.basename(manifest_path)}."
    return StepResult(summary=text, verified_success=True, evidence=text)


async def _check_installed(workflow: Workflow, step: WorkflowStep) -> StepResult:
    language = _data(workflow).get("language")
    required = _data(workflow).get("required_packages", [])
    if language is None or not required:
        _data(workflow)["missing_packages"] = []
        return StepResult(summary="Nothing declared to check against — skipped.", verified_success=None)

    profile = _LANGUAGE_PROFILES[language]
    import tool_registry as _tr
    outcome = await _tr.tool_registry.run_tool(
        "run_terminal_command", {"command": profile["list_cmd"], "cwd": workflow.project_path},
        auto_approved=True,
    )
    workflow.tool_calls_made += 1
    if outcome["status"] != "ok":
        return StepResult(
            summary="Could not check installed dependencies.", verified_success=False,
            error=outcome.get("message", "unknown error"),
        )
    installed_text = (outcome["result"].get("stdout") or "").lower()
    missing = [pkg for pkg in required if pkg.lower() not in installed_text]
    _data(workflow)["missing_packages"] = missing
    text = (
        f"{len(missing)} of {len(required)} declared package(s) appear missing: {', '.join(missing)}."
        if missing else f"All {len(required)} declared package(s) appear installed."
    )
    return StepResult(summary=text, verified_success=True, evidence=text)


async def _generate_plan(workflow: Workflow, step: WorkflowStep) -> StepResult:
    data = _data(workflow)
    language = data.get("language")
    missing = data.get("missing_packages", [])
    proposed: list[str] = []
    if not data.get("venv_found") and language is not None:
        proposed.append(f"Create a virtual environment / local install directory for {language}.")
    if missing:
        proposed.append(f"Install {len(missing)} missing package(s): {', '.join(missing)}.")

    # Late-bind the final "install" step — only if there's actually
    # something to install (Rule 9 — no silent destructive actions,
    # and Rule 6 — never invent a change that isn't backed by evidence
    # from the steps above).
    install_step = workflow.steps[-1]
    if missing and language is not None:
        profile = _LANGUAGE_PROFILES[language]
        install_step.tool_name = "run_terminal_command"
        install_step.tool_args = {"command": profile["install_cmd"], "cwd": workflow.project_path}

    lines = ["PROPOSED CHANGES", ""]
    if proposed:
        lines.extend(f"{i+1}. {p}" for i, p in enumerate(proposed))
    else:
        lines.append("None — the environment already looks ready.")
    lines.append("")
    lines.append(f"Permission required: {'YES' if missing else 'NO'}")
    text = "\n".join(lines)
    return StepResult(summary=text, verified_success=True, evidence=text)


workflow_engine.register_kind(WorkflowKindSpec(
    name="dev_env_prep",
    build_steps=_build_steps,
    handlers={
        "detect_language": _detect_language,
        "check_venv": _check_venv,
        "inspect_manifest": _inspect_manifest,
        "check_installed": _check_installed,
        "generate_plan": _generate_plan,
    },
))
