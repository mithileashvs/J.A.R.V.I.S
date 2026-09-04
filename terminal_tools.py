"""
JARVIS terminal tool (Phase 3, Sections 6-8).

Security model (Section 7), implemented as actual code, not just
categories on paper:

  - Commands are NEVER run through a shell (no shell=True, no
    create_subprocess_shell). asyncio.create_subprocess_exec is used
    exclusively, which passes argv directly to the OS with no shell
    interpretation at all — "&&", "|", ";", backticks, "$()", and
    redirection operators are inert, literal argument text, not shell
    syntax, regardless of what the command string contains.
  - As a second, independent layer (defense in depth — not required
    for safety given the above, but makes intent explicit and catches
    attempts to chain commands even before they'd fail at the shell
    level), any command containing a shell metacharacter TOKEN is
    rejected outright by classify_command() before execution is even
    considered.
  - Every command is classified SAFE / CONFIRM / DANGEROUS by
    matching its first token (the actual program being run) against
    explicit allow/require-confirmation/require-explicit-confirmation
    lists from Section 7 — never a substring match on the whole
    command string, which would be trivially bypassed
    ("echo git status" is not "git status").
  - Unknown commands (not on any list) default to CONFIRM, never SAFE
    — an unrecognized command is not assumed harmless.
  - This module itself never decides to skip confirmation; it reports
    a classification. tool_registry.py's existing permission_manager
    (Phase 1) is what actually enforces the CONFIRM gate — this file
    doesn't duplicate that system, per the Phase 3 brief.
"""

import asyncio
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("jarvis-terminal")

_WINDOWS = sys.platform.startswith("win")

# Programs that are cmd.exe *builtins* on Windows rather than standalone
# executables on PATH — e.g. `echo`/`dir`/`type` only exist as commands
# cmd.exe itself interprets, so create_subprocess_exec can never find them
# directly (there is no echo.exe). Routing just these through `cmd /c`
# with the already-tokenized argv (never the raw string) preserves the
# "no shell interpretation of user input" guarantee: classify_command()
# already rejected any shell metacharacter in the original command before
# execution reaches here, so there is nothing left for cmd.exe to expand.
_WINDOWS_CMD_BUILTINS = {"echo", "dir", "type", "cls", "ver", "vol"}


def _adapt_tokens_for_platform(tokens: list[str]) -> list[str]:
    """
    Translate a handful of Unix-only command names to a Windows-runnable
    equivalent when running on Windows. No-op everywhere else, and a
    no-op for anything not explicitly listed here — this only patches
    over real cross-platform gaps, it never changes behavior for
    commands that already work as typed.
    """
    if not _WINDOWS or not tokens:
        return tokens

    program = tokens[0].lower()

    # `sleep` has no standalone executable on stock Windows. Reproduce
    # "block for N seconds, still killable/timeout-able like any other
    # subprocess" using the same Python interpreter running JARVIS,
    # rather than shelling out to a Windows-only equivalent.
    if program == "sleep" and len(tokens) == 2:
        try:
            seconds = float(tokens[1])
        except ValueError:
            return tokens
        return [sys.executable, "-c", f"import time; time.sleep({seconds})"]

    if program in _WINDOWS_CMD_BUILTINS:
        return ["cmd", "/c", *tokens]

    return tokens

# Any of these appearing as a standalone token means "reject outright,
# this looks like an attempt to chain/redirect/substitute commands" —
# defense in depth on top of create_subprocess_exec already making
# shell interpretation impossible (see module docstring).
_SHELL_METACHARACTERS = {"&&", "||", ";", "|", ">", ">>", "<", "`", "$(", "&"}

# First-token classification. Matched against the program name only
# (argv[0]), not the full command string.
_SAFE_COMMANDS = {
    "git": {"status", "diff", "log", "branch", "show", "remote"},  # only these subcommands are SAFE
    "python": {"--version"}, "python3": {"--version"}, "py": {"--version"},
    "node": {"--version"}, "npm": {"--version", "list"},
    "pip": {"list", "show", "--version"},
    "ls": None, "dir": None, "pwd": None, "cd": None,  # None = any args are SAFE for these
    "whoami": None, "echo": None, "cat": None, "type": None,
}

_CONFIRM_COMMANDS = {
    "pip": {"install", "uninstall"},
    "npm": {"install", "uninstall", "run", "start", "build", "test"},
    "python": None, "python3": None, "py": None,  # running a script — could do anything
    "pytest": None, "node": None,
    "git": {"add", "checkout", "merge", "pull", "fetch", "stash", "commit", "push", "reset"},
}

_DANGEROUS_COMMANDS = {
    "rm", "rmdir", "del", "erase", "format", "taskkill", "kill",
    "shutdown", "mkfs",
}
# Git subcommands that are CONFIRM by default (see _CONFIRM_COMMANDS)
# but escalate to DANGEROUS specifically when a destructive flag is
# present — "git checkout main" just switches branches (CONFIRM is
# right for that); "git checkout -- ." or "git checkout -f" discards
# uncommitted work, which is what Section 7 actually means by
# "checkout with destructive effects". Checking for the flag, not
# blanket-flagging the subcommand, is what keeps this matching the
# spec instead of over-blocking an everyday branch switch.
_GIT_DESTRUCTIVE_FLAGS = {"-f", "--force", "--hard"}
# Per Section 7's explicit DANGEROUS list: "git reset --hard, git clean"
# — not bare "git reset" or "git push". Plain "git reset" (no --hard)
# only unstages files, which is far less destructive and stays CONFIRM
# via _CONFIRM_COMMANDS below; "git push" (even without force) stays
# CONFIRM too, matching Section 7 of the original Phase 2 spec.
_DANGEROUS_GIT_SUBCOMMANDS = {"clean"}  # clean is unconditionally dangerous (deletes untracked files)

_MAX_OUTPUT_CHARS = 20_000  # cap captured stdout/stderr so a runaway command can't blow up memory/context

# How long a command's result stays "recent enough" for debug_mode.py's
# investigation to treat it as relevant to a live "why isn't this
# working" question. Past this, an old error is more likely to
# misdiagnose a since-fixed problem than help — see get_last_result().
_LAST_RESULT_MAX_AGE_SECONDS = 600.0  # 10 minutes


@dataclass
class CommandClassification:
    level: str          # "SAFE" | "CONFIRM" | "DANGEROUS" | "REJECTED"
    reason: str


def classify_command(command: str) -> CommandClassification:
    """
    Classify without executing. REJECTED is stronger than DANGEROUS —
    DANGEROUS commands can still run with confirmation; REJECTED
    commands never run at all (chaining/redirection attempts).
    """
    stripped = command.strip()
    if not stripped:
        return CommandClassification("REJECTED", "Empty command.")

    # Tokenize defensively — shlex can raise on unbalanced quotes.
    import shlex
    try:
        tokens = shlex.split(stripped)
    except ValueError as e:
        return CommandClassification("REJECTED", f"Could not parse command safely: {e}")

    if not tokens:
        return CommandClassification("REJECTED", "Empty command after parsing.")

    # A metacharacter can appear as its own token ("cmd && cmd2") or
    # merged onto an adjacent word with no space ("status;", "`rm") —
    # shlex tokenization already consumed those boundaries, so this
    # checks the raw stripped string directly rather than per-token,
    # which catches both cases in one pass instead of needing two.
    #
    # Known trade-off, accepted deliberately: this also rejects a
    # metacharacter that's safely inside quotes, e.g.
    # `git commit -m "fix bug; update docs"` — the semicolon there is
    # inert (part of a quoted string, not shell syntax) but gets
    # blocked anyway. A quote-aware check could allow that case
    # through, but the failure mode of getting it wrong runs one way
    # only: false-rejecting a legitimate quoted command costs the user
    # a rephrase; false-accepting an actual chained/injected command
    # is a real security hole. Conservative-by-default is the correct
    # choice for a security boundary like this one.
    for meta in _SHELL_METACHARACTERS:
        if meta in stripped:
            return CommandClassification(
                "REJECTED",
                f"Command contains a shell metacharacter ('{meta}') — chaining, redirection, "
                f"and substitution are not allowed. Run one command at a time.",
            )

    program = tokens[0].lower()
    args = tokens[1:]
    subcommand = args[0].lower() if args else None

    if program in _DANGEROUS_COMMANDS:
        return CommandClassification("DANGEROUS", f"'{program}' can destroy data or kill processes.")

    if program == "git":
        if subcommand in _DANGEROUS_GIT_SUBCOMMANDS:
            return CommandClassification("DANGEROUS", f"'git {subcommand}' can discard or overwrite work.")
        if subcommand == "reset" and any(a in _GIT_DESTRUCTIVE_FLAGS for a in args[1:]):
            return CommandClassification("DANGEROUS", "'git reset --hard' discards uncommitted changes.")
        if subcommand == "checkout" and any(a in _GIT_DESTRUCTIVE_FLAGS for a in args[1:]):
            return CommandClassification(
                "DANGEROUS",
                "'git checkout' with a force flag discards uncommitted changes.",
            )

    if program in _SAFE_COMMANDS:
        allowed_subs = _SAFE_COMMANDS[program]
        if allowed_subs is None or subcommand in allowed_subs:
            return CommandClassification("SAFE", f"'{program}{' ' + subcommand if subcommand else ''}' is read-only/informational.")
        # Falls through to CONFIRM check below if the subcommand isn't in the SAFE set for this program.

    if program in _CONFIRM_COMMANDS:
        allowed_subs = _CONFIRM_COMMANDS[program]
        if allowed_subs is None or subcommand in allowed_subs:
            return CommandClassification("CONFIRM", f"'{program}{' ' + subcommand if subcommand else ''}' can change project state.")

    # Unknown command entirely, or a known program with a subcommand
    # not explicitly classified anywhere above — default to CONFIRM,
    # never SAFE. An unrecognized command is not assumed harmless.
    return CommandClassification("CONFIRM", f"'{program}' is not on the recognized-safe list.")


@dataclass
class CommandResult:
    command: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    execution_time: float
    timed_out: bool = False
    truncated: bool = False


# Module-level "last command result" cache. This is what lets Debug
# Mode's investigation (Section 10: "CHECK TERMINAL / ERROR OUTPUT")
# actually see terminal activity without continuously capturing the
# screen — JARVIS only ever knows about a terminal command if IT ran
# one via run_terminal_command, which is exactly the privacy boundary
# Section 15 asks for ("prefer targeted extraction", never continuous
# capture). A single global is enough for this project's actual
# architecture (one JARVIS backend process, one user, no
# multi-tenancy anywhere else in the codebase) — see
# get_last_result()/reset_last_result() for how callers read/clear it.
_last_result: Optional[CommandResult] = None
_last_result_at: Optional[float] = None


def get_last_result(max_age_seconds: float = _LAST_RESULT_MAX_AGE_SECONDS) -> Optional[CommandResult]:
    """
    Return the most recent run_command() result, or None if there
    isn't one or it's older than max_age_seconds. Age-bounding matters
    because Debug Mode uses this as evidence for "what's currently
    wrong" — a 2-hour-old error from a since-fixed problem is worse
    than no evidence at all, since it actively misleads the diagnosis.
    """
    if _last_result is None or _last_result_at is None:
        return None
    if time.monotonic() - _last_result_at > max_age_seconds:
        return None
    return _last_result


def reset_last_result() -> None:
    """Clear the cache. Used by tests for isolation; also safe to call in production (e.g. on session end)."""
    global _last_result, _last_result_at
    _last_result = None
    _last_result_at = None


async def run_command(command: str, cwd: Optional[str] = None, timeout: float = 30.0) -> CommandResult:
    """
    Execute a command via create_subprocess_exec — NEVER shell=True,
    see module docstring. Callers are responsible for having already
    checked classify_command() and gone through the permission system
    for anything above SAFE; this function executes unconditionally
    once called; it is not itself a security boundary, the caller is.
    """
    global _last_result, _last_result_at

    import shlex
    tokens = shlex.split(command.strip())
    tokens = _adapt_tokens_for_platform(tokens)

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *tokens,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            stdout_bytes, stderr_bytes = b"", b""
            timed_out = True

    except FileNotFoundError:
        elapsed = time.monotonic() - start
        result = CommandResult(
            command=command, exit_code=None, stdout="",
            stderr=f"Command not found: '{tokens[0]}'",
            execution_time=elapsed,
        )
        _last_result, _last_result_at = result, time.monotonic()
        return result
    except Exception as e:
        elapsed = time.monotonic() - start
        result = CommandResult(
            command=command, exit_code=None, stdout="",
            stderr=f"Failed to launch command: {e}",
            execution_time=elapsed,
        )
        _last_result, _last_result_at = result, time.monotonic()
        return result

    elapsed = time.monotonic() - start
    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    truncated = False
    if len(stdout) > _MAX_OUTPUT_CHARS:
        stdout = stdout[:_MAX_OUTPUT_CHARS] + "\n... [output truncated]"
        truncated = True
    if len(stderr) > _MAX_OUTPUT_CHARS:
        stderr = stderr[:_MAX_OUTPUT_CHARS] + "\n... [output truncated]"
        truncated = True

    result = CommandResult(
        command=command,
        exit_code=proc.returncode if not timed_out else None,
        stdout=stdout,
        stderr=stderr,
        execution_time=elapsed,
        timed_out=timed_out,
        truncated=truncated,
    )
    _last_result, _last_result_at = result, time.monotonic()
    return result


def extract_command_from_message(message: str) -> Optional[str]:
    """
    Best-effort extraction of a literal command from a natural-language
    TERMINAL-intent chat message — e.g. "run `pytest -k foo`" or
    "what does git status show". Deliberately conservative: returns
    None rather than guessing when nothing looks like an actual
    command, so the caller can ask the user to clarify instead of
    running the wrong thing (same "don't supply unstated assumptions"
    principle code_analysis.py's guess_target_file() follows).

    Checked in order of reliability:
      1. Backtick-quoted text: `pytest -k foo` — most reliable, the
         user explicitly delimited it.
      2. Text after "run"/"execute" (case-insensitive) to the end of
         the message.
      3. Nothing recognized -> None.
    """
    import re

    backtick_match = re.search(r"`([^`]+)`", message)
    if backtick_match:
        candidate = backtick_match.group(1).strip()
        return candidate or None

    trigger_match = re.search(r"\b(?:run|execute)\b\s+(.+)", message, re.IGNORECASE)
    if trigger_match:
        candidate = trigger_match.group(1).strip().rstrip("?.!")
        return candidate or None

    return None


# ── Error extraction (Section 8) ────────────────────────────────────

@dataclass
class ExtractedError:
    primary_error: Optional[str] = None
    likely_root_cause: Optional[str] = None
    secondary_errors: list[str] = field(default_factory=list)
    relevant_files: list[str] = field(default_factory=list)
    error_type: Optional[str] = None  # "python_traceback" | "npm" | "port_conflict" | "permission" | "generic" | None


_PATTERNS = {
    "module_not_found": re.compile(r"ModuleNotFoundError: No module named '([^']+)'"),
    "import_error": re.compile(r"ImportError: (.+)"),
    "python_syntax_error": re.compile(r"SyntaxError: (.+)"),
    "npm_error": re.compile(r"npm ERR! (.+)"),
    "port_in_use": re.compile(r"(?:EADDRINUSE|address already in use).*?:(\d+)", re.IGNORECASE),
    "permission_denied": re.compile(r"(?:PermissionError|EACCES|Permission denied)", re.IGNORECASE),
    "file_ref": re.compile(r'File "([^"]+)", line (\d+)'),
}


# ── Environment inspection (Section 6/17: inspect_environment) ─────
#
# Deliberately a fixed, hardcoded set of read-only commands per
# target — never user-supplied strings — so this can safely run as
# SAFE (no confirmation) even though it goes through the same
# create_subprocess_exec path as arbitrary terminal commands. Nothing
# here ever writes, installs, or modifies anything.
_ENVIRONMENT_COMMANDS = {
    "python": [
        ("version", "python --version"),
        ("packages", "pip list"),
    ],
    "node": [
        ("version", "node --version"),
        ("npm_version", "npm --version"),
        ("packages", "npm list --depth=0"),
    ],
}


async def inspect_environment(target: str, cwd: Optional[str] = None) -> dict:
    """
    Run a fixed set of read-only version/package-listing commands for
    'python' or 'node' and return the combined result. This is what
    lets a debugging investigation distinguish "package genuinely
    missing" from "wrong interpreter/environment active" (Section
    11's own worked example) without the model guessing.
    """
    target = target.lower().strip()
    if target not in _ENVIRONMENT_COMMANDS:
        return {
            "available": False,
            "reason": f"Unknown environment target '{target}'. Must be one of: {sorted(_ENVIRONMENT_COMMANDS)}.",
        }

    results = {}
    for label, command in _ENVIRONMENT_COMMANDS[target]:
        outcome = await run_command(command, cwd=cwd, timeout=15.0)
        results[label] = {
            "command": command,
            "exit_code": outcome.exit_code,
            "stdout": outcome.stdout.strip(),
            "stderr": outcome.stderr.strip(),
        }

    return {"available": True, "target": target, "results": results}


def extract_errors(output: str) -> ExtractedError:
    """
    Pull structured signal out of raw terminal output rather than
    handing the whole blob to an LLM — Section 8 explicitly asks for
    this. Pattern-based, so it only reports what it can genuinely
    identify; anything not matched stays out of the structured result
    (still available in the raw output the caller already has).
    """
    result = ExtractedError()

    if not output.strip():
        return result

    lines = output.splitlines()

    mod_match = _PATTERNS["module_not_found"].search(output)
    if mod_match:
        result.primary_error = f"ModuleNotFoundError: No module named '{mod_match.group(1)}'"
        result.likely_root_cause = (
            f"The '{mod_match.group(1)}' package is not installed in the active Python "
            f"environment, or the wrong environment/interpreter is active."
        )
        result.error_type = "python_traceback"

    elif _PATTERNS["import_error"].search(output):
        m = _PATTERNS["import_error"].search(output)
        result.primary_error = f"ImportError: {m.group(1)}"
        result.error_type = "python_traceback"

    elif _PATTERNS["python_syntax_error"].search(output):
        m = _PATTERNS["python_syntax_error"].search(output)
        result.primary_error = f"SyntaxError: {m.group(1)}"
        result.error_type = "python_traceback"

    elif _PATTERNS["port_in_use"].search(output):
        m = _PATTERNS["port_in_use"].search(output)
        port = m.group(1) if m.lastindex else "unknown"
        result.primary_error = f"Port already in use (port {port})."
        result.likely_root_cause = "Another process is already listening on this port."
        result.error_type = "port_conflict"

    elif _PATTERNS["permission_denied"].search(output):
        result.primary_error = "Permission denied."
        result.error_type = "permission"

    elif _PATTERNS["npm_error"].search(output):
        m = _PATTERNS["npm_error"].search(output)
        result.primary_error = f"npm error: {m.group(1)}"
        result.error_type = "npm"

    else:
        # No recognized pattern — report the last non-empty line as a
        # weak signal rather than nothing, but do NOT claim a type or
        # root cause we don't actually have evidence for.
        non_empty = [l for l in lines if l.strip()]
        if non_empty:
            result.primary_error = non_empty[-1].strip()
            result.error_type = "generic"

    # File references (Python tracebacks) — collected regardless of
    # which primary pattern matched, since a traceback can span
    # several files.
    for m in _PATTERNS["file_ref"].finditer(output):
        path = m.group(1)
        if path not in result.relevant_files:
            result.relevant_files.append(path)

    return result
