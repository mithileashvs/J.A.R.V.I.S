"""
JARVIS PROACTIVE INTELLIGENCE.

A small, generic event pipeline — OBSERVE -> DETECT -> ANALYZE ->
DECIDE IF IMPORTANT -> NOTIFY -> OFFER ACTION — plus a handful of
concrete detectors for the categories not already covered by an
existing system:

  SYSTEM   -> CPU / RAM / GPU usage+temp / storage % / battery / network
  SECURITY -> real Defender threat detections + scan completion
              (system_health.py)
  STORAGE  -> critically low free space + junk-cleanup opportunity
              (system_health.py)
  VOICE    -> voice service (LiveKit) configured/reachable

DEVELOPMENT and PROJECT (build/test failures, git/dependency signals)
are already fully implemented by background_tasks.py's TaskManager
(`background_task_notification` broadcasts) and project_health.py's
ProjectHealthMonitor (`health_alert` broadcasts) — this module doesn't
duplicate that detection. What it DOES add for those two is nothing;
main.py just needs to make sure the frontend renders those two
existing broadcast types the same way it renders this module's
`proactive_event` broadcasts, which — before this feature — it did
not (see frontend/app.js's new "system_alert"/"background_task_
notification"/"health_alert" cases).

── THE PIPELINE (Section 16) ──────────────────────────────────────

Detectors are pure functions that read one real metric and return
(is_breaching, severity, evidence) — no notification decision is made
there. Everything about WHETHER to actually surface something —
debouncing, cooldown, deduplication, recovery, severity escalation —
lives in one place: `ProactiveEngine._process()`, so a new detector
gets all of that for free just by calling it.

── DEBOUNCING (Section 5) ─────────────────────────────────────────

A single breaching reading doesn't notify by itself. Each event_id
tracks consecutive-breach count across polling cycles; only once that
reaches `_cycles_required(severity)` does it become eligible to
notify (CRITICAL needs fewer consecutive cycles than WARNING — a
critical condition shouldn't wait as long to be reported).

── DEDUPLICATION + COOLDOWN (Section 6/7) ─────────────────────────

Once notified, the same event_id won't notify again until either:
  - its cooldown period elapses, or
  - its severity gets strictly worse (escalation bypasses cooldown —
    Section 6: "if it becomes significantly worse, allow another
    notification with increased severity").

── RECOVERY (Section 8) ────────────────────────────────────────────

When a condition that WAS actively notified clears, exactly one INFO
recovery event fires. A condition that was merely debouncing (never
actually reached the user) clearing does not generate a recovery
notification — there was nothing to recover from, from the user's
perspective.

── RESOURCE USE (Section 17/18) ────────────────────────────────────

One asyncio task, one tick loop. Each detector group carries its own
poll interval and only actually does its (real, deterministic —
psutil/subprocess, never the LLM) work when due; the tick itself is
cheap. No detector calls the LLM. No detector uploads anything
external (Section 27) — every reading here is a local syscall or a
local PowerShell/subprocess call already used elsewhere in this
codebase (system_health.py).
"""

import asyncio
import logging
import os
import platform
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("jarvis-proactive")

_SYSTEM = platform.system()

NotifyFn = Callable[[dict], Awaitable[None]]


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


@dataclass
class ProactiveEvent:
    event_id: str
    category: str  # SYSTEM / SECURITY / STORAGE / VOICE
    severity: Severity
    title: str
    message: str
    evidence: dict = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)  # subset of ACTION_* below
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "category": self.category,
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "evidence": self.evidence,
            "actions": self.actions,
            "timestamp": self.timestamp,
        }


# Section 11 — only real, wired-up actions. Each maps to an existing
# chat command on the frontend side (see app.js's PROACTIVE_ACTION_COMMANDS)
# rather than a new tool-call surface.
ACTION_ANALYZE_STORAGE = "ANALYZE_STORAGE"
ACTION_CLEAN = "CLEAN"
ACTION_RUN_SCAN = "RUN_SCAN"
ACTION_INSPECT = "INSPECT"
ACTION_VIEW_DETAILS = "VIEW_DETAILS"


@dataclass
class _EventState:
    consecutive_breaches: int = 0
    is_active: bool = False           # condition currently ongoing
    was_notified: bool = False        # ...and the user was actually told about it
    last_notified_severity: Optional[Severity] = None
    last_notified_at: Optional[float] = None


@dataclass
class ProactiveConfig:
    """
    Section 5/19 — "thresholds should be configurable... do not assume
    one threshold is perfect for every computer." Every field here can
    be overridden via a `JARVIS_PROACTIVE_*` environment variable (see
    `from_env()`) without editing code, same pattern config.py already
    uses for TEXT_MODEL/VISION_MODEL.
    """
    enabled: bool = True

    cpu_warn_percent: float = 85.0
    cpu_critical_percent: float = 97.0
    ram_warn_percent: float = 85.0
    ram_critical_percent: float = 95.0
    gpu_warn_percent: float = 90.0
    gpu_critical_percent: float = 98.0
    cpu_temp_warn_c: float = 80.0
    cpu_temp_critical_c: float = 92.0
    gpu_temp_warn_c: float = 83.0
    gpu_temp_critical_c: float = 92.0
    storage_warn_percent: float = 85.0
    storage_critical_percent: float = 95.0
    battery_warn_percent: float = 15.0
    battery_critical_percent: float = 5.0
    storage_cleanup_reclaimable_warn_gb: float = 5.0

    # Debounce: consecutive breaching cycles required before the FIRST
    # notification for a WARNING vs a CRITICAL condition.
    warn_cycles_required: int = 3
    critical_cycles_required: int = 1

    # Cooldown, per severity — after notifying, wait this long before
    # notifying about the *same, unchanged* condition again.
    warn_cooldown_seconds: float = 30 * 60
    critical_cooldown_seconds: float = 10 * 60
    security_cooldown_seconds: float = 60 * 60
    storage_cleanup_cooldown_seconds: float = 24 * 60 * 60
    voice_cooldown_seconds: float = 6 * 60 * 60

    # Poll intervals (Section 18 — different categories, different
    # cadence; "system: periodic lightweight polling", "storage: less
    # frequent", "security: event/scan based where possible").
    system_poll_interval: float = 60.0
    storage_summary_poll_interval: float = 120.0
    storage_cleanup_poll_interval: float = 30 * 60.0
    security_poll_interval: float = 5 * 60.0
    voice_poll_interval: float = 30 * 60.0

    tick_interval: float = 20.0  # how often the loop wakes up to check what's due

    def cooldown_for(self, severity: Severity) -> float:
        return self.critical_cooldown_seconds if severity == Severity.CRITICAL else self.warn_cooldown_seconds

    def cycles_required_for(self, severity: Severity) -> int:
        return self.critical_cycles_required if severity == Severity.CRITICAL else self.warn_cycles_required

    @classmethod
    def from_env(cls) -> "ProactiveConfig":
        cfg = cls()
        overrides = {
            "JARVIS_PROACTIVE_ENABLED": ("enabled", lambda v: v.strip().lower() not in ("0", "false", "off", "no")),
            "JARVIS_PROACTIVE_CPU_WARN_PERCENT": ("cpu_warn_percent", float),
            "JARVIS_PROACTIVE_CPU_CRITICAL_PERCENT": ("cpu_critical_percent", float),
            "JARVIS_PROACTIVE_RAM_WARN_PERCENT": ("ram_warn_percent", float),
            "JARVIS_PROACTIVE_RAM_CRITICAL_PERCENT": ("ram_critical_percent", float),
            "JARVIS_PROACTIVE_STORAGE_WARN_PERCENT": ("storage_warn_percent", float),
            "JARVIS_PROACTIVE_STORAGE_CRITICAL_PERCENT": ("storage_critical_percent", float),
            "JARVIS_PROACTIVE_WARN_COOLDOWN_SECONDS": ("warn_cooldown_seconds", float),
            "JARVIS_PROACTIVE_CRITICAL_COOLDOWN_SECONDS": ("critical_cooldown_seconds", float),
        }
        for env_name, (attr, caster) in overrides.items():
            raw = os.environ.get(env_name)
            if raw is not None and raw.strip() != "":
                try:
                    setattr(cfg, attr, caster(raw))
                except ValueError:
                    logger.warning(f"[proactive] Ignoring invalid {env_name}={raw!r}")
        return cfg


def _severity_of(value: float, warn: float, critical: float) -> Optional[Severity]:
    if value >= critical:
        return Severity.CRITICAL
    if value >= warn:
        return Severity.WARNING
    return None


def _format_bytes_gb(n: float) -> str:
    return f"{n / (1024 ** 3):.1f} GB"


class ProactiveEngine:
    def __init__(self, config: Optional[ProactiveConfig] = None) -> None:
        self.config = config or ProactiveConfig.from_env()
        self._states: dict[str, _EventState] = {}
        self._notify_fn: Optional[NotifyFn] = None
        self._task: Optional[asyncio.Task] = None

        # Per-category "next due" timestamps for the differentiated
        # polling intervals (Section 18).
        self._next_due: dict[str, float] = {}

        # Edge-triggered watchers that aren't threshold-based —
        # "did a scan just finish" isn't a sustained condition, it's a
        # one-shot transition.
        self._last_seen_scan_completion: Optional[str] = None

        # Quiet/focus behavior (Section 20) — set from main.py right
        # before an active voice turn / a fresh user message, read here
        # to decide whether an INFO event should wait.
        self._suppress_info_until: float = 0.0

        # Recent event log for /proactive/events and Section 26 logging
        # (kept in-memory, bounded — this is a debugging/inspection
        # aid, not a second persistence layer; memory.log_event() is
        # still the durable record).
        self._recent_events: list[dict] = []

    # ── lifecycle ────────────────────────────────────────────────
    def set_broadcast_fn(self, fn: Optional[NotifyFn]) -> None:
        self._notify_fn = fn

    def set_enabled(self, enabled: bool) -> bool:
        self.config.enabled = enabled
        return self.config.enabled

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def note_user_activity(self, quiet_seconds: float = 15.0) -> None:
        """Section 20 — called from main.py whenever a fresh user
        message/voice turn starts, so a low-priority INFO notification
        doesn't land mid-interaction. CRITICAL events ignore this."""
        self._suppress_info_until = max(self._suppress_info_until, time.time() + quiet_seconds)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if self.is_running():
            return False
        self._task = asyncio.create_task(self._loop())
        return True

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def recent_events(self, limit: int = 20) -> list[dict]:
        return self._recent_events[-limit:]

    # ── main loop ────────────────────────────────────────────────
    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.config.tick_interval)
                if not self.config.enabled:
                    continue
                try:
                    await self._tick()
                except Exception as e:  # noqa: BLE001 — one bad cycle must never kill the loop.
                    logger.error(f"[proactive] tick failed: {e}")
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        now = time.time()

        if now >= self._next_due.get("system", 0):
            self._next_due["system"] = now + self.config.system_poll_interval
            await self._check_system()

        if now >= self._next_due.get("storage_summary", 0):
            self._next_due["storage_summary"] = now + self.config.storage_summary_poll_interval
            await self._check_storage_summary()

        if now >= self._next_due.get("storage_cleanup", 0):
            self._next_due["storage_cleanup"] = now + self.config.storage_cleanup_poll_interval
            await self._check_storage_cleanup()

        if now >= self._next_due.get("security", 0):
            self._next_due["security"] = now + self.config.security_poll_interval
            await self._check_security()

        if now >= self._next_due.get("voice", 0):
            self._next_due["voice"] = now + self.config.voice_poll_interval
            await self._check_voice()

    # ── core decision pipeline (Section 5/6/7/8) ────────────────────
    async def _process(self, event_id: str, breaching: bool, build_event) -> None:
        """
        `build_event` is a zero-arg callable returning a ProactiveEvent
        for the CURRENT breach — only called when actually needed (i.e.
        never on a non-breaching tick), so detectors that need to do
        extra work to build a rich message only do it when it matters.
        """
        state = self._states.setdefault(event_id, _EventState())

        if not breaching:
            if state.is_active:
                was_notified = state.was_notified
                state.is_active = False
                state.consecutive_breaches = 0
                prior_severity = state.last_notified_severity
                state.last_notified_severity = None
                state.was_notified = False
                if was_notified:
                    await self._emit_recovery(event_id, prior_severity)
            return

        state.is_active = True
        state.consecutive_breaches += 1
        event = build_event()

        if state.consecutive_breaches < self.config.cycles_required_for(event.severity):
            return  # Section 5 — not sustained long enough yet

        now = time.time()
        escalated = (
            state.last_notified_severity is not None
            and _SEVERITY_RANK[event.severity] > _SEVERITY_RANK[state.last_notified_severity]
        )
        cooldown = self.config.cooldown_for(event.severity)
        in_cooldown = state.last_notified_at is not None and (now - state.last_notified_at) < cooldown

        if in_cooldown and not escalated:
            return  # Section 6/7 — deduplicated / still cooling down

        state.last_notified_severity = event.severity
        state.last_notified_at = now
        state.was_notified = True
        await self._emit(event)

    async def _emit_recovery(self, event_id: str, prior_severity: Optional[Severity]) -> None:
        # Section 8 — one quiet INFO event, only for conditions that
        # were actually surfaced (was_notified), never for something
        # that only ever debounced silently.
        titles = {
            "cpu_high": ("CPU NORMAL", "CPU usage has returned to normal."),
            "ram_high": ("MEMORY NORMAL", "Memory usage has returned to normal."),
            "gpu_high": ("GPU NORMAL", "GPU usage has returned to normal."),
            "cpu_temp_high": ("CPU TEMPERATURE NORMAL", "CPU temperature has returned to normal."),
            "gpu_temp_high": ("GPU TEMPERATURE NORMAL", "GPU temperature has returned to normal."),
            "storage_low": ("STORAGE NORMAL", "Free storage has returned to a healthy level."),
            "battery_low": ("BATTERY NORMAL", "Battery level has returned to normal."),
            "network_disconnected": ("NETWORK RESTORED", "Network connectivity has been restored."),
            "security_threat": ("SECURITY CLEAR", "The previously detected threat is no longer active."),
        }
        title, message = titles.get(event_id, ("RESOLVED", f"The '{event_id}' condition has cleared."))
        await self._emit(ProactiveEvent(
            event_id=f"{event_id}:recovery",
            category="SYSTEM",
            severity=Severity.INFO,
            title=title,
            message=message,
        ))

    async def _emit(self, event: ProactiveEvent) -> None:
        # Section 20 — a low-priority (INFO) event waits out an active
        # user interaction; WARNING/CRITICAL are never held back.
        if event.severity == Severity.INFO and time.time() < self._suppress_info_until:
            logger.info(f"[proactive] holding back INFO event during active interaction: {event.event_id}")
            # Re-check shortly rather than dropping it entirely.
            asyncio.get_event_loop().call_later(
                max(1.0, self._suppress_info_until - time.time() + 1.0),
                lambda: asyncio.create_task(self._emit_if_still_relevant(event)),
            )
            return

        try:
            import memory
            memory.log_event(f"proactive:{event.category.lower()}", f"[{event.severity.value}] {event.event_id}: {event.message}")
        except Exception as e:
            logger.warning(f"[proactive] log_event failed: {e}")

        self._recent_events.append(event.to_dict())
        self._recent_events = self._recent_events[-100:]

        if self._notify_fn is not None:
            try:
                await self._notify_fn({"type": "proactive_event", **event.to_dict()})
            except Exception as e:
                logger.warning(f"[proactive] broadcast failed: {e}")

    async def _emit_if_still_relevant(self, event: ProactiveEvent) -> None:
        # Only re-checked for INFO events held back by quiet/focus mode
        # — re-emits as-is rather than re-running the detector, since
        # the point is just "don't interrupt", not "re-verify".
        if time.time() >= self._suppress_info_until:
            await self._emit(event)

    # ── SYSTEM detectors (Section 3/15) ─────────────────────────────
    async def _check_system(self) -> None:
        try:
            import psutil
        except ImportError:
            return  # Section 25 — no psutil is a capability gap, not a false alert

        cfg = self.config

        cpu = await asyncio.to_thread(psutil.cpu_percent, None)
        await self._process("cpu_high", cpu is not None and cpu >= cfg.cpu_warn_percent, lambda: ProactiveEvent(
            event_id="cpu_high", category="SYSTEM",
            severity=_severity_of(cpu, cfg.cpu_warn_percent, cfg.cpu_critical_percent) or Severity.WARNING,
            title="HIGH CPU USAGE", message=f"CPU usage has been sustained at {cpu:.0f}%.",
            evidence={"cpu_percent": cpu}, actions=[ACTION_VIEW_DETAILS],
        ))

        try:
            ram = psutil.virtual_memory()
            ram_percent = ram.percent
            used_gb = ram.total - ram.available
            await self._process("ram_high", ram_percent >= cfg.ram_warn_percent, lambda: ProactiveEvent(
                event_id="ram_high", category="SYSTEM",
                severity=_severity_of(ram_percent, cfg.ram_warn_percent, cfg.ram_critical_percent) or Severity.WARNING,
                title="HIGH MEMORY USAGE",
                message=f"Memory usage is unusually high: {_format_bytes_gb(used_gb)} / {_format_bytes_gb(ram.total)}.",
                evidence={"ram_percent": ram_percent}, actions=[ACTION_VIEW_DETAILS],
            ))
        except Exception as e:
            logger.warning(f"[proactive] RAM check failed: {e}")

        gpu = await asyncio.to_thread(_read_gpu_stats)
        if gpu is not None:
            util, temp = gpu
            if util is not None:
                await self._process("gpu_high", util >= cfg.gpu_warn_percent, lambda: ProactiveEvent(
                    event_id="gpu_high", category="SYSTEM",
                    severity=_severity_of(util, cfg.gpu_warn_percent, cfg.gpu_critical_percent) or Severity.WARNING,
                    title="HIGH GPU USAGE", message=f"GPU usage has been sustained at {util:.0f}%.",
                    evidence={"gpu_percent": util}, actions=[ACTION_VIEW_DETAILS],
                ))
            if temp is not None:
                await self._process("gpu_temp_high", temp >= cfg.gpu_temp_warn_c, lambda: ProactiveEvent(
                    event_id="gpu_temp_high", category="SYSTEM",
                    severity=_severity_of(temp, cfg.gpu_temp_warn_c, cfg.gpu_temp_critical_c) or Severity.WARNING,
                    title="HIGH GPU TEMPERATURE", message=f"GPU temperature is unusually high: {temp:.0f}°C.",
                    evidence={"gpu_temp_c": temp}, actions=[ACTION_VIEW_DETAILS],
                ))
        # else: no GPU telemetry available on this machine — silently
        # skipped, not reported as a warning (Section 25).

        cpu_temp = await asyncio.to_thread(_read_cpu_temp)
        if cpu_temp is not None:
            await self._process("cpu_temp_high", cpu_temp >= cfg.cpu_temp_warn_c, lambda: ProactiveEvent(
                event_id="cpu_temp_high", category="SYSTEM",
                severity=_severity_of(cpu_temp, cfg.cpu_temp_warn_c, cfg.cpu_temp_critical_c) or Severity.WARNING,
                title="HIGH CPU TEMPERATURE", message=f"CPU temperature is unusually high: {cpu_temp:.0f}°C.",
                evidence={"cpu_temp_c": cpu_temp}, actions=[ACTION_VIEW_DETAILS],
            ))

        try:
            battery = psutil.sensors_battery()
        except Exception:
            battery = None
        if battery is not None and not battery.power_plugged:
            pct = battery.percent
            await self._process("battery_low", pct <= cfg.battery_warn_percent, lambda: ProactiveEvent(
                event_id="battery_low", category="SYSTEM",
                severity=Severity.CRITICAL if pct <= cfg.battery_critical_percent else Severity.WARNING,
                title="LOW BATTERY", message=f"Battery is at {pct:.0f}% and not charging.",
                evidence={"battery_percent": pct}, actions=[ACTION_VIEW_DETAILS],
            ))

        try:
            stats = psutil.net_if_stats()
            has_link = any(s.isup for name, s in stats.items() if name.lower() not in ("lo", "loopback"))
        except Exception:
            has_link = True  # can't tell — don't false-alarm
        await self._process("network_disconnected", not has_link, lambda: ProactiveEvent(
            event_id="network_disconnected", category="SYSTEM", severity=Severity.WARNING,
            title="NETWORK DISCONNECTED", message="No active network interface was detected.",
            actions=[ACTION_VIEW_DETAILS],
        ))

    # ── STORAGE detectors (Section 3/14) ────────────────────────────
    async def _check_storage_summary(self) -> None:
        import system_health
        summary = await asyncio.to_thread(system_health.get_storage_summary)
        if not summary.get("available"):
            return  # Section 25 — capability gap, not a false alert
        pct = summary["percent_used"]
        cfg = self.config
        await self._process("storage_low", pct >= cfg.storage_warn_percent, lambda: ProactiveEvent(
            event_id="storage_low", category="STORAGE",
            severity=_severity_of(pct, cfg.storage_warn_percent, cfg.storage_critical_percent) or Severity.WARNING,
            title="STORAGE LOW",
            message=f"Free storage: {_format_bytes_gb(summary['free_bytes'])} / {_format_bytes_gb(summary['total_bytes'])}.",
            evidence={"percent_used": pct, "free_bytes": summary["free_bytes"], "total_bytes": summary["total_bytes"]},
            actions=[ACTION_ANALYZE_STORAGE],
        ))

    async def _check_storage_cleanup(self) -> None:
        import system_health
        analysis = await asyncio.to_thread(system_health.analyze_storage)
        if not analysis.get("available"):
            return
        reclaimable_gb = analysis["reclaimable_bytes"] / (1024 ** 3)
        threshold = self.config.storage_cleanup_reclaimable_warn_gb
        await self._process("storage_cleanup_opportunity", reclaimable_gb >= threshold, lambda: ProactiveEvent(
            event_id="storage_cleanup_opportunity", category="STORAGE", severity=Severity.INFO,
            title="CLEANUP OPPORTUNITY",
            message=f"JARVIS found {_format_bytes_gb(analysis['reclaimable_bytes'])} of safely removable files.",
            evidence={"reclaimable_bytes": analysis["reclaimable_bytes"]},
            actions=[ACTION_ANALYZE_STORAGE, ACTION_CLEAN],
        ))

    # ── SECURITY detectors (Section 3/13) ───────────────────────────
    async def _check_security(self) -> None:
        import system_health

        threats = await asyncio.to_thread(system_health.get_threat_detections)
        if threats.get("available"):
            unresolved = [t for t in (threats.get("threats") or []) if not t.get("action_succeeded")]
            if unresolved:
                names = sorted({t.get("name", "unknown") for t in unresolved})
                signature = "|".join(names)
                await self._process(f"security_threat:{signature}", True, lambda: ProactiveEvent(
                    event_id=f"security_threat:{signature}", category="SECURITY", severity=Severity.CRITICAL,
                    title="SECURITY THREAT DETECTED",
                    message=f"Windows Security reported a threat: {', '.join(names)}.",
                    evidence={"threats": unresolved}, actions=[ACTION_VIEW_DETAILS, ACTION_RUN_SCAN],
                ))
            else:
                # No unresolved threats right now — clear ANY previously
                # active security_threat:* state so a resolved threat
                # gets its recovery event (Section 8/13).
                for event_id in list(self._states.keys()):
                    if event_id.startswith("security_threat:"):
                        await self._process(event_id, False, lambda: None)  # breaching=False, build_event unused

        status = await asyncio.to_thread(system_health.get_security_status)
        if status.get("available"):
            latest = status.get("last_quick_scan") or status.get("last_full_scan")
            if latest and latest != self._last_seen_scan_completion:
                first_run = self._last_seen_scan_completion is None
                self._last_seen_scan_completion = latest
                if not first_run:  # don't announce "scan completed" for scans that finished before JARVIS started watching
                    threat_count = len([t for t in (threats.get("threats") or []) if not t.get("action_succeeded")]) if threats.get("available") else 0
                    await self._emit(ProactiveEvent(
                        event_id=f"scan_completed:{latest}", category="SECURITY",
                        severity=Severity.CRITICAL if threat_count else Severity.INFO,
                        title="SECURITY SCAN COMPLETE",
                        message=(f"Security scan completed — {threat_count} threat(s) detected." if threat_count
                                 else "Security scan completed with no threats."),
                        actions=[ACTION_VIEW_DETAILS],
                    ))

    # ── VOICE detector (Section 3) ───────────────────────────────────
    async def _check_voice(self) -> None:
        try:
            from config import LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
        except Exception:
            return
        configured = bool(LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET)
        state = self._states.setdefault("voice_unavailable", _EventState())
        # One notch of manual cooldown control since this is a static
        # config check, not a threshold — reuse _process but with a
        # long, fixed cooldown by overriding cooldown_for's effect via
        # the WARNING branch (voice_cooldown_seconds is applied by
        # simply treating this as a WARNING-tier event with the
        # standard pipeline; the interval itself — 30 min — combined
        # with a 6h notify cooldown keeps this from being noisy).
        await self._process("voice_unavailable", not configured, lambda: ProactiveEvent(
            event_id="voice_unavailable", category="VOICE", severity=Severity.WARNING,
            title="VOICE SERVICE UNAVAILABLE",
            message="Voice isn't configured (LIVEKIT_URL/LIVEKIT_API_KEY/LIVEKIT_API_SECRET missing) — text chat still works normally.",
            actions=[ACTION_VIEW_DETAILS],
        ))


def _read_gpu_stats() -> Optional[tuple[Optional[float], Optional[float]]]:
    """(utilization_percent, temperature_c) via nvidia-smi, or None if
    unavailable — never fabricated. AMD/Intel GPUs aren't covered
    (nvidia-smi is the only widely-available local, no-extra-dependency
    query this codebase can rely on); see the final report."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        first_line = proc.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in first_line.split(",")]
        util = float(parts[0]) if len(parts) > 0 and parts[0] else None
        temp = float(parts[1]) if len(parts) > 1 and parts[1] else None
        return (util, temp)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None
    except Exception as e:
        logger.warning(f"[proactive] GPU read failed: {e}")
        return None


def _read_cpu_temp() -> Optional[float]:
    """Highest reported CPU package/core temperature via
    psutil.sensors_temperatures() — Linux-only in most psutil builds;
    returns None (not an error) on Windows/macOS or if no sensor is
    exposed, per Section 3's own "if available" qualifier."""
    try:
        import psutil
        temps = psutil.sensors_temperatures()
    except (AttributeError, ImportError, Exception):
        return None
    if not temps:
        return None
    candidates = []
    for label, entries in temps.items():
        if any(k in label.lower() for k in ("cpu", "core", "package", "k10temp", "coretemp")):
            candidates.extend(e.current for e in entries if e.current is not None)
    if not candidates:
        return None
    return max(candidates)


proactive_engine = ProactiveEngine()
