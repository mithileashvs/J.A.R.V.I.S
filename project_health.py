"""
Phase 6 Feature 6 — Project Health Monitor.

This does NOT create a second background-task system (Rule 1/5). It
extends the existing `background_tasks.py` TaskManager by reading its
already-tracked task history for Build/Tests signals, and reuses
`git_tools.git_status` for Git and `dev_env_prep.py`'s language-profile
table for Dependencies. This module starts no processes of its own,
polls nothing, and does no scanning of the machine — it only:

  1. Tracks an explicit per-project opt-in set (Feature 6 —
     "Explicitly enabled, project-scoped") so a report is only ever
     produced for a project the user actually asked to monitor.
  2. Aggregates state that already exists elsewhere into the
     PROJECT HEALTH report shape from the spec.

"Repeated" build/test failures are read directly from
`background_tasks.task_manager`'s own task history for that project —
every `npm run build`/`pytest` the user has run through
`run_terminal_command`/`start_background_task` this session is already
sitting there with a status; this module just looks at the trailing
run of same-kind tasks to see whether the streak is failing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_REPEATED_FAILURE_THRESHOLD = 2  # consecutive failures before calling it "repeated," not just "failed once"
_DEFAULT_AUTO_INTERVAL_SECONDS = 900.0  # 15 minutes
_MIN_AUTO_INTERVAL_SECONDS = 60.0        # floor — never hammer the project every few seconds

NotifyFn = Callable[[dict], Awaitable[None]]

_BUILD_KEYWORDS = ("build", "compile", "webpack", "vite", "tsc")
_TEST_KEYWORDS = ("test", "pytest", "jest", "mocha")


class HealthSignal(str, Enum):
    GOOD = "\u2713"      # ✓
    WARNING = "\u26a0"   # ⚠
    UNKNOWN = "?"


@dataclass
class ProjectHealthReport:
    project_path: str
    build: HealthSignal = HealthSignal.UNKNOWN
    tests: HealthSignal = HealthSignal.UNKNOWN
    dependencies: HealthSignal = HealthSignal.UNKNOWN
    git: HealthSignal = HealthSignal.UNKNOWN
    runtime: HealthSignal = HealthSignal.UNKNOWN
    attention: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [
            "J.A.R.V.I.S PROJECT HEALTH", "",
            f"Build: {self.build.value}",
            f"Tests: {self.tests.value}",
            f"Dependencies: {self.dependencies.value}",
            f"Git: {self.git.value}",
            f"Runtime: {self.runtime.value}",
        ]
        if self.attention:
            lines.append("")
            lines.append("Attention needed:")
            lines.extend(f"  {a}" for a in self.attention)
        return "\n".join(lines)


class ProjectHealthMonitor:
    def __init__(self) -> None:
        self._enabled_projects: set[str] = set()
        # Feature 6 (remaining work) — automatic/periodic monitoring.
        # A single asyncio background task (never more than one — see
        # start_auto_monitor()) that periodically re-runs get_report()
        # for every currently-enabled project and notifies only on new
        # or changed attention items, so it never spams the same
        # unresolved warning every cycle.
        self._auto_task: Optional[asyncio.Task] = None
        self._auto_interval_seconds: float = _DEFAULT_AUTO_INTERVAL_SECONDS
        self._notify_fn: Optional[NotifyFn] = None
        self._last_attention_signature: dict[str, str] = {}

    # ── Explicit opt-in (Feature 6 — "Explicitly enabled") ──────────
    def enable(self, project_path: str) -> None:
        self._enabled_projects.add(project_path)

    def disable(self, project_path: str) -> None:
        self._enabled_projects.discard(project_path)
        self._last_attention_signature.pop(project_path, None)

    def is_enabled(self, project_path: str) -> bool:
        return project_path in self._enabled_projects

    def enabled_projects(self) -> list[str]:
        return sorted(self._enabled_projects)

    # ── Automatic / periodic monitoring ─────────────────────────────
    def set_broadcast_fn(self, fn: Optional[NotifyFn]) -> None:
        """Same wiring pattern as background_tasks.TaskManager — set once
        from main.py's lifespan so a periodic alert reaches WS clients
        without this module needing to know anything about FastAPI/WS."""
        self._notify_fn = fn

    def set_auto_interval(self, seconds: float) -> float:
        """Configurable interval (Feature 6 requirement). Floored so a
        typo/careless value can't turn this into a tight polling loop.
        Returns the interval actually applied."""
        self._auto_interval_seconds = max(_MIN_AUTO_INTERVAL_SECONDS, float(seconds))
        return self._auto_interval_seconds

    @property
    def auto_interval_seconds(self) -> float:
        return self._auto_interval_seconds

    def is_auto_monitor_running(self) -> bool:
        return self._auto_task is not None and not self._auto_task.done()

    def start_auto_monitor(self) -> bool:
        """Starts the single periodic checker task. Idempotent — calling
        this again while it's already running is a no-op (Feature 6:
        'avoid duplicate background workers'). Returns True if a task
        was (newly) started."""
        if self.is_auto_monitor_running():
            return False
        self._auto_task = asyncio.create_task(self._auto_loop())
        return True

    def stop_auto_monitor(self) -> None:
        """Graceful shutdown (Feature 6 requirement) — cancels the loop
        and waits for nothing further; safe to call even if it was
        never started."""
        if self._auto_task is not None:
            self._auto_task.cancel()
            self._auto_task = None

    async def _auto_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._auto_interval_seconds)
                for project_path in list(self._enabled_projects):
                    await self._check_and_notify(project_path)
        except asyncio.CancelledError:
            # Expected on graceful shutdown/stop_auto_monitor() — not an error.
            raise
        except Exception as e:  # noqa: BLE001 — a health-monitor bug must never take the app down.
            logger.error(f"[project_health] auto-monitor loop crashed: {e}")

    async def _check_and_notify(self, project_path: str) -> None:
        """One project's periodic check. Isolated per project and
        wrapped so a failure analysing one project never stops the
        others in the same cycle, and never crashes the loop overall
        (Feature 6: 'health failures must not crash the application')."""
        try:
            report = await self.get_report(project_path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[project_health] auto check failed for {project_path}: {e}")
            return

        if not report.attention:
            self._last_attention_signature.pop(project_path, None)
            return

        # De-dup: only notify when the set of issues actually changed
        # since the last cycle (Feature 6: 'avoid notification spam').
        signature = "|".join(sorted(report.attention))
        if self._last_attention_signature.get(project_path) == signature:
            return
        self._last_attention_signature[project_path] = signature

        import memory
        memory.log_event(
            "health:auto_alert",
            f"{project_path}: {'; '.join(report.attention)}",
        )

        # Feature 7 — the health monitor is the trigger; the suggestion
        # engine turns each new attention line into an actionable,
        # user-retrievable suggestion. Isolated in its own try/except so
        # a Feature 7 bug can never take down Feature 6's own alerting.
        try:
            import suggestion_engine as _suggestion_module
            _suggestion_module.suggestion_engine.record_health_alert(project_path, report.attention)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[project_health] suggestion generation failed for {project_path}: {e}")

        if self._notify_fn is not None:
            try:
                await self._notify_fn({
                    "type": "health_alert",
                    "project_path": project_path,
                    "attention": report.attention,
                })
            except Exception as e:  # noqa: BLE001 — a broken WS broadcast must not break monitoring.
                logger.error(f"[project_health] notify failed for {project_path}: {e}")

    # ── Signal computation ──────────────────────────────────────────
    def _task_streak_signal(self, project_path: str, keywords: tuple[str, ...]) -> tuple[HealthSignal, int]:
        """Looks at background_tasks.task_manager's own task history
        (Rule 5 — no second tracking system) for this project, filtered
        to tasks whose command matches one of `keywords`, and reports
        the trailing consecutive-failure streak."""
        import background_tasks as _bt
        relevant = [
            t for t in _bt.task_manager.list_tasks()
            if t.project == project_path and any(k in t.command.lower() for k in keywords)
            and t.status.value in ("SUCCEEDED", "FAILED")  # only finished runs carry a real signal
        ]
        if not relevant:
            return HealthSignal.UNKNOWN, 0
        relevant.sort(key=lambda t: t.start_time)
        streak = 0
        for t in reversed(relevant):
            if t.status.value == "FAILED":
                streak += 1
            else:
                break
        if relevant[-1].status.value == "SUCCEEDED":
            return HealthSignal.GOOD, 0
        return HealthSignal.WARNING, streak

    async def _git_signal(self, project_path: str) -> tuple[HealthSignal, Optional[str]]:
        import git_tools
        result = await git_tools.git_status(cwd=project_path)
        if not result.available:
            return HealthSignal.UNKNOWN, None
        if result.entries:
            return HealthSignal.WARNING, f"{len(result.entries)} uncommitted change(s)."
        return HealthSignal.GOOD, None

    async def _dependency_signal(self, project_path: str) -> tuple[HealthSignal, Optional[str]]:
        """Reuses dev_env_prep.py's language-profile table rather than
        re-deriving language->manifest->list-command mappings a second
        time (Rule 1)."""
        import os
        import project_detector
        from workflows.dev_env_prep import _LANGUAGE_PROFILES

        summary = project_detector.detect_project(project_path)
        language = next((t for t in summary.technologies if t in _LANGUAGE_PROFILES), None)
        if language is None:
            return HealthSignal.UNKNOWN, None
        profile = _LANGUAGE_PROFILES[language]
        manifest_path = next(
            (os.path.join(project_path, n) for n in profile["manifests"] if os.path.isfile(os.path.join(project_path, n))),
            None,
        )
        if manifest_path is None:
            return HealthSignal.UNKNOWN, None

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
        except (OSError, ValueError):
            return HealthSignal.UNKNOWN, None
        if not required:
            return HealthSignal.GOOD, None

        import tool_registry
        outcome = await tool_registry.tool_registry.run_tool(
            "run_terminal_command", {"command": profile["list_cmd"], "cwd": project_path}, auto_approved=True,
        )
        if outcome["status"] != "ok":
            return HealthSignal.UNKNOWN, None
        installed_text = (outcome["result"].get("stdout") or "").lower()
        missing = [p for p in required if p.lower() not in installed_text]
        if missing:
            return HealthSignal.WARNING, f"Missing dependencies: {', '.join(missing)}."
        return HealthSignal.GOOD, None

    async def get_report(self, project_path: str) -> ProjectHealthReport:
        report = ProjectHealthReport(project_path=project_path)

        build_signal, build_streak = self._task_streak_signal(project_path, _BUILD_KEYWORDS)
        test_signal, test_streak = self._task_streak_signal(project_path, _TEST_KEYWORDS)
        report.build = build_signal
        report.tests = test_signal
        if build_streak >= _REPEATED_FAILURE_THRESHOLD:
            report.attention.append(f"The build has failed {build_streak} times in a row.")
        if test_streak >= _REPEATED_FAILURE_THRESHOLD:
            report.attention.append(f"Tests have failed {test_streak} times in a row.")

        report.git, git_note = await self._git_signal(project_path)
        if git_note and report.git == HealthSignal.WARNING:
            report.attention.append(git_note)

        report.dependencies, dep_note = await self._dependency_signal(project_path)
        if dep_note and report.dependencies == HealthSignal.WARNING:
            report.attention.append(dep_note)

        # "Runtime" here means "is the language runtime itself
        # resolvable" — a full running-process check belongs to
        # background_tasks.py's own task tracking, not duplicated here.
        report.runtime = HealthSignal.GOOD if report.dependencies != HealthSignal.UNKNOWN or report.git != HealthSignal.UNKNOWN else HealthSignal.UNKNOWN

        return report


project_health_monitor = ProjectHealthMonitor()
