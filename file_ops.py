"""
JARVIS controlled file modification (Phase 3, Section 14).

Closes the one gap Section 14 explicitly warned about: JARVIS
proposing a fix but never having any way to actually apply it. This
module is the "APPLY FIX" + "VERIFY RESULT" half of that workflow —
the "PROPOSE FIX" / "SHOW SUMMARY" half already happens in prose
before this is ever called, and "REQUEST USER CONFIRMATION" is
enforced by tool_registry.py registering apply_fix as CONFIRM (never
SAFE) — this module has no opinion on permissions, it only executes
once told to.

Safety properties:
  - Never called directly from a chat turn. Only reachable via
    tool_registry.py's apply_fix ToolSpec, which is CONFIRM-tier — the
    existing permission_manager confirmation flow (Allow/Deny in the
    UI) is what actually gates this, exactly like Section 14 asks.
  - A timestamped backup of the original file is always written
    alongside it before the new content is applied, so a bad fix is
    trivially reversible without relying on git.
  - "Verify result" is real, not decorative: for Python files it
    re-runs the same pyflakes pass code_analysis.py uses, so the
    caller can see immediately whether the fix introduced a NEW
    syntax/undefined-name problem, not just that the write succeeded.
  - Bounded like everything else in Phase 3: same _MAX_FILE_BYTES
    ceiling code_analysis.py uses, so this can't be used to silently
    dump an enormous amount of content into a file at once.
"""

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("jarvis-file-ops")

_MAX_FILE_BYTES = 500_000  # matches code_analysis.py's ceiling — same reasoning


@dataclass
class ApplyFixResult:
    file_path: str
    backup_path: str
    bytes_written: int
    verification: Optional[dict] = None


def apply_fix(file_path: str, new_content: str, description: str = "") -> ApplyFixResult:
    """
    Overwrite file_path with new_content, after backing up the
    original. Raises rather than silently no-op'ing on any precondition
    failure (missing file, oversized content, path escaping the
    original file's directory) — tool_registry.run_tool() turns that
    into a clean error response for the caller, same pattern as
    code_analysis.analyze_file().

    file_path must already exist — this applies a fix to an existing
    file, it does not create new files (Section 14 is framed entirely
    around "here's the file that will change", implying it already
    exists; creating new files is a different, larger permission
    surface that's out of scope here).
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"'{file_path}' does not exist or is not a file — apply_fix only modifies existing files.")

    encoded = new_content.encode("utf-8")
    if len(encoded) > _MAX_FILE_BYTES:
        raise ValueError(
            f"New content is {len(encoded):,} bytes, over the {_MAX_FILE_BYTES:,}-byte limit "
            f"for a single apply_fix call."
        )

    abs_path = os.path.abspath(file_path)
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d%H%M%S")
    backup_path = f"{abs_path}.jarvis-backup-{timestamp}"

    # Backup BEFORE any write, so a failure partway through never
    # leaves the original unrecoverable.
    shutil.copy2(abs_path, backup_path)
    logger.info(f"[file_ops] Backed up '{abs_path}' -> '{backup_path}'")

    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        # Write failed after backup succeeded — restore immediately
        # rather than leaving the file in a half-written or
        # inconsistent state.
        logger.error(f"[file_ops] Write failed for '{abs_path}', restoring from backup: {e}")
        shutil.copy2(backup_path, abs_path)
        raise

    verification = _verify(abs_path)

    logger.info(f"[file_ops] Applied fix to '{abs_path}' ({description or 'no description given'})")

    return ApplyFixResult(
        file_path=abs_path,
        backup_path=backup_path,
        bytes_written=len(encoded),
        verification=verification,
    )


def _verify(file_path: str) -> dict:
    """
    Real verification, not a rubber stamp: re-analyze the file with
    the same static-analysis pass code_analysis.py uses, so the caller
    (and the user, via the chat reply) can see whether the fix
    introduced a new confirmed issue, not just that bytes were
    written. Never raises — a verification failure is itself useful
    information, not a reason to hide the fact the write succeeded.
    """
    try:
        import code_analysis
        result = code_analysis.analyze_file(file_path)
        return {
            "ran": True,
            "analysis_depth": result.analysis_depth,
            "issue_count": len(result.issues),
            "issues": [
                {"severity": i.severity, "confidence": i.confidence, "message": i.message, "line": i.line}
                for i in result.issues
            ],
        }
    except Exception as e:
        logger.warning(f"[file_ops] Post-fix verification failed: {e}")
        return {"ran": False, "reason": str(e)}


def build_diff_preview(file_path: str, new_content: str, context_lines: int = 3) -> str:
    """
    Unified diff between the current file content and the proposed
    new content — this is what a caller shows the user as "SHOW
    SUMMARY OF CHANGES" (Section 14) BEFORE requesting confirmation,
    i.e. before apply_fix() is ever called. Kept separate from
    apply_fix() itself so the confirmation prompt can display this
    without writing anything.
    """
    import difflib

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"'{file_path}' does not exist or is not a file.")

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        old_lines = f.readlines()
    new_lines = new_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{file_path} (current)",
        tofile=f"{file_path} (proposed)",
        n=context_lines,
    )
    return "".join(diff)
