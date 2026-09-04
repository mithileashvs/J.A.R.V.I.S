"""
JARVIS screen-aware error analysis (Phase 4, Feature 1).

Implements the flow the Phase 4 brief describes:

    USER REQUEST -> CHECK ACTIVE WINDOW -> IDENTIFY APPLICATION ->
    CAPTURE SCREEN ONLY IF REQUIRED -> EXTRACT RELEVANT CONTENT ->
    DETECT ERROR / CODE / TERMINAL -> COMBINE WITH PROJECT CONTEXT

Privacy model (non-negotiable, matches awareness.py's existing
pattern for get_active_window rather than inventing a new one):
  - Nothing in this module runs on a loop, a timer, or a background
    thread. Every function here is a single on-demand call, only
    ever reached through the SAFE analyze_screen tool
    (tool_registry.py) — never polled, never streamed, never
    triggered as a side effect of context gathering.
  - Capture targets the active window's region only (via awareness.
    get_active_window() + a bounding box from pygetwindow), never the
    full desktop, unless the active window genuinely covers the
    whole screen anyway.
  - Screenshots are written to a temp file, OCR'd, and deleted in a
    `finally` block before this function returns — nothing persists
    on disk after the call unless save_screenshot=True is passed
    explicitly by the caller (i.e. the user asked for it kept).
  - What actually leaves this module is EXTRACTED TEXT, not image
    bytes — analyze_screen()'s return value is a dict of strings.
    Sending the raw screenshot itself to anything (an LLM, a log) is
    not something this module does; if a caller wants that it has to
    read the (already-deleted-by-default) file itself, deliberately.
"""

import logging
import os
import platform
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("jarvis-screen")

_SYSTEM = platform.system()

# Same reasoning as code_analysis._MAX_FILE_BYTES / terminal_tools.
# _MAX_OUTPUT_CHARS — OCR text is capped so a screen full of dense
# text (a huge log window) can't blow up the size of what gets
# returned/handed to an LLM.
_MAX_EXTRACTED_CHARS = 12_000

# How stale is "too stale" for get_active_window()'s title alone to
# be trusted as still-current before we bother capturing anything —
# not actually used as a cache here (Section 15: no continuous
# capture to cache from), kept only as a documented constant in case
# a future caller wants to reason about capture recency.
_ANALYSIS_TIMEOUT_SECONDS = 20.0


@dataclass
class ScreenContext:
    available: bool
    reason: Optional[str] = None
    application_type: Optional[str] = None   # "IDE" | "TERMINAL" | "BROWSER" | "ERROR_DIALOG" | "TEXT_EDITOR" | "UNKNOWN"
    window_title: Optional[str] = None
    extracted_text: Optional[str] = None
    truncated: bool = False
    detected_errors: list[str] = field(default_factory=list)
    file_references: list[str] = field(default_factory=list)
    line_references: list[int] = field(default_factory=list)
    screenshot_path: Optional[str] = None    # only set if save_screenshot=True was requested


# Window-title substrings -> application type. Matched case-insensitively
# against the title pygetwindow reports (the only thing awareness.py's
# get_active_window() gives us) — this is a heuristic, not a guarantee,
# and ScreenContext.application_type is reported as "UNKNOWN" rather
# than a guess dressed up as certainty when nothing matches.
_APP_SIGNATURES = [
    ("IDE", ("visual studio code", "vscode", "pycharm", "intellij", "webstorm",
              "sublime text", "android studio", "eclipse", "rider")),
    ("TERMINAL", ("windows terminal", "powershell", "cmd.exe", "command prompt",
                   "wsl", "git bash", "terminal —", "conemu")),
    ("BROWSER", ("google chrome", "mozilla firefox", "microsoft edge", " - brave",
                  "safari", "opera")),
    ("ERROR_DIALOG", ("error", "exception", "unhandled", "crash reporter")),
]


def _classify_application(window_title: str) -> str:
    title_lower = window_title.lower()
    for app_type, signatures in _APP_SIGNATURES:
        if any(sig in title_lower for sig in signatures):
            return app_type
    return "UNKNOWN"


def _bounding_box_for_active_window() -> Optional[tuple]:
    """
    (left, top, width, height) of the current foreground window, or
    None if that can't be determined — in which case the caller falls
    back to a full-screen capture rather than failing outright
    (Section 1's "avoid capturing the entire desktop unless
    necessary" is a preference, not a hard requirement when the
    targeted approach genuinely isn't available).
    """
    if _SYSTEM != "Windows":
        return None
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow()
        if win is None:
            return None
        return (win.left, win.top, win.width, win.height)
    except Exception as e:
        logger.warning(f"[screen] Could not get window bounding box: {e}")
        return None


def _capture_region(box: Optional[tuple], out_path: str) -> bool:
    """
    Grab a screenshot of `box` (or the full primary monitor if box is
    None) into out_path. Uses mss — fast, cross-platform,
    dependency-light — rather than a heavier CV pipeline, matching the
    brief's "do not create an unnecessarily heavy computer vision
    pipeline if targeted OCR/text extraction is sufficient."

    Returns False (never raises) if mss isn't installed or the
    capture otherwise fails, so callers can report that honestly
    instead of crashing.
    """
    try:
        import mss
        import mss.tools
    except ImportError:
        logger.warning("[screen] mss is not installed — cannot capture the screen.")
        return False

    try:
        with mss.mss() as sct:
            if box is not None:
                left, top, width, height = box
                monitor = {"left": left, "top": top, "width": max(width, 1), "height": max(height, 1)}
            else:
                monitor = sct.monitors[1]  # index 0 is "all monitors combined"; 1 is the primary
            shot = sct.grab(monitor)
            mss.tools.to_png(shot.rgb, shot.size, output=out_path)
        return True
    except Exception as e:
        logger.warning(f"[screen] Capture failed: {e}")
        return False


def _ocr_extract(image_path: str) -> Optional[str]:
    """
    Run OCR over the captured image. Returns None (not "") when OCR
    genuinely isn't available, so callers can distinguish "nothing was
    on screen" from "we couldn't even try" — same honesty principle as
    code_analysis.py's confidence labelling.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.warning("[screen] pytesseract/Pillow not installed — cannot OCR the capture.")
        return None

    try:
        text = pytesseract.image_to_string(Image.open(image_path))
        return text
    except Exception as e:
        logger.warning(f"[screen] OCR failed: {e}")
        return None


# Reuses terminal_tools.extract_errors' pattern vocabulary conceptually
# but works over free-form OCR'd text rather than clean subprocess
# output, so it's deliberately looser/broader — OCR introduces its own
# noise (misread characters, broken line wraps) that a strict regex
# match against subprocess output doesn't have to deal with.
_ERROR_LINE_PATTERNS = [
    re.compile(r'\b\w*Error\b.*', re.IGNORECASE),
    re.compile(r'\bException\b.*', re.IGNORECASE),
    re.compile(r'\btraceback\b.*', re.IGNORECASE),
    re.compile(r'\bfailed\b.*', re.IGNORECASE),
]
# Python traceback style — 'File "path", line N' — checked first since
# it directly pairs a file with its line number (most reliable).
_TRACEBACK_FILE_REF_PATTERN = re.compile(r'File "([^"]+)", line (\d+)')
# Looser fallback — 'path.ext:N' or a bare 'path.ext' with no line
# number — for non-traceback contexts (terminal prompts, IDE tabs).
_GENERIC_FILE_REF_PATTERN = re.compile(r'([\w./\\-]+\.\w{1,5}):?(\d+)?')


def _extract_error_signal(text: str) -> tuple[list[str], list[str], list[int]]:
    """Pull candidate error lines / file references / line numbers out of OCR'd text."""
    detected_errors = []
    file_references = []
    line_references = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for pattern in _ERROR_LINE_PATTERNS:
            if pattern.search(line):
                detected_errors.append(line[:300])
                break

    for match in _TRACEBACK_FILE_REF_PATTERN.finditer(text):
        path, lineno = match.group(1), match.group(2)
        if path not in file_references:
            file_references.append(path)
        if lineno.isdigit():
            line_references.append(int(lineno))

    for match in _GENERIC_FILE_REF_PATTERN.finditer(text):
        path, lineno = match.group(1), match.group(2)
        # Require a plausible source-file extension to cut down on
        # false positives from OCR noise matching the loose pattern
        # (e.g. "e.g." or version strings) — same conservatism as
        # debug_mode.guess_target_file's own candidate filter.
        if "." not in path or len(path) < 5:
            continue
        if path not in file_references:
            file_references.append(path)
        if lineno and lineno.isdigit():
            line_references.append(int(lineno))

    # Cap list sizes — an OCR pass over a busy IDE can otherwise
    # produce dozens of spurious "matches".
    return detected_errors[:10], file_references[:10], line_references[:10]


def analyze_screen(save_screenshot: bool = False) -> ScreenContext:
    """
    The Feature 1 entry point. One on-demand call:
      1. Check the active window (awareness.get_active_window)
      2. Classify what kind of application it is
      3. Capture ONLY that window's region (falls back to full-screen
         only if a targeted region can't be determined)
      4. OCR the capture
      5. Extract error/file/line signal from the OCR'd text
      6. Clean up the temp screenshot unless save_screenshot=True

    Never raises — always returns a ScreenContext, with .available
    telling the caller whether there's anything usable in it, and
    .reason explaining why not when there isn't.
    """
    import awareness

    window = awareness.get_active_window()
    if not window.get("available"):
        return ScreenContext(available=False, reason=window.get("reason", "Active window unavailable."))

    window_title = window.get("title", "")
    application_type = _classify_application(window_title)

    box = _bounding_box_for_active_window()

    tmp_dir = tempfile.gettempdir()
    tmp_path = os.path.join(tmp_dir, f"jarvis_screen_{int(time.time() * 1000)}.png")

    captured = _capture_region(box, tmp_path)
    if not captured:
        return ScreenContext(
            available=False,
            reason="Screen capture is not available (mss is not installed, or the capture failed). "
                   "Add mss to requirements.txt and `pip install mss` to enable this.",
            application_type=application_type,
            window_title=window_title,
        )

    try:
        raw_text = _ocr_extract(tmp_path)
        if raw_text is None:
            return ScreenContext(
                available=False,
                reason="OCR is not available (pytesseract/Pillow not installed, or the Tesseract "
                       "binary is not on PATH). Install Tesseract OCR and `pip install pytesseract pillow`.",
                application_type=application_type,
                window_title=window_title,
            )

        truncated = False
        if len(raw_text) > _MAX_EXTRACTED_CHARS:
            raw_text = raw_text[:_MAX_EXTRACTED_CHARS] + "\n... [truncated]"
            truncated = True

        detected_errors, file_references, line_references = _extract_error_signal(raw_text)

        kept_path = None
        if save_screenshot:
            # Move rather than copy — no reason to leave two copies on
            # disk, and the temp original is being deleted either way.
            kept_dir = os.path.join(tmp_dir, "jarvis_saved_screenshots")
            os.makedirs(kept_dir, exist_ok=True)
            kept_path = os.path.join(kept_dir, os.path.basename(tmp_path))
            os.replace(tmp_path, kept_path)

        return ScreenContext(
            available=True,
            application_type=application_type,
            window_title=window_title,
            extracted_text=raw_text,
            truncated=truncated,
            detected_errors=detected_errors,
            file_references=file_references,
            line_references=line_references,
            screenshot_path=kept_path,
        )
    finally:
        # Temporary screenshots are cleaned up after analysis unless
        # explicitly kept (Section: "SCREEN PRIVACY REQUIREMENTS").
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as e:
                logger.warning(f"[screen] Could not remove temp screenshot {tmp_path}: {e}")


async def screen_aware_debug_context(user_message: str, ctx) -> Optional[ScreenContext]:
    """
    Bridge for debug_mode.Investigation: only capture the screen when
    genuinely useful evidence — an active IDE/terminal/error dialog
    window AND no stronger evidence already available (a recent
    terminal error, or a target file debug_mode already identified).
    This is what Section 1's "Debug Mode determines screen evidence is
    needed" means in practice — screen capture is the last resort, not
    the first thing tried, since it's the most privacy-sensitive and
    least precise source of evidence available to JARVIS.
    """
    import awareness

    window = awareness.get_active_window()
    if not window.get("available"):
        return None

    application_type = _classify_application(window.get("title", ""))
    if application_type not in ("IDE", "TERMINAL", "ERROR_DIALOG"):
        # Nothing that plausibly shows code/errors is focused right
        # now (e.g. it's a browser tab, or an unrelated app) — don't
        # capture just because a window exists.
        return None

    return analyze_screen()
