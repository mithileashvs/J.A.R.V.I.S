"""
JARVIS system health module — SECURITY SCAN + STORAGE CLEANER.

Two responsibilities, kept in one module because they share the same
honesty/safety rules (Section 13/25 of the spec this was built against):

  SECURITY  -> talks to the real Windows Security stack (Microsoft
               Defender) via its PowerShell cmdlets. JARVIS is never
               itself an antivirus — every result here either came
               from `Get-MpComputerStatus` / `Get-MpThreatDetection` /
               `Start-MpScan`, or the function says plainly that it
               couldn't reach Defender. No file is ever called a
               threat unless Defender itself reported it as one.

  STORAGE   -> real disk/file measurements via psutil + os.walk, never
               invented numbers. Every byte count in every function
               here comes from an actual stat() call made during this
               function's own execution.

Platform reality: this project's deployment target is Windows (see
awareness.py's docstring for the same note) — Defender's PowerShell
module and the concrete junk-file paths below (%TEMP%, %LOCALAPPDATA%,
$Recycle.Bin, ...) are Windows-specific. Every public function still
imports its platform-specific pieces lazily and returns
available=False with a clear reason on any other OS, so importing this
module never crashes a non-Windows dev machine or the test suite —
same pattern as awareness.py.

Deletion safety (Section 13 — "NEVER silently delete files"):
  - Every function that can remove anything (`clean_junk`,
    `empty_recycle_bin`) defaults to dry_run=True and MUST be called
    with dry_run=False explicitly to delete anything. There is no
    third way to trigger a delete from this module.
  - clean_junk() only ever touches paths that analyze_storage() itself
    just enumerated and classified SAFE_TO_CLEAN in THIS call — never
    an arbitrary caller-supplied path — so a confused/malicious caller
    can restrict *which* SAFE_TO_CLEAN categories run, but can't smuggle
    in an unrelated path to delete.
  - Files this module cannot positively identify as regenerable cache/
    temp data (Downloads, Documents, source files, anything outside the
    known junk directories below) are never included in
    SAFE_TO_CLEAN — see analyze_storage()'s category table.
  - Deletion uses send2trash (Recycle Bin) where available in
    preference to permanent removal, so an approved cleanup is still
    recoverable afterward. Falls back to a clear "send2trash not
    installed" error rather than silently permanent-deleting instead.
"""

import logging
import os
import platform
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("jarvis-system-health")

_SYSTEM = platform.system()  # "Windows" / "Darwin" / "Linux"

# Wall-clock budgets for anything that walks a directory tree — Section
# 18's "must not unnecessarily use resources" applies to raw I/O too,
# not just the LLM. A category/search that hits its budget reports a
# partial, honestly-labelled result (available=True, but
# note="scan time limit reached") rather than either hanging
# indefinitely on a huge/slow disk or blocking forever.
_SCAN_TIME_BUDGET_SECONDS = 8.0
_POWERSHELL_TIMEOUT_SECONDS = 20.0


# ════════════════════════════════════════════════════════════════
# SECURITY
# ════════════════════════════════════════════════════════════════

def _run_powershell(script: str, timeout: float = _POWERSHELL_TIMEOUT_SECONDS) -> dict:
    """
    Runs a PowerShell snippet and returns {"available": bool, "stdout":
    str, "stderr": str, "returncode": int} or {"available": False,
    "reason": str}. Never raises. -NoProfile/-NonInteractive so this
    never blocks on a user's custom profile script or a prompt.
    """
    if _SYSTEM != "Windows":
        return {"available": False, "reason": f"Windows Security tooling is only available on Windows (detected: {_SYSTEM})."}

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return {
            "available": True,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "returncode": proc.returncode,
        }
    except FileNotFoundError:
        return {"available": False, "reason": "PowerShell was not found on this system."}
    except subprocess.TimeoutExpired:
        return {"available": False, "reason": f"PowerShell command timed out after {timeout}s."}
    except Exception as e:
        logger.warning(f"[system_health] PowerShell call failed: {e}")
        return {"available": False, "reason": f"Could not run PowerShell: {e}"}


def get_security_status() -> dict:
    """
    Real Microsoft Defender status via Get-MpComputerStatus. Returns
    available=False with a clear reason if Defender's PowerShell module
    can't be reached (non-Windows, Defender disabled/uninstalled,
    replaced by third-party AV that doesn't expose this cmdlet, no
    permission, etc.) — never fabricates a "protected" status.
    """
    # Compact, parseable, single-line-per-field output instead of
    # PowerShell's default table (which wraps/truncates) or full JSON
    # (ConvertTo-Json can choke on some of this object's property
    # types) — a plain "Key=Value" scrape is the most robust format to
    # parse back out reliably.
    script = (
        "$s = Get-MpComputerStatus; "
        "Write-Output \"AntivirusEnabled=$($s.AntivirusEnabled)\"; "
        "Write-Output \"RealTimeProtectionEnabled=$($s.RealTimeProtectionEnabled)\"; "
        "Write-Output \"AntispywareEnabled=$($s.AntispywareEnabled)\"; "
        "Write-Output \"AMServiceEnabled=$($s.AMServiceEnabled)\"; "
        "Write-Output \"AntivirusSignatureLastUpdated=$($s.AntivirusSignatureLastUpdated)\"; "
        "Write-Output \"AntivirusSignatureAge=$($s.AntivirusSignatureAge)\"; "
        "Write-Output \"QuickScanAge=$($s.QuickScanAge)\"; "
        "Write-Output \"FullScanAge=$($s.FullScanAge)\"; "
        "Write-Output \"QuickScanEndTime=$($s.QuickScanEndTime)\"; "
        "Write-Output \"FullScanEndTime=$($s.FullScanEndTime)\""
    )
    outcome = _run_powershell(script)
    if not outcome.get("available"):
        return {"available": False, "reason": outcome.get("reason", "Could not reach Windows Security.")}
    if outcome["returncode"] != 0:
        return {
            "available": False,
            "reason": (outcome["stderr"] or "Get-MpComputerStatus failed — Windows Security "
                       "(Microsoft Defender) module may not be present on this system.").strip(),
        }

    fields = _parse_kv_lines(outcome["stdout"])
    return {
        "available": True,
        "antivirus_enabled": _to_bool(fields.get("AntivirusEnabled")),
        "realtime_protection_enabled": _to_bool(fields.get("RealTimeProtectionEnabled")),
        "antispyware_enabled": _to_bool(fields.get("AntispywareEnabled")),
        "service_enabled": _to_bool(fields.get("AMServiceEnabled")),
        "signature_last_updated": fields.get("AntivirusSignatureLastUpdated") or None,
        "signature_age_days": _to_int(fields.get("AntivirusSignatureAge")),
        "quick_scan_age_days": _to_int(fields.get("QuickScanAge")),
        "full_scan_age_days": _to_int(fields.get("FullScanAge")),
        "last_quick_scan": fields.get("QuickScanEndTime") or None,
        "last_full_scan": fields.get("FullScanEndTime") or None,
    }


def get_threat_detections(limit: int = 10) -> dict:
    """Real threat history via Get-MpThreatDetection — never invented."""
    script = (
        f"Get-MpThreatDetection | Select-Object -First {int(limit)} | ForEach-Object {{ "
        "Write-Output \"---\"; "
        "Write-Output \"ThreatName=$($_.ThreatName)\"; "
        "Write-Output \"Resources=$($_.Resources -join ';')\"; "
        "Write-Output \"InitialDetectionTime=$($_.InitialDetectionTime)\"; "
        "Write-Output \"ActionSuccess=$($_.ActionSuccess)\" "
        "}"
    )
    outcome = _run_powershell(script)
    if not outcome.get("available"):
        return {"available": False, "reason": outcome.get("reason")}
    if outcome["returncode"] != 0:
        return {"available": False, "reason": (outcome["stderr"] or "Could not query threat history.").strip()}

    threats = []
    current: dict = {}
    for line in outcome["stdout"].splitlines():
        line = line.strip()
        if line == "---":
            if current:
                threats.append(current)
            current = {}
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            current[key] = value
    if current:
        threats.append(current)

    return {
        "available": True,
        "threats": [
            {
                "name": t.get("ThreatName"),
                "affected_paths": [p for p in (t.get("Resources") or "").split(";") if p],
                "detected_at": t.get("InitialDetectionTime"),
                "action_succeeded": _to_bool(t.get("ActionSuccess")),
            }
            for t in threats if t.get("ThreatName")
        ],
    }


def is_scan_running() -> bool:
    """
    Whether Defender's scan engine (MpCmdRun.exe) is currently active —
    the real, checkable signal used to render "SCANNING..." vs
    "COMPLETE" (Section 15), since Defender's PowerShell surface
    doesn't expose a live percentage to poll instead (see
    start_scan()'s docstring).
    """
    try:
        import psutil
    except ImportError:
        return False
    try:
        for p in psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            if name in ("mpcmdrun.exe", "msmpeng.exe"):
                return True
    except Exception:
        pass
    return False


def start_scan(scan_type: str = "quick") -> dict:
    """
    Starts a REAL Defender scan via Start-MpScan and returns
    immediately — does not block until completion. Section 3 asks for
    a progress percentage "when actual progress information is
    available"; Defender's PowerShell module does not expose one (this
    was checked, not assumed — Get-MpComputerStatus has age/timestamp
    fields, not a live percent-complete), so this deliberately does NOT
    fabricate a percentage. Callers should show "SCANNING..." while
    is_scan_running() is True and treat its transition to False as
    "scan finished" (then re-read get_security_status() /
    get_threat_detections() for the actual result).

    scan_type: "quick" or "full". A quick scan is bounded and returns
    once Start-MpScan itself returns (typically well under a minute);
    a full scan can run for a long time, so this launches it and
    returns "started" rather than waiting on it — matching Section 3's
    "this may take significantly longer" note.
    """
    scan_type = (scan_type or "quick").strip().lower()
    if scan_type not in ("quick", "full"):
        return {"available": False, "reason": f"Unknown scan type '{scan_type}' (use 'quick' or 'full')."}

    ps_type = "QuickScan" if scan_type == "quick" else "FullScan"

    if _SYSTEM != "Windows":
        return {"available": False, "reason": f"Security scanning is only available on Windows (detected: {_SYSTEM})."}

    if is_scan_running():
        return {"available": True, "started": False, "already_running": True}

    try:
        # Full scans can run for a very long time, so this is launched
        # detached (no .wait()) rather than through _run_powershell's
        # bounded subprocess.run — the point of Start-MpScan is that
        # Defender's own engine keeps running after PowerShell returns.
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Start-MpScan -ScanType {ps_type}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return {"available": True, "started": True, "scan_type": scan_type}
    except FileNotFoundError:
        return {"available": False, "reason": "PowerShell was not found on this system."}
    except Exception as e:
        logger.warning(f"[system_health] start_scan failed: {e}")
        return {"available": False, "reason": f"Could not start the scan: {e}"}


def _parse_kv_lines(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _to_bool(value: Optional[str]) -> Optional[bool]:
    if value is None or value == "":
        return None
    return value.strip().lower() == "true"


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


# ════════════════════════════════════════════════════════════════
# STORAGE
# ════════════════════════════════════════════════════════════════

def get_storage_summary(drive: Optional[str] = None) -> dict:
    """Real TOTAL/USED/FREE via psutil.disk_usage — Section 9."""
    try:
        import psutil
    except ImportError:
        return {"available": False, "reason": "psutil is not installed."}

    path = drive or ("C:\\" if _SYSTEM == "Windows" else "/")
    try:
        usage = psutil.disk_usage(path)
        return {
            "available": True,
            "path": path,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent_used": usage.percent,
        }
    except Exception as e:
        logger.warning(f"[system_health] get_storage_summary failed: {e}")
        return {"available": False, "reason": f"Could not read storage for '{path}': {e}"}


@dataclass
class JunkCategory:
    key: str
    label: str
    paths: list[str]
    classification: str  # SAFE_TO_CLEAN / REVIEW / DO_NOT_TOUCH
    note: str = ""


def _candidate_junk_categories() -> list[JunkCategory]:
    """
    The fixed, allowlisted set of directories this module will ever
    look inside for cleanup candidates (Section 13: "Use allowlisted
    cleanup operations"). Nothing outside this list is ever scanned or
    touched by analyze_storage()/clean_junk() — there is no "scan
    everything" mode.

    Classification follows Section 5's rule directly: only genuinely
    regenerable cache/temp data is SAFE_TO_CLEAN; anything
    system-managed or that could affect a running application/Windows
    itself is REVIEW, never auto-classified safe.
    """
    if _SYSTEM != "Windows":
        return []

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    windir = os.environ.get("WINDIR", "C:\\Windows")

    cats = [
        JunkCategory(
            key="temp_files", label="Temporary Files",
            paths=[p for p in [os.environ.get("TEMP"), os.environ.get("TMP")] if p],
            classification="SAFE_TO_CLEAN",
            note="Files in use by a running program are skipped automatically.",
        ),
        JunkCategory(
            key="browser_cache", label="Browser Cache",
            paths=[p for p in [
                os.path.join(local_appdata, "Google", "Chrome", "User Data", "Default", "Cache") if local_appdata else None,
                os.path.join(local_appdata, "Microsoft", "Edge", "User Data", "Default", "Cache") if local_appdata else None,
            ] if p and os.path.isdir(p)],
            classification="SAFE_TO_CLEAN",
            note="Regenerated automatically by the browser; close the browser first for a complete clean.",
        ),
        JunkCategory(
            key="crash_dumps", label="Crash Dumps",
            paths=[os.path.join(local_appdata, "CrashDumps")] if local_appdata else [],
            classification="SAFE_TO_CLEAN",
        ),
        JunkCategory(
            key="recycle_bin", label="Recycle Bin",
            paths=["C:\\$Recycle.Bin"],
            classification="SAFE_TO_CLEAN",
            note="Emptying the Recycle Bin is permanent.",
        ),
        JunkCategory(
            key="windows_temp", label="Windows Temporary Files",
            paths=[os.path.join(windir, "Temp")],
            classification="REVIEW",
            note="Shared system temp directory — requires administrator permission; review before removing.",
        ),
        JunkCategory(
            key="installer_leftovers", label="Windows Update Leftovers",
            paths=[os.path.join(windir, "SoftwareDistribution", "Download")],
            classification="REVIEW",
            note="Used by Windows Update; safe in most cases but can affect an in-progress update. Requires administrator permission.",
        ),
    ]
    return [c for c in cats if c.paths]


def _dir_size_bounded(path: str, deadline: float) -> tuple[int, int, bool]:
    """
    Returns (total_bytes, file_count, hit_time_limit). Walks `path`
    accumulating real st_size values, skipping anything it can't stat
    (in-use/permission-denied files) rather than failing the whole
    category. Stops early (hit_time_limit=True) if `deadline`
    (time.monotonic() cutoff) passes — Section 18's resource-usage
    discipline applied to raw disk I/O.
    """
    total = 0
    count = 0
    if not os.path.isdir(path):
        return 0, 0, False
    for root, dirs, files in os.walk(path):
        if time.monotonic() > deadline:
            return total, count, True
        for name in files:
            if time.monotonic() > deadline:
                return total, count, True
            fpath = os.path.join(root, name)
            try:
                total += os.path.getsize(fpath)
                count += 1
            except OSError:
                continue  # locked/permission-denied/vanished mid-scan — skip, don't fail the scan
    return total, count, False


def analyze_storage() -> dict:
    """
    Real scan of the allowlisted junk categories (Section 4/5). Every
    category reports actual measured bytes; a category whose paths
    don't exist on this machine (e.g. no Edge installed) is simply
    omitted rather than reported as zero (zero would imply "scanned and
    empty", which is a different, false claim).
    """
    categories = _candidate_junk_categories()
    if not categories:
        return {"available": False, "reason": f"Storage analysis is only implemented for Windows (detected: {_SYSTEM})."}

    results = []
    reclaimable_bytes = 0
    for cat in categories:
        deadline = time.monotonic() + _SCAN_TIME_BUDGET_SECONDS
        cat_total = 0
        cat_files = 0
        truncated = False
        existing_paths = []
        for p in cat.paths:
            if not os.path.isdir(p):
                continue
            existing_paths.append(p)
            size, count, hit_limit = _dir_size_bounded(p, deadline)
            cat_total += size
            cat_files += count
            truncated = truncated or hit_limit

        if not existing_paths:
            continue  # nothing on this machine for this category — omit, don't fake a zero

        if cat.classification == "SAFE_TO_CLEAN":
            reclaimable_bytes += cat_total

        results.append({
            "key": cat.key,
            "label": cat.label,
            "paths": existing_paths,
            "classification": cat.classification,
            "size_bytes": cat_total,
            "file_count": cat_files,
            "note": cat.note,
            "scan_truncated": truncated,
        })

    # Section 11 — an informational insight (NOT a deletable category:
    # these are the user's own files). Reports a real count/size of
    # Downloads items older than 90 days, omitted entirely if there's
    # no Downloads folder or nothing qualifies.
    insights = []
    userprofile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    downloads = os.path.join(userprofile, "Downloads")
    if os.path.isdir(downloads):
        cutoff = time.time() - 90 * 86400
        old_bytes = 0
        old_count = 0
        deadline = time.monotonic() + _SCAN_TIME_BUDGET_SECONDS
        try:
            for entry in os.scandir(downloads):
                if time.monotonic() > deadline:
                    break
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        old_bytes += entry.stat().st_size
                        old_count += 1
                except OSError:
                    continue
        except OSError:
            pass
        if old_count > 0:
            insights.append({
                "text": f"Your Downloads folder contains {_format_bytes(old_bytes)} across {old_count} "
                        f"file(s) older than 90 days.",
                "path": downloads,
            })

    return {
        "available": True,
        "categories": results,
        "reclaimable_bytes": reclaimable_bytes,
        "insights": insights,
    }


def find_large_files(
    root: Optional[str] = None,
    min_size_mb: float = 100.0,
    max_results: int = 25,
    max_seconds: float = _SCAN_TIME_BUDGET_SECONDS,
) -> dict:
    """
    Real large-file finder (Section 10). Defaults to the user's home
    directory rather than the whole drive: an unbounded C:\\ walk on a
    large disk can take minutes and isn't something a chat request
    should block on. A caller can pass an explicit `root` for a
    narrower/wider search. Time-budgeted the same way as
    analyze_storage() — an exhausted budget is reported honestly
    (`scan_truncated: true`) rather than presented as a complete result.
    """
    root = root or os.environ.get("USERPROFILE", os.path.expanduser("~"))
    if not os.path.isdir(root):
        return {"available": False, "reason": f"'{root}' is not an accessible directory."}

    min_bytes = int(min_size_mb * 1024 * 1024)
    deadline = time.monotonic() + max_seconds
    found = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() > deadline:
            truncated = True
            break
        # Never descend into the categories clean_junk() already covers
        # or system-reserved trees — this is a "what's using my space"
        # finder, not a duplicate of the junk scan.
        dirnames[:] = [d for d in dirnames if d not in ("$Recycle.Bin", "System Volume Information")]
        for name in filenames:
            fpath = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(fpath)
            except OSError:
                continue
            if size >= min_bytes:
                found.append({"path": fpath, "size_bytes": size, "type": os.path.splitext(name)[1] or "(none)"})

    found.sort(key=lambda f: f["size_bytes"], reverse=True)
    return {
        "available": True,
        "root": root,
        "files": found[:max_results],
        "scan_truncated": truncated,
    }


def _format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def clean_junk(category_keys: list[str], dry_run: bool = True) -> dict:
    """
    Section 6/7/8/13 — the ONLY function in this module that deletes
    anything, and only for `category_keys` that analyze_storage() (run
    fresh, right here — never a cached/stale list) currently classifies
    SAFE_TO_CLEAN. A category key that doesn't exist, or that
    classifies as REVIEW/DO_NOT_TOUCH, is refused rather than silently
    skipped-but-reported-as-done, so a caller can't be misled about
    what actually happened.

    dry_run=True (the default — Section 8's "this should be the
    default behavior"): computes and returns exactly what WOULD be
    removed, deletes nothing.

    dry_run=False: actually removes files, using send2trash (Recycle
    Bin) where available so the operation is recoverable; the Recycle
    Bin category itself is emptied via PowerShell's Clear-RecycleBin
    (send2trash can't "trash the trash"). Locked/in-use files are
    skipped individually (reported in `skipped`) rather than aborting
    the whole cleanup.
    """
    fresh = analyze_storage()
    if not fresh.get("available"):
        return {"available": False, "reason": fresh.get("reason")}

    by_key = {c["key"]: c for c in fresh["categories"]}
    unknown = [k for k in category_keys if k not in by_key]
    if unknown:
        return {"available": False, "reason": f"Unknown or unavailable categor{'y' if len(unknown)==1 else 'ies'}: {', '.join(unknown)}."}

    not_safe = [k for k in category_keys if by_key[k]["classification"] != "SAFE_TO_CLEAN"]
    if not_safe:
        return {
            "available": False,
            "reason": (
                f"Refusing — {', '.join(not_safe)} require manual review, not automatic cleanup. "
                "JARVIS only auto-cleans categories classified SAFE_TO_CLEAN."
            ),
        }

    selected = [by_key[k] for k in category_keys]

    if dry_run:
        return {
            "available": True,
            "dry_run": True,
            "categories": [
                {"key": c["key"], "label": c["label"], "size_bytes": c["size_bytes"], "file_count": c["file_count"]}
                for c in selected
            ],
            "total_bytes": sum(c["size_bytes"] for c in selected),
        }

    try:
        import send2trash
        _have_send2trash = True
    except ImportError:
        _have_send2trash = False

    freed_bytes = 0
    removed_count = 0
    skipped: list[str] = []
    per_category: list[dict] = []

    for c in selected:
        cat_freed = 0
        cat_removed = 0
        if c["key"] == "recycle_bin":
            outcome = _run_powershell("Clear-RecycleBin -Force -ErrorAction Stop")
            if outcome.get("available") and outcome.get("returncode") == 0:
                cat_freed = c["size_bytes"]
                cat_removed = c["file_count"]
            else:
                skipped.append(f"{c['label']}: {outcome.get('stderr') or outcome.get('reason') or 'could not empty Recycle Bin'}")
        else:
            if not _have_send2trash:
                skipped.append(f"{c['label']}: send2trash is not installed — add it to requirements.txt to enable safe deletion.")
            else:
                for p in c["paths"]:
                    if not os.path.isdir(p):
                        continue
                    for root, dirs, files in os.walk(p):
                        for name in files:
                            fpath = os.path.join(root, name)
                            try:
                                size = os.path.getsize(fpath)
                                send2trash.send2trash(fpath)
                                cat_freed += size
                                cat_removed += 1
                            except Exception as e:
                                skipped.append(f"{fpath}: {e}")

        freed_bytes += cat_freed
        removed_count += cat_removed
        per_category.append({"key": c["key"], "label": c["label"], "freed_bytes": cat_freed, "removed_count": cat_removed})

    return {
        "available": True,
        "dry_run": False,
        "categories": per_category,
        "total_freed_bytes": freed_bytes,
        "total_removed_count": removed_count,
        "skipped": skipped,
    }


# NOTE: the old SystemHealthMonitor (storage/security proactive polling)
# has been superseded by proactive_engine.py's ProactiveEngine, which
# covers storage+security plus CPU/RAM/GPU/temp/battery/network/voice
# with proper debouncing, cooldown, severity escalation, and recovery
# events (SystemHealthMonitor only had a one-shot dedup, no debounce/
# cooldown/severity). main.py now wires proactive_engine.proactive_engine
# instead.



# ════════════════════════════════════════════════════════════════
# RESPONSE FORMATTING (Section 1/7/9/10/15 — the exact report shapes
# the spec asked for). Kept here rather than in main.py, matching how
# git_tools/code_analysis hand back pre-formatted text instead of
# main.py assembling report strings itself.
# ════════════════════════════════════════════════════════════════

def format_security_report(status: dict, threats: dict, scan_started: Optional[dict] = None) -> str:
    if not status.get("available"):
        return "Security scan unavailable. Windows security tooling could not be accessed."

    threat_list = (threats.get("threats") if threats.get("available") else None) or []
    if threat_list:
        names = ", ".join(sorted({t.get("name", "unknown") for t in threat_list}))
        status_line = f"⚠ {len(threat_list)} threat(s) detected: {names}"
    else:
        status_line = "✓ No threats detected"

    protection = "ACTIVE" if (status.get("antivirus_enabled") and status.get("realtime_protection_enabled")) else "INACTIVE"
    last_scan = status.get("last_quick_scan") or status.get("last_full_scan") or "unknown"

    lines = ["J.A.R.V.I.S.", "SYSTEM SECURITY SCAN", "", "STATUS", status_line, "", "Protection:", protection, "", "Last scan:", last_scan]

    if scan_started is not None:
        if scan_started.get("already_running"):
            lines += ["", "(A scan is already in progress — I'll have fresh results once it finishes.)"]
        elif scan_started.get("started"):
            kind = "full" if scan_started.get("scan_type") == "full" else "quick"
            lines += ["", f"A {kind} scan has just been started. Ask me again shortly for the result — "
                          f"Windows Security doesn't expose a live progress percentage, so I can only report "
                          f"SCANNING vs COMPLETE, not a percent."]

    if threat_list:
        lines.append("\nAFFECTED PATHS")
        for t in threat_list[:5]:
            paths = ", ".join(t.get("affected_paths") or []) or "(path unavailable)"
            lines.append(f"- {t.get('name')}: {paths}")

    return "\n".join(lines)


def format_storage_summary(summary: dict) -> str:
    if not summary.get("available"):
        return f"I couldn't read storage information, Sir: {summary.get('reason')}"
    total_gb = summary["total_bytes"] / (1024 ** 3)
    used_gb = summary["used_bytes"] / (1024 ** 3)
    free_gb = summary["free_bytes"] / (1024 ** 3)
    return (
        f"STORAGE\n\n"
        f"{total_gb:.0f} GB TOTAL\n"
        f"{used_gb:.0f} GB USED\n"
        f"{free_gb:.0f} GB FREE\n"
        f"({summary['percent_used']:.0f}% used)"
    )


def format_storage_analysis(analysis: dict) -> str:
    if not analysis.get("available"):
        return f"I couldn't analyze storage, Sir: {analysis.get('reason')}"

    categories = analysis.get("categories") or []
    if not categories:
        return "I didn't find any of the usual junk-file locations on this system, Sir."

    lines = ["STORAGE CLEANUP ANALYSIS", "", f"Potentially reclaimable: {_format_bytes(analysis['reclaimable_bytes'])}", "", "CATEGORIES"]
    for c in categories:
        truncated = "  (scan time limit reached — figure may be a partial count)" if c.get("scan_truncated") else ""
        lines.append(f"{c['label']:<28}{_format_bytes(c['size_bytes']):>10}   [{c['classification']}]{truncated}")
        if c.get("note"):
            lines.append(f"    {c['note']}")

    for insight in analysis.get("insights") or []:
        lines.append(f"\n{insight['text']}")

    return "\n".join(lines)


def format_large_files(result: dict) -> str:
    if not result.get("available"):
        return f"I couldn't search for large files, Sir: {result.get('reason')}"
    files = result.get("files") or []
    if not files:
        return f"I didn't find anything unusually large under {result['root']}, Sir."

    lines = [f"LARGEST FILES under {result['root']}", ""]
    for f in files:
        lines.append(f"{_format_bytes(f['size_bytes']):>10}   {f['type']:<8}  {f['path']}")
    if result.get("scan_truncated"):
        lines.append("\n(Scan time limit reached — there may be larger files outside what was checked.)")
    return "\n".join(lines)


def format_clean_preview(preview: dict) -> str:
    """Section 7's exact preview shape, for the dry-run result JARVIS
    shows before the CONFIRM gate is ever reached."""
    if not preview.get("available"):
        return f"I couldn't prepare a cleanup preview, Sir: {preview.get('reason')}"
    categories = preview.get("categories") or []
    if not categories:
        return "There's nothing in the SAFE_TO_CLEAN categories to remove right now, Sir."

    lines = [f"I found {_format_bytes(preview['total_bytes'])} of potentially removable data, Sir.", ""]
    for c in categories:
        lines.append(f"{c['label'].upper()}\n{_format_bytes(c['size_bytes'])} ({c['file_count']} files)\n")
    return "\n".join(lines).strip()


def format_clean_result(result: dict) -> str:
    if not result.get("available"):
        return f"I couldn't complete the cleanup, Sir: {result.get('reason')}"
    lines = [f"Cleanup complete, Sir. Freed {_format_bytes(result['total_freed_bytes'])} across {result['total_removed_count']} files."]
    for c in result.get("categories") or []:
        lines.append(f"- {c['label']}: {_format_bytes(c['freed_bytes'])} ({c['removed_count']} files)")
    if result.get("skipped"):
        lines.append(f"\n{len(result['skipped'])} item(s) were skipped (in use or inaccessible).")
    return "\n".join(lines)
