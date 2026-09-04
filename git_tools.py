"""
JARVIS Git Assistant (Phase 4, Feature 4).

Reuses terminal_tools.py's subprocess machinery rather than
reimplementing process execution — every git invocation here goes
through terminal_tools.run_command, the same create_subprocess_exec
path (never shell=True) everything else in this codebase uses.

Permission model: every function in this module only ever runs
read-only git subcommands — status, diff, log, branch, show — which
are exactly the subcommands terminal_tools.classify_command already
lists as SAFE for `git` (see terminal_tools._SAFE_COMMANDS). Nothing
here stages, commits, pushes, or merges anything; commit-message
generation produces TEXT for the user to use themselves, it never
calls `git commit`. This is deliberate and matches Section "GIT
PERMISSIONS" + "Do not automatically commit" directly: rather than
building a second permission-classification path parallel to
terminal_tools', this module simply never reaches for anything above
SAFE in the first place. If a future feature needs git add/commit/
push, that goes through run_terminal_command's own CONFIRM gate like
any other state-changing command — not through this module.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("jarvis-git")

# Same reasoning as terminal_tools._MAX_OUTPUT_CHARS / code_analysis's
# file-size cap — a huge diff shouldn't blow up what gets returned to
# a caller (or, eventually, put in front of an LLM).
_MAX_DIFF_CHARS = 12_000
_MAX_LOG_ENTRIES = 30


@dataclass
class GitStatusEntry:
    path: str
    status: str          # "modified" | "added" | "deleted" | "renamed" | "untracked" | "unmerged"
    staged: bool


@dataclass
class GitStatusResult:
    available: bool
    reason: Optional[str] = None
    branch: Optional[str] = None
    entries: list[GitStatusEntry] = field(default_factory=list)

    def to_text(self) -> str:
        if not self.available:
            return f"Git status unavailable: {self.reason}"
        if not self.entries:
            return f"On branch {self.branch or 'unknown'} — working tree clean."
        lines = [f"On branch {self.branch or 'unknown'} — {len(self.entries)} file(s) changed:"]
        for e in self.entries:
            tag = "staged" if e.staged else "unstaged"
            lines.append(f"  [{e.status}, {tag}] {e.path}")
        return "\n".join(lines)


@dataclass
class GitDiffResult:
    available: bool
    reason: Optional[str] = None
    stat_summary: Optional[str] = None
    diff_text: Optional[str] = None
    truncated: bool = False


# git status --porcelain=v1 two-letter codes: [index][worktree].
# See `git help status` — this covers the codes that actually occur in
# practice; anything else falls back to "unmerged"/"unknown" rather
# than guessing.
_STATUS_CODE_MAP = {
    "M": "modified", "A": "added", "D": "deleted",
    "R": "renamed", "C": "copied", "U": "unmerged", "?": "untracked",
}


def _resolve_cwd(cwd: Optional[str]) -> Optional[str]:
    """
    Falls back to the active project path (context_manager.py) when no
    explicit cwd is given, same pattern debug_mode.py's environment
    check uses for inspect_environment — callers shouldn't have to
    thread the active project path through manually.
    """
    if cwd:
        return cwd
    import context_manager
    return context_manager.context_manager.get_active_project_path()


async def _run_git(args: list[str], cwd: Optional[str]) -> tuple[bool, str, str]:
    """
    Run a fixed, hardcoded git subcommand (never user-supplied raw
    text) via terminal_tools.run_command. Returns (ok, stdout, stderr)
    — ok is False for "not a git repo" / "git not installed" /
    nonzero exit, with stderr carrying the reason, so every public
    function here can report failure honestly instead of crashing.
    """
    import shlex
    import terminal_tools as _tt

    command = "git " + " ".join(shlex.quote(a) for a in args)
    result = await _tt.run_command(command, cwd=cwd, timeout=15.0)
    if result.exit_code is None:
        return False, result.stdout, result.stderr or "git is not installed or could not be launched."
    if result.exit_code != 0:
        stderr = result.stderr.strip()
        if "not a git repository" in stderr.lower():
            return False, result.stdout, "Not a Git repository."
        return False, result.stdout, stderr or f"git exited with code {result.exit_code}."
    return True, result.stdout, result.stderr


async def git_status(cwd: Optional[str] = None) -> GitStatusResult:
    resolved = _resolve_cwd(cwd)
    if resolved is None:
        return GitStatusResult(available=False, reason="No project path given and no active project set.")

    branch_ok, branch_out, branch_err = await _run_git(["branch", "--show-current"], resolved)
    ok, out, err = await _run_git(["status", "--porcelain=v1"], resolved)
    if not ok:
        return GitStatusResult(available=False, reason=err)

    entries = []
    for line in out.splitlines():
        if not line or len(line) < 4:
            continue
        index_code, worktree_code, path = line[0], line[1], line[3:]
        # Rename entries look like "R  old -> new" — surface the new path.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if index_code == "?" and worktree_code == "?":
            entries.append(GitStatusEntry(path=path, status="untracked", staged=False))
            continue
        if index_code != " " and index_code != "?":
            entries.append(GitStatusEntry(path=path, status=_STATUS_CODE_MAP.get(index_code, "modified"), staged=True))
        if worktree_code != " " and worktree_code != "?":
            entries.append(GitStatusEntry(path=path, status=_STATUS_CODE_MAP.get(worktree_code, "modified"), staged=False))

    return GitStatusResult(
        available=True,
        branch=branch_out.strip() if branch_ok else None,
        entries=entries,
    )


async def git_diff(cwd: Optional[str] = None, staged: bool = False, file: Optional[str] = None) -> GitDiffResult:
    resolved = _resolve_cwd(cwd)
    if resolved is None:
        return GitDiffResult(available=False, reason="No project path given and no active project set.")

    stat_args = ["diff", "--stat"]
    diff_args = ["diff"]
    if staged:
        stat_args.append("--staged")
        diff_args.append("--staged")
    if file:
        stat_args.append("--")
        stat_args.append(file)
        diff_args.append("--")
        diff_args.append(file)

    stat_ok, stat_out, stat_err = await _run_git(stat_args, resolved)
    if not stat_ok:
        return GitDiffResult(available=False, reason=stat_err)

    diff_ok, diff_out, diff_err = await _run_git(diff_args, resolved)
    truncated = False
    if len(diff_out) > _MAX_DIFF_CHARS:
        diff_out = diff_out[:_MAX_DIFF_CHARS] + "\n... [diff truncated]"
        truncated = True

    return GitDiffResult(
        available=True,
        stat_summary=stat_out.strip() or "No changes.",
        diff_text=diff_out,
        truncated=truncated,
    )


async def git_log(cwd: Optional[str] = None, limit: int = 10) -> dict:
    resolved = _resolve_cwd(cwd)
    if resolved is None:
        return {"available": False, "reason": "No project path given and no active project set."}
    limit = max(1, min(limit, _MAX_LOG_ENTRIES))
    ok, out, err = await _run_git(["log", f"-{limit}", "--pretty=format:%h|%an|%ar|%s"], resolved)
    if not ok:
        return {"available": False, "reason": err}
    if not out.strip():
        return {"available": True, "commits": []}
    commits = []
    for line in out.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({"hash": parts[0], "author": parts[1], "when": parts[2], "message": parts[3]})
    return {"available": True, "commits": commits}


async def git_branch(cwd: Optional[str] = None) -> dict:
    resolved = _resolve_cwd(cwd)
    if resolved is None:
        return {"available": False, "reason": "No project path given and no active project set."}
    ok, out, err = await _run_git(["branch", "-vv"], resolved)
    if not ok:
        return {"available": False, "reason": err}
    branches = []
    current = None
    for line in out.splitlines():
        is_current = line.startswith("*")
        name = line[2:].split()[0] if line[2:].split() else line[2:].strip()
        branches.append({"name": name, "current": is_current})
        if is_current:
            current = name
    return {"available": True, "current": current, "branches": branches}


# ── Change summaries (Section: GIT CHANGE ANALYSIS) ─────────────────

# File patterns that hint at what a change is *about*, used only to
# group/describe files in the summary — never to guess intent beyond
# what the path itself says.
_CATEGORY_PATTERNS = [
    ("tests", re.compile(r"(^|/)tests?/|_test\.py$|\.test\.[jt]sx?$|^test_")),
    ("docs", re.compile(r"(^|/)(docs?/|readme)", re.IGNORECASE)),
    ("dependencies", re.compile(r"(requirements.*\.txt$|package\.json$|package-lock\.json$|pyproject\.toml$|Pipfile$|go\.mod$|Cargo\.toml$)")),
    ("config", re.compile(r"\.(ya?ml|json|toml|ini|env)$|(^|/)\.env")),
]


def _categorize(path: str) -> str:
    for label, pattern in _CATEGORY_PATTERNS:
        if pattern.search(path):
            return label
    return "code"


# Dependency files changed without an obvious pinned version — same
# "flag it, don't guess harder than the evidence supports" principle
# as debug_mode.py's confidence labelling.
_UNPINNED_DEP_PATTERN = re.compile(r'^\+\s*[\w.-]+\s*$', re.MULTILINE)  # a bare "+packagename" line, no version


async def generate_change_summary(cwd: Optional[str] = None) -> dict:
    """
    Section: GIT CHANGE ANALYSIS. Groups changed files, gives a short
    per-file description (from the diff stat, not a guess at semantic
    intent this module can't actually know), and flags a small set of
    concrete, checkable concerns — never invents a narrative the diff
    doesn't support.
    """
    status = await git_status(cwd)
    if not status.available:
        return {"available": False, "reason": status.reason}
    if not status.entries:
        return {"available": True, "file_count": 0, "groups": {}, "concerns": [], "summary_text": "No changes to summarize — working tree clean."}

    resolved = _resolve_cwd(cwd)
    diff = await git_diff(resolved, staged=False)
    unstaged_diff_text = diff.diff_text or ""
    staged_diff = await git_diff(resolved, staged=True)
    staged_diff_text = staged_diff.diff_text or ""
    combined_diff = unstaged_diff_text + "\n" + staged_diff_text

    groups: dict[str, list[str]] = {}
    seen_paths = set()
    for entry in status.entries:
        if entry.path in seen_paths:
            continue
        seen_paths.add(entry.path)
        groups.setdefault(_categorize(entry.path), []).append(entry.path)

    concerns = []
    for path in seen_paths:
        if _categorize(path) == "dependencies":
            file_diff_ok, file_diff_out, _ = await _run_git(["diff", "--", path], resolved)
            file_diff_ok2, file_diff_out2, _ = await _run_git(["diff", "--staged", "--", path], resolved)
            combined_file_diff = (file_diff_out or "") + "\n" + (file_diff_out2 or "")
            if _UNPINNED_DEP_PATTERN.search(combined_file_diff):
                concerns.append(f"{path}: a dependency appears to have been added without a pinned version.")

    lines = [f"CHANGES SUMMARY", "", f"{len(seen_paths)} file(s) modified.", ""]
    for category, paths in sorted(groups.items()):
        for path in paths:
            lines.append(f"{path} ({category})")
    if concerns:
        lines.append("")
        lines.append("POTENTIAL CONCERN")
        lines.extend(f"- {c}" for c in concerns)

    return {
        "available": True,
        "file_count": len(seen_paths),
        "groups": groups,
        "concerns": concerns,
        "summary_text": "\n".join(lines),
    }


# ── Commit message generation (Section: GIT COMMIT MESSAGE GENERATION) ──

_COMMIT_TYPE_BY_CATEGORY = {
    "tests": "test",
    "docs": "docs",
    "dependencies": "chore",
    "config": "chore",
    "code": "feat",
}


async def generate_commit_message(cwd: Optional[str] = None) -> dict:
    """
    Proposes a conventional-commit-style message from the staged
    changes (falls back to unstaged if nothing is staged, since a
    user asking for a commit message before running `git add` is a
    completely normal thing to do). Rule-based, not an LLM call — same
    "structure the evidence, don't narrate" principle code_analysis.py
    and debug_mode.py both follow, and it keeps this deterministic and
    testable. NEVER runs `git commit` — this only ever returns text.
    """
    resolved = _resolve_cwd(cwd)
    if resolved is None:
        return {"available": False, "reason": "No project path given and no active project set."}

    staged_status = await git_status(resolved)
    if not staged_status.available:
        return {"available": False, "reason": staged_status.reason}

    staged_entries = [e for e in staged_status.entries if e.staged]
    using = staged_entries if staged_entries else staged_status.entries
    source_label = "staged" if staged_entries else "unstaged (nothing is staged yet)"
    if not using:
        return {"available": True, "message": None, "reason": "No changes to describe."}

    categories = {}
    for e in using:
        categories.setdefault(_categorize(e.path), []).append(e.path)

    # Pick the dominant category (most files) to choose the commit type/scope.
    dominant_category = max(categories, key=lambda c: len(categories[c]))
    commit_type = _COMMIT_TYPE_BY_CATEGORY.get(dominant_category, "chore")
    scope = None
    all_paths = [e.path for e in using]
    top_dirs = {p.split("/")[0] for p in all_paths if "/" in p}
    if len(top_dirs) == 1:
        scope = next(iter(top_dirs))

    subject_target = os.path.basename(all_paths[0]) if len(all_paths) == 1 else f"{len(all_paths)} files"
    header = f"{commit_type}({scope}): update {subject_target}" if scope else f"{commit_type}: update {subject_target}"

    body_lines = []
    for e in using[:10]:
        verb = {"added": "add", "deleted": "remove", "modified": "update", "renamed": "rename", "untracked": "add"}.get(e.status, "update")
        body_lines.append(f"- {verb} {e.path}")

    message = header + "\n\n" + "\n".join(body_lines)
    return {"available": True, "message": message, "source": source_label}


# ── Merge conflict explanation (Section: MERGE CONFLICT EXPLANATION) ────

# ── Natural-language sub-routing (Section: GIT ASSISTANT user examples) ─
#
# The intent router (intent_router.py) only classifies down to GIT —
# it doesn't say *which* git action the user wants. Rather than
# building a second Ollama classification pass just for that, this is
# a small, honest keyword router over the specific example phrases
# the brief itself gives ("what changed?", "explain these changes",
# "generate a commit message", "explain this merge conflict", ...).
# Falls back to "summary" (git status + change summary combined) —
# the most generally useful default — when nothing more specific
# matches, rather than guessing at something narrower.
_GIT_ACTION_KEYWORDS = [
    ("merge_conflict", ("merge conflict", "conflict marker", "resolve conflict")),
    ("commit_message", ("commit message", "generate a commit", "write a commit")),
    ("log", ("commit history", "recent commits", "git log", "last commits")),
    ("branch", ("what branch", "which branch", "current branch", "list branches")),
    ("diff", ("show me the diff", "git diff", "the full diff", "since my last commit")),
    ("summary", (
        "what changed", "what did i modify", "explain these changes", "explain the changes",
        "summarize my work", "summarize the changes", "what's changed", "whats changed",
    )),
]


def route_git_request(message: str) -> str:
    lowered = message.lower()
    for action, phrases in _GIT_ACTION_KEYWORDS:
        if any(p in lowered for p in phrases):
            return action
    return "summary"


@dataclass
class ConflictBlock:
    ours: str
    theirs: str
    ours_label: str
    theirs_label: str
    context_before: str = ""


@dataclass
class MergeConflictAnalysis:
    available: bool
    reason: Optional[str] = None
    file: Optional[str] = None
    blocks: list[ConflictBlock] = field(default_factory=list)

    def to_text(self) -> str:
        if not self.available:
            return f"Could not analyze merge conflict: {self.reason}"
        if not self.blocks:
            return f"No conflict markers found in '{self.file}'."
        lines = [f"MERGE CONFLICT in '{self.file}' — {len(self.blocks)} conflicted section(s)."]
        for i, b in enumerate(self.blocks, start=1):
            lines.append(f"\nSECTION {i}")
            lines.append(f"OURS ({b.ours_label}):\n{b.ours.strip() or '(empty)'}")
            lines.append(f"THEIRS ({b.theirs_label}):\n{b.theirs.strip() or '(empty)'}")
        lines.append(
            "\nJARVIS has not changed anything — resolve by keeping one side, "
            "combining both, or writing a new version, then remove the "
            "<<<<<<< / ======= / >>>>>>> markers and stage the file."
        )
        return "\n".join(lines)


_CONFLICT_START = re.compile(r"^<{7} (.*)$")
_CONFLICT_MID = re.compile(r"^={7}$")
_CONFLICT_END = re.compile(r"^>{7} (.*)$")


async def analyze_merge_conflict(file_path: str) -> MergeConflictAnalysis:
    """
    Read-only — parses conflict markers already in the file (put there
    by git itself during a failed merge) and explains both sides.
    Never edits the file or picks a side; Section "Do not automatically
    resolve the conflict unless the user explicitly approves."
    """
    if not os.path.isfile(file_path):
        return MergeConflictAnalysis(available=False, reason=f"File not found: '{file_path}'.")

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return MergeConflictAnalysis(available=False, reason=f"Could not read file: {e}")

    blocks = []
    i = 0
    while i < len(lines):
        start_match = _CONFLICT_START.match(lines[i].rstrip("\n"))
        if not start_match:
            i += 1
            continue
        ours_label = start_match.group(1).strip() or "current change"
        ours_lines = []
        i += 1
        while i < len(lines) and not _CONFLICT_MID.match(lines[i].rstrip("\n")):
            ours_lines.append(lines[i])
            i += 1
        i += 1  # skip the ======= line
        theirs_lines = []
        theirs_label = "incoming change"
        while i < len(lines):
            end_match = _CONFLICT_END.match(lines[i].rstrip("\n"))
            if end_match:
                theirs_label = end_match.group(1).strip() or "incoming change"
                i += 1
                break
            theirs_lines.append(lines[i])
            i += 1
        blocks.append(ConflictBlock(
            ours="".join(ours_lines), theirs="".join(theirs_lines),
            ours_label=ours_label, theirs_label=theirs_label,
        ))

    return MergeConflictAnalysis(available=True, file=file_path, blocks=blocks)
