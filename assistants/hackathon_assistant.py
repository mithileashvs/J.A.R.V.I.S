"""
JARVIS Hackathon Assistant (Phase 5, Section 8).

Pure prompt-construction module, same pattern as study_assistant.py's
prompt builders — kept modular per Section 8 ("Keep this modular so it
can later integrate with external services if needed") and Section 21
(no undocumented coupling to Phase 4's frontend). One function per
capability listed in Section 8, plus a light keyword dispatcher
(`dispatch`) so main.py doesn't need its own copy of this mapping.
"""

import re
from typing import Optional


def idea_generation_prompt(theme: Optional[str] = None, count: int = 5) -> str:
    theme_note = f" themed around '{theme}'" if theme else ""
    return (
        f"Generate {count} hackathon project ideas{theme_note}. For each: a one-line pitch, the core "
        "tech involved, and why it's feasible to build in a hackathon timeframe (24-48 hours)."
    )


def problem_statement_analysis_prompt(statement: str) -> str:
    return (
        "Analyze this hackathon problem statement. Identify: the core problem being solved, the target "
        "user, implicit constraints/judging criteria, and 2-3 possible solution angles.\n\n"
        f"Problem statement:\n{statement}"
    )


def architecture_prompt(project_description: str) -> str:
    return (
        "Design a system architecture for this hackathon project. Cover: main components, data flow "
        "between them, and the minimum viable tech needed for each — keep it buildable in a hackathon "
        f"timeframe, not a production system.\n\nProject:\n{project_description}"
    )


def tech_stack_prompt(project_description: str, constraints: Optional[str] = None) -> str:
    constraint_note = f" Constraints: {constraints}." if constraints else ""
    return (
        f"Recommend a tech stack for this hackathon project.{constraint_note} Prioritize tools the team "
        "can move fastest with, not the 'best' tool in the abstract. Justify each pick in one line.\n\n"
        f"Project:\n{project_description}"
    )


def mvp_breakdown_prompt(idea_description: str) -> str:
    return (
        "Break this idea into an MVP: the smallest set of features that demonstrates the core value, "
        "clearly separated from 'nice to have if time permits'.\n\n"
        f"Idea:\n{idea_description}"
    )


def task_breakdown_prompt(project_description: str, team_size: int) -> str:
    return (
        f"Break this project into parallel workstreams for {team_size} team members, minimizing "
        "blocking dependencies between people. For each member: their tasks and what they need from "
        f"the others before they can start.\n\nProject:\n{project_description}"
    )


def pitch_prompt(project_description: str, duration_minutes: int = 2) -> str:
    return (
        f"Create a {duration_minutes}-minute pitch script for this project: hook, problem, solution/demo "
        "beat, impact, and a memorable closing line.\n\n"
        f"Project:\n{project_description}"
    )


def demo_flow_prompt(project_description: str) -> str:
    return (
        "Create a live-demo flow for this project: the exact sequence of actions to show on screen, "
        "in the order that tells the best story and avoids fragile live-typing.\n\n"
        f"Project:\n{project_description}"
    )


def judging_prep_prompt(project_description: str) -> str:
    return (
        "Suggest how to improve this project's chances with judges: likely weak points a judge would "
        "probe, and one concrete improvement for each.\n\n"
        f"Project:\n{project_description}"
    )


def risk_assessment_prompt(project_description: str) -> str:
    return (
        "Identify the highest-risk part of this project — the piece most likely to fail, run out of "
        "time, or be hard to demo — and suggest a fallback for it.\n\n"
        f"Project:\n{project_description}"
    )


_DISPATCH_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"hackathon ideas?|ai hackathon ideas", re.IGNORECASE), "idea"),
    (re.compile(r"analyz.*problem statement", re.IGNORECASE), "problem_statement"),
    (re.compile(r"architecture", re.IGNORECASE), "architecture"),
    (re.compile(r"tech stack", re.IGNORECASE), "tech_stack"),
    (re.compile(r"\bmvp\b|break.*into an mvp", re.IGNORECASE), "mvp"),
    (re.compile(r"tasks for \d+|team members?", re.IGNORECASE), "task_breakdown"),
    (re.compile(r"pitch", re.IGNORECASE), "pitch"),
    (re.compile(r"demo flow|demo prep", re.IGNORECASE), "demo_flow"),
    (re.compile(r"improve our chances|judging", re.IGNORECASE), "judging"),
    (re.compile(r"highest.?risk", re.IGNORECASE), "risk"),
]


def classify_request(message: str) -> Optional[str]:
    """Which hackathon capability this message is asking for, or None if it doesn't match any."""
    for pattern, kind in _DISPATCH_PATTERNS:
        if pattern.search(message):
            return kind
    return None


def dispatch(message: str, project_description: Optional[str] = None, team_size: Optional[int] = None) -> Optional[str]:
    """
    Build the right prompt for `message`, or return None if this
    doesn't look like a hackathon-assistant request at all (caller
    should fall back to a generic prompt in that case).
    """
    kind = classify_request(message)
    if kind is None:
        return None
    desc = project_description or message
    if kind == "idea":
        return idea_generation_prompt()
    if kind == "problem_statement":
        return problem_statement_analysis_prompt(desc)
    if kind == "architecture":
        return architecture_prompt(desc)
    if kind == "tech_stack":
        return tech_stack_prompt(desc)
    if kind == "mvp":
        return mvp_breakdown_prompt(desc)
    if kind == "task_breakdown":
        match = re.search(r"(\d+)", message)
        n = team_size or (int(match.group(1)) if match else 3)
        return task_breakdown_prompt(desc, n)
    if kind == "pitch":
        return pitch_prompt(desc)
    if kind == "demo_flow":
        return demo_flow_prompt(desc)
    if kind == "judging":
        return judging_prep_prompt(desc)
    if kind == "risk":
        return risk_assessment_prompt(desc)
    return None
