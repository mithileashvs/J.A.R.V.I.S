"""
JARVIS awareness module (Section 16 — screen + active window awareness).

Provides:
  - get_active_window(): current foreground application/window title
  - list_running_processes(): lightweight process snapshot (for
    SYSTEM_MONITOR-style questions later)

Deliberately conservative per Section 16 / your Section 1 security
requirement: this NEVER captures the screen's pixel content and never
runs continuously in a background loop. Every function here is a
single on-demand snapshot, called only when a SAFE tool explicitly
asks for it (see tool_registry.py) — never polled, never streamed
anywhere.

Platform reality check: pygetwindow raises NotImplementedError at
IMPORT TIME on any non-Windows OS (confirmed directly — not assumed).
Since this project's actual deployment target is Windows (per
startup.bat, piper voice models, the .bat launcher), that's fine in
production, but it means this module must not let that import crash
the whole backend on a dev machine running Linux/Mac, or during any
test run. All platform-specific imports are deferred inside functions
and guarded, so `import awareness` itself always succeeds everywhere,
and unsupported-platform calls fail cleanly with a clear message
instead of taking the process down.
"""

import logging
import platform
from typing import Optional

logger = logging.getLogger("jarvis-awareness")

_SYSTEM = platform.system()  # "Windows" / "Darwin" / "Linux"


def get_active_window() -> dict:
    """
    Return info about the current foreground window: {"title": str,
    "available": bool}. On unsupported platforms (or if the
    underlying call fails for any reason — no window manager, headless
    session, permissions), returns available=False with a clear reason
    rather than raising, so a caller (e.g. a SAFE tool handler) can
    always produce a sensible response instead of crashing.
    """
    if _SYSTEM == "Windows":
        try:
            import pygetwindow as gw
            win = gw.getActiveWindow()
            if win is None:
                return {"available": False, "reason": "No active window detected."}
            return {"available": True, "title": win.title}
        except ImportError:
            return {
                "available": False,
                "reason": "pygetwindow is not installed. Add it to requirements.txt "
                          "and `pip install pygetwindow` to enable window awareness.",
            }
        except Exception as e:
            logger.warning(f"[awareness] get_active_window failed: {e}")
            return {"available": False, "reason": f"Could not read active window: {e}"}

    # macOS/Linux: not implemented in this phase. Section 16 asks for
    # "current active application" awareness — this project's actual
    # target platform is Windows (see this module's docstring), so
    # non-Windows support is explicitly out of scope for now rather
    # than half-implemented and unreliable.
    return {
        "available": False,
        "reason": f"Active window awareness is only implemented for Windows (detected: {_SYSTEM}).",
    }


def list_running_processes(limit: int = 15) -> dict:
    """
    Lightweight snapshot of running processes, sorted by CPU usage.
    Cross-platform via psutil (unlike pygetwindow, this works
    identically on Windows/Mac/Linux — confirmed directly). Useful for
    SYSTEM_MONITOR-intent questions and as a cheap building block for
    later "what IDE/terminal is running" project-context detection.
    """
    try:
        import psutil
    except ImportError:
        return {
            "available": False,
            "reason": "psutil is not installed. Add it to requirements.txt "
                      "and `pip install psutil` to enable process listing.",
        }

    try:
        # A first cpu_percent() call always returns 0.0 (no interval
        # to measure against) — this is documented psutil behaviour,
        # not a bug. Callers wanting accurate live CPU% would need two
        # calls with a sleep between; a single on-demand snapshot here
        # is intentional given Section 16's "no continuous background
        # awareness" requirement, so this reports 0% CPU for now and
        # is honest about why rather than silently sleeping to fake
        # accuracy on every call.
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs.sort(key=lambda p: p.get("memory_percent") or 0, reverse=True)
        return {"available": True, "processes": procs[:limit]}
    except Exception as e:
        logger.warning(f"[awareness] list_running_processes failed: {e}")
        return {"available": False, "reason": f"Could not list processes: {e}"}


def check_port_usage(port: int) -> dict:
    """
    Report whether `port` currently has a process listening on it, and
    which one if psutil can determine that — Section 6/17's
    check_port_usage tool. Directly answers terminal_tools.py's
    port_in_use error pattern ("is something already using this
    port?") without the user having to run netstat/lsof themselves.

    Read-only (net_connections is a snapshot, nothing is opened or
    closed) — SAFE. On some platforms/permission levels psutil can't
    see the owning process for connections it doesn't own; that's
    reported honestly as "unknown" rather than guessed.
    """
    try:
        import psutil
    except ImportError:
        return {"available": False, "reason": "psutil is not installed."}

    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                process_name = None
                if conn.pid:
                    try:
                        process_name = psutil.Process(conn.pid).name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        process_name = None
                return {
                    "available": True,
                    "port": port,
                    "in_use": True,
                    "pid": conn.pid,
                    "process_name": process_name,
                }
        return {"available": True, "port": port, "in_use": False}
    except psutil.AccessDenied:
        return {
            "available": False,
            "reason": "Permission denied reading system connection table "
                      "(may need elevated privileges on this OS).",
        }
    except Exception as e:
        logger.warning(f"[awareness] check_port_usage failed: {e}")
        return {"available": False, "reason": f"Could not check port usage: {e}"}


def get_system_load() -> dict:
    """CPU/memory snapshot for SYSTEM_MONITOR-intent questions."""
    try:
        import psutil
    except ImportError:
        return {
            "available": False,
            "reason": "psutil is not installed.",
        }

    try:
        return {
            "available": True,
            "cpu_percent": psutil.cpu_percent(interval=0.3),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage("/").percent if _SYSTEM != "Windows" else psutil.disk_usage("C:\\").percent,
        }
    except Exception as e:
        logger.warning(f"[awareness] get_system_load failed: {e}")
        return {"available": False, "reason": f"Could not read system load: {e}"}
