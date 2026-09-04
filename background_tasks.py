"""
JARVIS background build/error monitoring (Phase 4, Feature 3).

Only monitors processes explicitly registered with JARVIS through
start_background_task — NEVER scans/attaches to every process on the
machine (Section: "Do NOT automatically monitor every process on the
computer"). One user, one backend process, module-level registry —
same architectural scope as terminal_tools.py's single-slot
_last_result cache, just keyed by task id since several background
tasks can legitimately run at once (a dev server AND a test watcher,
for instance).

Reuses terminal_tools.py rather than duplicating it:
  - classify_command() for the same SAFE/CONFIRM/DANGEROUS/REJECTED
    permission classification run_terminal_command uses — a
    background task is still just a command, and Section "Do not
    create a second command-classification path" applies here too.
  - _adapt_tokens_for_platform() for the same Windows echo/sleep shims.
  - extract_errors() for failure diagnosis when a task fails.

Does NOT reuse run_command() directly — that function's whole design
is "wait for the process to finish, with a timeout" (asyncio.wait_for
around communicate()), which is wrong for a `npm run dev` that's
*supposed* to run indefinitely. Background tasks need a
start-now/check-later model instead: spawn, hand back a task id
immediately, and let an asyncio.Task quietly drain stdout/stderr in
the background until the process exits (or is stopped).
"""

import asyncio
import logging
import shlex
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("jarvis-background")

BroadcastFn = Callable[[dict], Awaitable[None]]

# Cap on captured stdout/stderr per task — same reasoning as
# terminal_tools._MAX_OUTPUT_CHARS: a long-running dev server can emit
# megabytes of log output over its lifetime, and only the tail is ever
# useful for diagnosing a failure.
_MAX_OUTPUT_CHARS = 8_000

# Bound on how many background tasks can be RUNNING at once. Not a
# hard technical limit — it's a sanity backstop so a chain of "monitor
# this build" requests can't quietly accumulate an unbounded number of
# live subprocesses under JARVIS.
_MAX_CONCURRENT_TASKS = 5


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class NotificationSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class BackgroundTask:
    id: str
    name: str
    command: str
    project: Optional[str]
    start_time: float
    status: TaskStatus = TaskStatus.PENDING
    exit_code: Optional[int] = None
    stdout_summary: str = ""
    stderr_summary: str = ""
    error_detected: Optional[str] = None
    end_time: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "project": self.project,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "stdout_summary": self.stdout_summary,
            "stderr_summary": self.stderr_summary,
            "error_detected": self.error_detected,
        }


class TaskManager:
    """
    One instance lives at module scope (see `task_manager` below) —
    matches terminal_tools.py's module-level last-result cache and
    context_manager.py's module-level singleton, the established
    pattern in this codebase for "one shared piece of backend state,
    one user, no multi-tenancy."
    """

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._monitor_handles: dict[str, asyncio.Task] = {}
        # Notification dedup (Section: NOTIFICATION RULES) — tracks the
        # last-notified failure fingerprint per task id, so the same
        # recurring error doesn't re-notify on every poll/restart cycle.
        self._last_notified_fingerprint: dict[str, Optional[str]] = {}
        # Set once at startup (main.py's lifespan wires this to
        # ConnectionManager.broadcast) so tool-registry handlers — which
        # only ever receive **args, not a broadcast callback (see
        # tool_registry.run_tool) — don't need one threaded through
        # every call just to notify on a background failure later.
        self._default_broadcast: Optional[BroadcastFn] = None

    def set_broadcast_fn(self, fn: Optional[BroadcastFn]) -> None:
        self._default_broadcast = fn

    def list_tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def get_task(self, task_id: str) -> Optional[BackgroundTask]:
        return self._tasks.get(task_id)

    def _running_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)

    async def start_task(
        self,
        name: str,
        command: str,
        project: Optional[str] = None,
        cwd: Optional[str] = None,
        broadcast: Optional[BroadcastFn] = None,
    ) -> BackgroundTask:
        """
        Spawn `command` as a monitored background task and return
        immediately with a PENDING->RUNNING task record — does not
        wait for the process to exit (that's the whole point of
        "background"). Callers must already have gone through
        classify_command()/the permission gate before this is
        reached; this function executes unconditionally, same
        division of responsibility as terminal_tools.run_command.
        """
        if self._running_count() >= _MAX_CONCURRENT_TASKS:
            raise RuntimeError(
                f"Already monitoring {_MAX_CONCURRENT_TASKS} background tasks — "
                f"stop one before starting another."
            )

        broadcast = broadcast or self._default_broadcast
        import terminal_tools as _tt

        task_id = uuid.uuid4().hex[:12]
        task = BackgroundTask(
            id=task_id, name=name, command=command, project=project,
            start_time=time.time(), status=TaskStatus.PENDING,
        )
        self._tasks[task_id] = task
        self._last_notified_fingerprint.setdefault(task_id, None)

        tokens = shlex.split(command.strip())
        tokens = _tt._adapt_tokens_for_platform(tokens)

        try:
            proc = await asyncio.create_subprocess_exec(
                *tokens, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            task.status = TaskStatus.FAILED
            task.error_detected = f"Command not found: '{tokens[0] if tokens else command}'"
            task.end_time = time.time()
            return task
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error_detected = f"Failed to launch: {e}"
            task.end_time = time.time()
            return task

        self._processes[task_id] = proc
        task.status = TaskStatus.RUNNING
        self._monitor_handles[task_id] = asyncio.create_task(
            self._monitor(task_id, proc, broadcast)
        )
        return task

    async def _monitor(self, task_id: str, proc: asyncio.subprocess.Process, broadcast: Optional[BroadcastFn]) -> None:
        """
        Drains stdout/stderr as the process runs and updates the task
        record when it exits. Runs as a background asyncio.Task — this
        is the "quietly watch, don't block anything else" half of the
        feature; nothing about it polls or captures anything outside
        this one process's own output streams.
        """
        import terminal_tools as _tt

        task = self._tasks[task_id]
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _drain(stream, sink):
            while True:
                chunk = await stream.readline()
                if not chunk:
                    break
                sink.append(chunk)

        try:
            await asyncio.gather(
                _drain(proc.stdout, stdout_chunks),
                _drain(proc.stderr, stderr_chunks),
            )
            exit_code = await proc.wait()
        except asyncio.CancelledError:
            # stop_task() cancelled us — proc.kill() already happened there.
            return
        finally:
            self._processes.pop(task_id, None)

        stdout_text = b"".join(stdout_chunks).decode(errors="replace")
        stderr_text = b"".join(stderr_chunks).decode(errors="replace")
        if len(stdout_text) > _MAX_OUTPUT_CHARS:
            stdout_text = stdout_text[-_MAX_OUTPUT_CHARS:]
        if len(stderr_text) > _MAX_OUTPUT_CHARS:
            stderr_text = stderr_text[-_MAX_OUTPUT_CHARS:]

        task.stdout_summary = stdout_text
        task.stderr_summary = stderr_text
        task.exit_code = exit_code
        task.end_time = time.time()

        if task.status == TaskStatus.CANCELLED:
            # stop_task() already set this — don't overwrite it with
            # SUCCEEDED/FAILED just because the process has now
            # actually exited following the kill signal.
            return

        combined_output = stderr_text if stderr_text.strip() else stdout_text
        if exit_code == 0:
            task.status = TaskStatus.SUCCEEDED
            task.error_detected = None
        else:
            task.status = TaskStatus.FAILED
            extracted = _tt.extract_errors(combined_output)
            task.error_detected = extracted.primary_error or f"Process exited with code {exit_code}."

        await self._maybe_notify(task, broadcast)

    async def _maybe_notify(self, task: BackgroundTask, broadcast: Optional[BroadcastFn]) -> None:
        """
        Section: NOTIFICATION RULES — notify on the first occurrence of
        a failure, suppress repeats of the exact same error, always
        notify on a genuinely new/different error. Success is INFO and
        always notified once (task completion is inherently a
        one-time, meaningful event, not a repeatable annoyance).
        """
        if broadcast is None:
            return

        if task.status == TaskStatus.SUCCEEDED:
            await broadcast({
                "type": "background_task_notification",
                "severity": NotificationSeverity.INFO.value,
                "task": task.to_dict(),
                "message": f"'{task.name}' completed successfully.",
            })
            return

        if task.status not in (TaskStatus.FAILED, TaskStatus.TIMED_OUT):
            return

        fingerprint = task.error_detected or f"exit_code={task.exit_code}"
        last = self._last_notified_fingerprint.get(task.id)
        if fingerprint == last:
            logger.info(f"[background] Suppressing duplicate notification for task {task.id}: {fingerprint}")
            return

        self._last_notified_fingerprint[task.id] = fingerprint
        severity = NotificationSeverity.CRITICAL if task.status == TaskStatus.TIMED_OUT else NotificationSeverity.ERROR
        await broadcast({
            "type": "background_task_notification",
            "severity": severity.value,
            "task": task.to_dict(),
            "message": f"'{task.name}' failed. I detected: {task.error_detected}. Investigate?",
        })

    async def stop_task(self, task_id: str) -> BackgroundTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"No background task with id '{task_id}'.")
        if task.status != TaskStatus.RUNNING:
            return task  # already finished — nothing to stop, report current state honestly

        proc = self._processes.get(task_id)
        task.status = TaskStatus.CANCELLED
        task.end_time = time.time()
        handle = self._monitor_handles.pop(task_id, None)
        if handle is not None:
            handle.cancel()
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass  # already exited between the status check above and here
            else:
                # Reap the killed process so its transport is closed
                # cleanly rather than left for the GC to complain about
                # once the event loop is torn down (harmless but noisy).
                try:
                    await proc.wait()
                except Exception:
                    pass
            self._processes.pop(task_id, None)
        return task

    def shutdown_all(self) -> None:
        """
        Kill every still-running monitored process. Called from
        main.py's lifespan shutdown so a background `npm run dev`
        doesn't outlive the JARVIS backend process itself as an orphan.
        Synchronous and best-effort — process.kill() doesn't need an
        event loop, this just needs to run once during shutdown.
        """
        for task_id, proc in list(self._processes.items()):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            task = self._tasks.get(task_id)
            if task is not None and task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.CANCELLED
                task.end_time = time.time()
        for handle in self._monitor_handles.values():
            handle.cancel()


task_manager = TaskManager()
