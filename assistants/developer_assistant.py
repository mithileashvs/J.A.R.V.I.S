"""
JARVIS Developer/Debugging Mode (Phase 5, Section 7).

This does NOT reimplement debugging — debug_mode.py's Investigation
class (Phase 3) remains the single authoritative implementation for
actually running a diagnosis (gather context -> identify file -> check
terminal -> analyze code -> check project memory -> form diagnosis).
This module only:

  1. Tracks whether a session is currently "in developer mode"
     (via core/session_context.py's mode field — not a second state
     store).
  2. Recognizes the mode-entry/exit phrases from Section 7
     ("Jarvis, enter developer mode." / "Jarvis, debug this." /
     "Jarvis, explain the error.").
  3. Formats debug_mode.py's existing Diagnosis into the
     Problem/Likely Cause/Evidence/Fix/Verification shape Section 7
     asks for — Diagnosis.to_text() (Phase 3) already emits DIAGNOSIS/
     EVIDENCE/ROOT CAUSE/CONFIDENCE/SECONDARY ISSUES/RECOMMENDED FIX/
     NEXT STEP, so this is a thin relabeling for the specific "developer
     mode" framing Section 7 asks for, not a second diagnosis engine.
  4. Suggests commands without executing them (Section 7: "capable of
     suggesting commands without automatically executing them") —
     this returns text only; if the user then says "run it", that goes
     through the normal TERMINAL intent -> run_terminal_command ->
     permission_manager path exactly like any other command.
"""

import re
from dataclasses import dataclass
from typing import Optional

_ENTER_PATTERNS = re.compile(
    r"enter developer mode|debug this|explain the error|developer mode\b", re.IGNORECASE,
)
_EXIT_PATTERNS = re.compile(
    r"exit developer mode|leave developer mode|stop debugging|normal mode", re.IGNORECASE,
)
# Distinct from _ENTER_PATTERNS: this is specifically "start an
# investigation now" (Section 7's "Jarvis, debug this." / "Jarvis,
# explain the error."), separate from the plain mode-toggle phrase
# ("Jarvis, enter developer mode.") which should only flip the mode
# and wait for the next message — conflating the two would mean every
# mode-entry phrase also kicks off a (probably contextless) debug
# investigation, which Section 7's own examples treat as two different
# commands.
_INVESTIGATION_PATTERNS = re.compile(r"debug this|explain the error", re.IGNORECASE)


def wants_developer_mode(message: str) -> bool:
    return bool(_ENTER_PATTERNS.search(message))


def wants_to_exit_developer_mode(message: str) -> bool:
    return bool(_EXIT_PATTERNS.search(message))


def wants_investigation(message: str) -> bool:
    """True for 'debug this' / 'explain the error' — an explicit request to run an investigation now."""
    return bool(_INVESTIGATION_PATTERNS.search(message))


@dataclass
class DeveloperReport:
    problem: str
    likely_cause: str
    evidence: str
    fix: str
    verification: str

    def to_text(self) -> str:
        return (
            f"PROBLEM\n{self.problem}\n\n"
            f"LIKELY CAUSE\n{self.likely_cause}\n\n"
            f"EVIDENCE\n{self.evidence}\n\n"
            f"FIX\n{self.fix}\n\n"
            f"VERIFICATION\n{self.verification}"
        )


def format_diagnosis_as_developer_report(diagnosis) -> DeveloperReport:
    """
    diagnosis: a debug_mode.Diagnosis instance (Phase 3). Duck-typed
    rather than imported at module load time, so this module has no
    hard import-time dependency on debug_mode.py beyond what callers
    already bring in — avoids a circular-import risk since debug_mode
    may itself grow Phase 5 hooks later.
    """
    return DeveloperReport(
        problem=getattr(diagnosis, "diagnosis", None) or "See diagnosis below.",
        likely_cause=getattr(diagnosis, "root_cause", None) or "Not determined.",
        evidence=getattr(diagnosis, "evidence", None) or "No direct evidence captured.",
        fix=getattr(diagnosis, "recommended_fix", None) or "No fix suggested yet.",
        verification=getattr(diagnosis, "next_step", None) or "Re-run the failing command after applying a fix.",
    )


# Command suggestions are plain text, intentionally not tied to
# tool_registry — this module has no ability to run anything, matching
# Section 7's "suggesting... without automatically executing" and
# Section 13's permission classification (execution stays behind
# run_terminal_command's own SAFE/CONFIRM/BLOCKED classifier).
_SUGGESTIONS: dict[re.Pattern, str] = {
    re.compile(r"module not found|no module named", re.IGNORECASE): "pip install <missing-package>",
    re.compile(r"port.*(already in use|EADDRINUSE)", re.IGNORECASE): "lsof -i :<port>  (then kill the PID, or change the port)",
    re.compile(r"permission denied", re.IGNORECASE): "check file ownership with `ls -l <file>`, or re-run with appropriate permissions",
    re.compile(r"git.*(diverged|conflict)", re.IGNORECASE): "git status  (then resolve conflicts before committing)",
}


def suggest_command(error_text: str) -> Optional[str]:
    for pattern, suggestion in _SUGGESTIONS.items():
        if pattern.search(error_text):
            return suggestion
    return None
