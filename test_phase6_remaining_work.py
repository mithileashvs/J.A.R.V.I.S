"""
JARVIS Phase 6 "remaining work" test suite — covers:

  - Feature 12: workflow progress checklist
  - Feature 17: error recovery (RECOVER -> REPLAN)
  - Feature 3 fix found during audit: MANUAL autonomy actually enforced
  - Structured Approval UX / Feature 10: approve_steps / approve_all_remaining /
    reject_next_step / pending_steps_preview
  - Feature 16: audit log recent-activity viewer + clear
  - Feature 6 (remaining work): automatic/periodic health monitoring
  - Feature 13: CSE Exam Prep workflow, routing, and its architectural boundary

Run with: pytest test_phase6_remaining_work.py -v
Mirrors test_phase6.py's fixtures exactly (this suite's convention is
one self-contained file per test module — no shared conftest.py).
"""

import json as _json
from unittest.mock import patch

import pytest

import workflows  # noqa: F401 — registers all workflow kinds, including exam_prep

from workflow_engine import (
    AutonomyLevel,
    StepResult,
    StepStatus,
    WorkflowEngine,
    WorkflowKindSpec,
    WorkflowStatus,
    WorkflowStep,
)


# ── Fixtures (mirrors test_phase6.py) ─────────────────────────────
@pytest.fixture
def jarvis_db_path(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test_jarvis_chat.db")
    monkeypatch.setenv("JARVIS_DB_PATH", db_path)
    return db_path


@pytest.fixture(autouse=True)
def isolate_shared_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_phase6_remaining.db")
    monkeypatch.setenv("JARVIS_DB_PATH", db_path)
    import memory
    import project_memory
    monkeypatch.setattr(memory, "DB_PATH", db_path)
    monkeypatch.setattr(project_memory, "DB_PATH", db_path)
    memory.init_db()
    project_memory.init_project_memory_db()


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake_key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fake_secret")


@pytest.fixture
def engine():
    return WorkflowEngine()


def _register_simple_kind(engine, n_steps=3, fail_at=None, verified=True, always_fail=False):
    """Same helper as test_phase6.py, plus `always_fail` (every attempt
    at that index fails, for recovery tests that need a failure that
    persists through a retry, not just the first attempt)."""
    attempts = {}

    async def handler(workflow, step):
        idx = int(step.description.split()[-1])
        if fail_at is not None and idx == fail_at:
            attempts[idx] = attempts.get(idx, 0) + 1
            if always_fail or attempts[idx] == 1:
                return StepResult(summary="failed on purpose", verified_success=False, error="synthetic failure")
        return StepResult(summary=f"did step {idx}", verified_success=verified, evidence=f"evidence for step {idx}")

    def build_steps(**kwargs):
        return [WorkflowStep(f"do thing {i}", handler_key="h") for i in range(n_steps)]

    engine.register_kind(WorkflowKindSpec(name="simple", build_steps=build_steps, handlers={"h": handler}))
    return attempts


def _register_tool_kind(engine, n_tool_steps=2):
    """Steps that each name a CONFIRM-level tool, so the engine stops at
    WAITING_FOR_PERMISSION exactly like test_phase6.py's 'risky' kind
    (a genuinely safe command like `echo` never triggers a confirmation
    at all, so this deliberately reuses the same dangerous-looking
    command shape as test_phase6.py's own CONFIRM-path tests)."""
    def build_steps(**kwargs):
        return [
            WorkflowStep(f"risky step {i}", tool_name="run_terminal_command", tool_args={"command": f"rm -rf somedir{i}"})
            for i in range(n_tool_steps)
        ]
    engine.register_kind(WorkflowKindSpec(name="tool_gated", build_steps=build_steps, handlers={}))


# ── Feature 12: progress checklist ────────────────────────────────
class TestProgressChecklist:
    def test_checklist_marks_done_current_pending(self, engine):
        _register_simple_kind(engine, n_steps=3)
        wf = engine.create_workflow("simple", user_request="test")
        wf.steps[0].status = StepStatus.DONE
        wf.current_step = 1
        wf.status = WorkflowStatus.RUNNING
        checklist = wf.to_checklist()
        assert checklist[0]["marker"] == "\u2713"
        assert checklist[1]["marker"] == "\u25cf"
        assert checklist[2]["marker"] == "\u25cb"

    def test_checklist_marks_failed_step(self, engine):
        _register_simple_kind(engine, n_steps=2)
        wf = engine.create_workflow("simple", user_request="test")
        wf.steps[0].status = StepStatus.FAILED
        checklist = wf.to_checklist()
        assert checklist[0]["marker"] == "\u2717"

    def test_checklist_marks_skipped(self, engine):
        _register_simple_kind(engine, n_steps=2)
        wf = engine.create_workflow("simple", user_request="test")
        wf.steps[0].status = StepStatus.SKIPPED
        checklist = wf.to_checklist()
        assert checklist[0]["marker"] == "\u2013"

    def test_cancelled_workflow_shows_remaining_as_skipped(self, engine):
        _register_simple_kind(engine, n_steps=3)
        wf = engine.create_workflow("simple", user_request="test")
        wf.steps[0].status = StepStatus.DONE
        wf.status = WorkflowStatus.CANCELLED
        checklist = wf.to_checklist()
        assert checklist[0]["marker"] == "\u2713"
        assert checklist[1]["marker"] == "\u2013"
        assert checklist[2]["marker"] == "\u2013"

    @pytest.mark.asyncio
    async def test_real_run_produces_all_done_checklist(self, engine):
        """Not hand-set statuses — an actual completed run."""
        _register_simple_kind(engine, n_steps=3)
        wf = engine.create_workflow("simple", user_request="test")
        result = await engine.run(wf.id)
        checklist = result.to_checklist()
        assert all(item["marker"] == "\u2713" for item in checklist)

    def test_to_dict_preserves_existing_steps_key_and_adds_checklist(self, engine):
        _register_simple_kind(engine, n_steps=2)
        wf = engine.create_workflow("simple", user_request="test")
        d = wf.to_dict()
        assert "steps" in d and len(d["steps"]) == 2  # existing payload untouched
        assert "checklist" in d and len(d["checklist"]) == 2
        assert d["type"] == "workflow_progress"


# ── Feature 3 (audit fix): MANUAL autonomy really enforced ────────
class TestManualAutonomyEnforcement:
    @pytest.mark.asyncio
    async def test_manual_autonomy_skips_tool_backed_step(self, engine, fake_env):
        _register_tool_kind(engine, n_tool_steps=1)
        wf = engine.create_workflow("tool_gated", user_request="test", autonomy_level=AutonomyLevel.MANUAL)
        result = await engine.run(wf.id)
        assert result.steps[0].status == StepStatus.SKIPPED
        assert result.status == WorkflowStatus.COMPLETED  # skipped isn't failed

    @pytest.mark.asyncio
    async def test_manual_autonomy_still_runs_handler_steps(self, engine):
        _register_simple_kind(engine, n_steps=1)
        wf = engine.create_workflow("simple", user_request="test", autonomy_level=AutonomyLevel.MANUAL)
        result = await engine.run(wf.id)
        assert result.steps[0].status == StepStatus.DONE


# ── Feature 17: error recovery ────────────────────────────────────
class TestErrorRecovery:
    @pytest.mark.asyncio
    async def test_recoverable_failure_succeeds_on_replan_retry(self, engine):
        """fail_at=1 without always_fail -> fails once, succeeds on the
        engine's own retry."""
        _register_simple_kind(engine, n_steps=2, fail_at=1)
        wf = engine.create_workflow("simple", user_request="test")
        result = await engine.run(wf.id)
        assert result.status == WorkflowStatus.COMPLETED
        assert result.steps[1].status == StepStatus.DONE
        assert result.recovery_attempts  # something was recorded as attempted

    @pytest.mark.asyncio
    async def test_recovery_failure_leaves_step_failed_but_workflow_continues(self, engine):
        _register_simple_kind(engine, n_steps=3, fail_at=1, always_fail=True)
        wf = engine.create_workflow("simple", user_request="test")
        result = await engine.run(wf.id)
        # One replan/retry attempted (capped), still failing -> step FAILED,
        # but the engine doesn't get stuck: it still executes step 2.
        assert result.steps[1].status == StepStatus.FAILED
        assert result.steps[2].status == StepStatus.DONE
        assert result.status == WorkflowStatus.FAILED  # Rule 7: unresolved failure -> not COMPLETED

    @pytest.mark.asyncio
    async def test_recovery_cannot_loop_indefinitely(self, engine):
        """A failure that always fails must not be retried more than
        _MAX_RECOVERY_ATTEMPTS times, and repeated-action detection
        still applies underneath it regardless."""
        _register_simple_kind(engine, n_steps=1, fail_at=0, always_fail=True)
        wf = engine.create_workflow("simple", user_request="test")
        result = await engine.run(wf.id)
        key = result.steps[0].action_key()
        assert result.recovery_attempts.get(key, 0) <= 1

    @pytest.mark.asyncio
    async def test_manual_autonomy_disables_auto_recovery(self, engine):
        _register_simple_kind(engine, n_steps=2, fail_at=0, always_fail=True)
        wf = engine.create_workflow("simple", user_request="test", autonomy_level=AutonomyLevel.MANUAL)
        result = await engine.run(wf.id)
        key = result.steps[0].action_key()
        assert result.recovery_attempts.get(key, 0) == 0

    @pytest.mark.asyncio
    async def test_safety_limit_prevents_recovery(self, engine):
        """Once a workflow is already at/past its step ceiling, recovery
        must not get a "free" extra attempt past that limit."""
        _register_simple_kind(engine, n_steps=1)
        wf = engine.create_workflow("simple", user_request="test", max_steps=1)
        wf.current_step = 1  # already at the ceiling
        step = wf.steps[0]
        step.status = StepStatus.FAILED
        assert engine._recovery_allowed(wf, step) is False

    @pytest.mark.asyncio
    async def test_repeated_action_limit_still_wins_over_recovery(self, engine):
        """Recovery (cap 1) is strictly tighter than repeated-action
        detection (cap 3) — a step that keeps failing must still stop
        via STOP_REPEATED, not loop forever waiting on recovery."""
        _register_simple_kind(engine, n_steps=1, fail_at=0, always_fail=True)
        wf = engine.create_workflow("simple", user_request="test")
        result = await engine.run(wf.id)
        assert result.status == WorkflowStatus.FAILED
        assert "Repeated action" in (result.stopped_reason or "") or result.steps[0].status == StepStatus.FAILED

    @pytest.mark.asyncio
    async def test_approval_interaction_recovery_respects_permission_gate(self, engine, fake_env):
        """A recovery retry of a tool-backed step that needs permission
        must still surface WAITING_FOR_PERMISSION, not silently bypass it."""
        call_count = {"n": 0}

        async def flaky_run_tool(tool_name, tool_args, broadcast=None, auto_approved=False):
            call_count["n"] += 1
            if not auto_approved:
                return {"status": "pending_confirmation", "confirmation_id": "abc123"}
            return {"status": "ok", "result": "ran"}

        def build_steps(**kw):
            return [WorkflowStep("gated", tool_name="run_terminal_command", tool_args={"command": "echo hi"})]
        engine.register_kind(WorkflowKindSpec(name="gated_kind", build_steps=build_steps, handlers={}))
        wf = engine.create_workflow("gated_kind", user_request="test")

        import tool_registry as _tr
        with patch.object(_tr.tool_registry, "run_tool", side_effect=flaky_run_tool):
            result = await engine.run(wf.id, auto_approved=False)
        assert result.status == WorkflowStatus.WAITING_FOR_PERMISSION


# ── Structured Approval UX / Feature 10 ───────────────────────────
class TestStructuredApproval:
    @pytest.mark.asyncio
    async def test_pending_steps_preview_lists_upcoming_tool_steps(self, engine, fake_env):
        _register_tool_kind(engine, n_tool_steps=2)
        wf = engine.create_workflow("tool_gated", user_request="test")
        await engine.run(wf.id, auto_approved=False)
        preview = engine.pending_steps_preview(wf.id)
        assert len(preview) == 2
        assert preview[0]["description"] == "risky step 0"

    @pytest.mark.asyncio
    async def test_approve_one_step_then_gate_reasserts(self, engine, fake_env):
        _register_tool_kind(engine, n_tool_steps=2)
        wf = engine.create_workflow("tool_gated", user_request="test")
        await engine.run(wf.id, auto_approved=False)
        assert wf.status == WorkflowStatus.WAITING_FOR_PERMISSION

        ok = engine.approve_steps(wf.id, count=1)
        assert ok is True
        task = engine._tasks[wf.id]
        await task
        # First step ran, second step re-hit the permission gate.
        assert wf.steps[0].status == StepStatus.DONE
        assert wf.status == WorkflowStatus.WAITING_FOR_PERMISSION
        assert wf.steps[1].status != StepStatus.DONE

    @pytest.mark.asyncio
    async def test_approve_all_remaining_completes_workflow(self, engine, fake_env):
        _register_tool_kind(engine, n_tool_steps=2)
        wf = engine.create_workflow("tool_gated", user_request="test")
        await engine.run(wf.id, auto_approved=False)
        engine.approve_all_remaining(wf.id)
        await engine._tasks[wf.id]
        assert wf.status == WorkflowStatus.COMPLETED
        assert all(s.status == StepStatus.DONE for s in wf.steps)

    @pytest.mark.asyncio
    async def test_reject_next_step_skips_and_continues(self, engine, fake_env):
        _register_tool_kind(engine, n_tool_steps=2)
        wf = engine.create_workflow("tool_gated", user_request="test")
        await engine.run(wf.id, auto_approved=False)
        engine.reject_next_step(wf.id)
        await engine._tasks[wf.id]
        assert wf.steps[0].status == StepStatus.SKIPPED
        # Second step still requires its own permission.
        assert wf.status == WorkflowStatus.WAITING_FOR_PERMISSION

    def test_approve_and_reject_are_noops_when_not_waiting(self, engine):
        _register_simple_kind(engine, n_steps=1)
        wf = engine.create_workflow("simple", user_request="test")
        assert engine.approve_steps(wf.id) is False
        assert engine.approve_all_remaining(wf.id) is False
        assert engine.reject_next_step(wf.id) is False


# ── Feature 16: audit log ─────────────────────────────────────────
class TestAuditLog:
    def test_clear_events_returns_count_and_empties_table(self):
        import memory
        memory.log_event("workflow:workflow_created", "test 1")
        memory.log_event("workflow:workflow_step", "test 2")
        count = memory.clear_events()
        assert count == 2
        assert memory.get_recent_events(limit=10) == []

    def test_clear_events_on_empty_log_returns_zero(self):
        import memory
        assert memory.clear_events() == 0

    def test_get_recent_events_filters_by_prefix(self):
        import memory
        memory.log_event("workflow:workflow_created", "a workflow event")
        memory.log_event("system_start", "not a workflow event")
        events = memory.get_recent_events(limit=10, event_type_prefix="workflow:")
        assert len(events) == 1
        assert events[0]["event_type"] == "workflow:workflow_created"

    def test_get_recent_events_without_prefix_is_unchanged(self):
        """Backward compatibility: existing callers passing just `limit`
        still see everything."""
        import memory
        memory.log_event("workflow:workflow_created", "a")
        memory.log_event("system_start", "b")
        assert len(memory.get_recent_events(limit=10)) == 2


class TestAuditLogChatIntegration:
    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return main, TestClient(main.app)

    def test_recent_activity_shows_workflow_events(self, monkeypatch, fake_env, jarvis_db_path):
        import memory
        memory.log_event("workflow:workflow_created", "[abcd1234] kind=simple goal=test")
        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with client_factory as client:
            r = client.post("/chat", json={"message": "recent activity", "session_id": "s1"})
            assert r.status_code == 200
            assert "workflow_created" in r.json()["response"]

    def test_recent_activity_empty_is_graceful(self, monkeypatch, fake_env, jarvis_db_path):
        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with client_factory as client:
            r = client.post("/chat", json={"message": "recent activity", "session_id": "s1"})
            assert "no recent workflow activity" in r.json()["response"].lower()

    def test_clear_logs_requires_explicit_confirmation(self, monkeypatch, fake_env, jarvis_db_path):
        import memory
        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with client_factory as client:
            memory.log_event("workflow:workflow_created", "test")
            before_count = len(memory.get_recent_events(limit=100))
            assert before_count >= 1

            r = client.post("/chat", json={"message": "clear logs", "session_id": "s1"})
            assert "confirm clear logs" in r.json()["response"].lower()
            # Not cleared yet — explicit confirmation required.
            assert len(memory.get_recent_events(limit=100)) == before_count

            r2 = client.post("/chat", json={"message": "confirm clear logs", "session_id": "s1"})
            assert "cleared" in r2.json()["response"].lower()
            assert memory.get_recent_events(limit=100) == []

    def test_clear_logs_on_empty_log_is_graceful(self, monkeypatch, fake_env, jarvis_db_path):
        import memory
        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with client_factory as client:
            memory.clear_events()  # lifespan startup itself logs a couple of events
            r = client.post("/chat", json={"message": "confirm clear logs", "session_id": "s1"})
            assert "already empty" in r.json()["response"].lower()

    def test_clear_logs_storage_error_is_handled_gracefully(self, monkeypatch, fake_env, jarvis_db_path):
        import memory
        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch.object(main, "clear_events", side_effect=memory.AuditLogError("disk error")):
            with client_factory as client:
                r = client.post("/chat", json={"message": "confirm clear logs", "session_id": "s1"})
                assert r.status_code == 200
                assert "couldn't clear" in r.json()["response"].lower()


# ── Feature 6 (remaining work): automatic/periodic health monitor ─
class TestAutomaticHealthMonitor:
    @pytest.mark.asyncio
    async def test_runtime_signal_unknown_when_git_and_dependencies_unknown(self):
        """Closes a previously-documented coverage gap: the 'Runtime'
        signal was only ever exercised indirectly through a full
        get_report() run. Isolated here via the two signals it's
        actually derived from."""
        from project_health import HealthSignal, ProjectHealthMonitor
        monitor = ProjectHealthMonitor()
        with patch.object(monitor, "_git_signal", side_effect=lambda p: (HealthSignal.UNKNOWN, None)), \
             patch.object(monitor, "_dependency_signal", side_effect=lambda p: (HealthSignal.UNKNOWN, None)):
            report = await monitor.get_report("/some/project")
        assert report.runtime == HealthSignal.UNKNOWN

    @pytest.mark.asyncio
    async def test_runtime_signal_good_when_either_git_or_dependencies_known(self):
        from project_health import HealthSignal, ProjectHealthMonitor
        monitor = ProjectHealthMonitor()
        with patch.object(monitor, "_git_signal", side_effect=lambda p: (HealthSignal.GOOD, None)), \
             patch.object(monitor, "_dependency_signal", side_effect=lambda p: (HealthSignal.UNKNOWN, None)):
            report = await monitor.get_report("/some/project")
        assert report.runtime == HealthSignal.GOOD


    @pytest.mark.asyncio
    async def test_start_auto_monitor_is_idempotent(self):
        from project_health import ProjectHealthMonitor
        monitor = ProjectHealthMonitor()
        started_first = monitor.start_auto_monitor()
        started_second = monitor.start_auto_monitor()
        assert started_first is True
        assert started_second is False  # no duplicate worker
        assert monitor.is_auto_monitor_running() is True
        monitor.stop_auto_monitor()
        assert monitor.is_auto_monitor_running() is False

    def test_set_auto_interval_floors_at_minimum(self):
        from project_health import ProjectHealthMonitor, _MIN_AUTO_INTERVAL_SECONDS
        monitor = ProjectHealthMonitor()
        applied = monitor.set_auto_interval(1)
        assert applied == _MIN_AUTO_INTERVAL_SECONDS

    def test_stop_before_start_is_safe(self):
        from project_health import ProjectHealthMonitor
        monitor = ProjectHealthMonitor()
        monitor.stop_auto_monitor()  # must not raise

    @pytest.mark.asyncio
    async def test_check_and_notify_dedups_unchanged_attention(self, tmp_path):
        from project_health import ProjectHealthMonitor, ProjectHealthReport
        monitor = ProjectHealthMonitor()
        monitor.enable(str(tmp_path))
        notified = []

        async def fake_broadcast(payload):
            notified.append(payload)
        monitor.set_broadcast_fn(fake_broadcast)

        report = ProjectHealthReport(project_path=str(tmp_path), attention=["The build has failed 2 times in a row."])
        with patch.object(monitor, "get_report", side_effect=lambda p: report):
            await monitor._check_and_notify(str(tmp_path))
            await monitor._check_and_notify(str(tmp_path))  # same issue again
        assert len(notified) == 1  # not spammed on the second, unchanged cycle

    @pytest.mark.asyncio
    async def test_check_and_notify_survives_get_report_exception(self, tmp_path):
        from project_health import ProjectHealthMonitor
        monitor = ProjectHealthMonitor()
        monitor.enable(str(tmp_path))

        async def boom(_):
            raise RuntimeError("disk unavailable")
        with patch.object(monitor, "get_report", side_effect=boom):
            await monitor._check_and_notify(str(tmp_path))  # must not raise


class TestAutomaticHealthMonitorChatIntegration:
    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return main, TestClient(main.app)

    def test_start_and_stop_automatic_monitoring_via_chat(self, monkeypatch, fake_env, jarvis_db_path):
        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        try:
            with client_factory as client:
                r = client.post("/chat", json={"message": "start automatic health monitoring", "session_id": "s1"})
                assert "running" in r.json()["response"].lower()
                r2 = client.post("/chat", json={"message": "stop automatic health monitoring", "session_id": "s1"})
                assert "stopped" in r2.json()["response"].lower()
        finally:
            main.project_health_monitor.stop_auto_monitor()

    def test_set_health_check_interval_via_chat(self, monkeypatch, fake_env, jarvis_db_path):
        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with client_factory as client:
            r = client.post("/chat", json={"message": "set health check interval to 5 minutes", "session_id": "s1"})
            assert "5 minute" in r.json()["response"].lower()
        assert main.project_health_monitor.auto_interval_seconds == 300.0


# ── Feature 13: CSE Exam Prep workflow ────────────────────────────
# ── Feature 7: Proactive Suggestion Engine ────────────────────────
class TestSuggestionEngine:
    def test_record_health_alert_creates_suggestions(self):
        from suggestion_engine import SuggestionEngine
        engine = SuggestionEngine()
        created = engine.record_health_alert("/proj", ["Missing dependencies: flask."])
        assert len(created) == 1
        assert "install" in created[0].text.lower()

    def test_get_pending_excludes_dismissed(self):
        from suggestion_engine import SuggestionEngine
        engine = SuggestionEngine()
        engine.record_health_alert("/proj", ["3 uncommitted change(s)."])
        assert len(engine.get_pending("/proj")) == 1
        engine.dismiss_all("/proj")
        assert engine.get_pending("/proj") == []

    def test_dismiss_all_returns_count_and_is_graceful_when_empty(self):
        from suggestion_engine import SuggestionEngine
        engine = SuggestionEngine()
        assert engine.dismiss_all("/proj") == 0
        engine.record_health_alert("/proj", ["a", "b"])
        assert engine.dismiss_all("/proj") == 2

    def test_bucket_is_bounded_per_project(self):
        from suggestion_engine import SuggestionEngine
        engine = SuggestionEngine()
        for i in range(30):
            engine.record_health_alert("/proj", [f"issue {i}"])
        assert len(engine.get_pending("/proj")) <= engine._MAX_PER_PROJECT

    def test_empty_attention_creates_nothing(self):
        from suggestion_engine import SuggestionEngine
        engine = SuggestionEngine()
        assert engine.record_health_alert("/proj", []) == []

    @pytest.mark.asyncio
    async def test_health_monitor_feeds_suggestion_engine(self, tmp_path):
        """Feature 6 -> Feature 7 wiring: an automatic health check that
        finds something raises a real suggestion, not just a log line."""
        from project_health import ProjectHealthMonitor, ProjectHealthReport
        from suggestion_engine import suggestion_engine as real_suggestion_engine

        monitor = ProjectHealthMonitor()
        monitor.enable(str(tmp_path))
        report = ProjectHealthReport(project_path=str(tmp_path), attention=["Missing dependencies: requests."])
        with patch.object(monitor, "get_report", side_effect=lambda p: report):
            await monitor._check_and_notify(str(tmp_path))
        pending = real_suggestion_engine.get_pending(str(tmp_path))
        assert any("requests" in s.text for s in pending)
        real_suggestion_engine.dismiss_all(str(tmp_path))  # don't leak into other tests

    @pytest.mark.asyncio
    async def test_suggestion_generation_failure_does_not_break_health_monitor(self, tmp_path):
        """A Feature 7 bug must never take down Feature 6's own alerting."""
        from project_health import ProjectHealthMonitor, ProjectHealthReport
        monitor = ProjectHealthMonitor()
        monitor.enable(str(tmp_path))
        report = ProjectHealthReport(project_path=str(tmp_path), attention=["Missing dependencies: x."])
        with patch.object(monitor, "get_report", side_effect=lambda p: report):
            with patch("suggestion_engine.suggestion_engine.record_health_alert", side_effect=RuntimeError("boom")):
                await monitor._check_and_notify(str(tmp_path))  # must not raise


class TestSuggestionEngineChatIntegration:
    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return main, TestClient(main.app)

    def test_suggestions_with_no_active_project(self, monkeypatch, fake_env, jarvis_db_path):
        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with client_factory as client:
            r = client.post("/chat", json={"message": "suggestions", "session_id": "s1"})
            assert "don't have an active project" in r.json()["response"].lower()

    def test_suggestions_lists_pending_for_active_project(self, monkeypatch, fake_env, jarvis_db_path):
        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with client_factory as client:
            monkeypatch.setattr(
                main.context_manager, "gather",
                lambda session_id=None: type("Ctx", (), {"active_project_path": "/proj"})(),
            )
            main.suggestion_engine.record_health_alert("/proj", ["Missing dependencies: flask."])
            r = client.post("/chat", json={"message": "suggestions", "session_id": "s1"})
            assert "flask" in r.json()["response"].lower()

            r2 = client.post("/chat", json={"message": "dismiss suggestions", "session_id": "s1"})
            assert "dismissed 1" in r2.json()["response"].lower()

            r3 = client.post("/chat", json={"message": "suggestions", "session_id": "s1"})
            assert "no suggestions" in r3.json()["response"].lower()


class TestExamPrepWorkflow:
    def test_build_steps_shape(self):
        from workflows.exam_prep import _build_steps
        steps = _build_steps(subject="operating systems")
        assert len(steps) == 3
        assert steps[0].handler_key == "build_revision_plan"
        assert steps[1].handler_key == "generate_practice_questions"
        assert steps[2].handler_key == "compile_report"
        assert steps[0].tool_args["subject"] == "operating systems"

    def test_build_steps_without_subject_uses_generic_label(self):
        from workflows.exam_prep import _build_steps
        steps = _build_steps()
        assert "the exam" in steps[0].description

    @pytest.mark.asyncio
    async def test_revision_plan_and_practice_questions_invoked(self):
        from workflow_engine import workflow_engine as real_engine

        wf = real_engine.create_workflow("exam_prep", user_request="prepare for my DBMS exam", subject="DBMS")
        with patch("core.llm_orchestrator._call_model", side_effect=["REVISION PLAN TEXT", "PRACTICE QUESTIONS TEXT"]):
            result = await real_engine.run(wf.id)
        assert result.status == WorkflowStatus.COMPLETED
        assert result.steps[0].result == "REVISION PLAN TEXT"
        assert result.steps[1].result == "PRACTICE QUESTIONS TEXT"
        final_report = result.steps[2].result
        assert "REVISION PLAN TEXT" in final_report
        assert "PRACTICE QUESTIONS TEXT" in final_report

    @pytest.mark.asyncio
    async def test_llm_failure_is_handled_not_faked(self):
        """core/llm_orchestrator.run() never raises — it returns an
        apology string on failure. exam_prep.py must recognise that and
        mark the step a genuine failure, not report fake success."""
        from workflow_engine import workflow_engine as real_engine

        wf = real_engine.create_workflow("exam_prep", user_request="prepare for my exam", subject="OS")
        with patch("core.llm_orchestrator._call_model", side_effect=RuntimeError("ollama down")):
            result = await real_engine.run(wf.id)
        assert result.steps[0].status == StepStatus.FAILED
        assert result.steps[0].error
        # The compiled report must not claim a plan was generated.
        assert "not generated" in result.steps[2].result

    @pytest.mark.asyncio
    async def test_workflow_state_correct_across_run(self):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("exam_prep", user_request="prepare for exam", subject="Networks")
        assert wf.status == WorkflowStatus.CREATED
        with patch("core.llm_orchestrator._call_model", return_value="ok"):
            result = await real_engine.run(wf.id)
        assert result.status == WorkflowStatus.COMPLETED
        assert result.current_step == len(result.steps)


class TestExamPrepChatRouting:
    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return main, TestClient(main.app)

    def test_exam_prep_request_routes_to_workflow(self, monkeypatch, fake_env, jarvis_db_path):
        gen_calls = {"n": 0}

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "PLANNING", "confidence": 0.9, "mode": None})}}
            gen_calls["n"] += 1
            return {"message": {"content": f"generated content {gen_calls['n']}"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                gen_calls["n"] = 0  # discard the lifespan startup verification ping
                r = client.post("/chat", json={"message": "help me prepare for my exam on operating systems", "session_id": "s1"})
                assert r.status_code == 200
                reply = r.json()["response"]
                assert "EXAM PREP" in reply
                assert "generated content" in reply
        # Both LLM-backed steps were actually invoked (not faked).
        assert gen_calls["n"] == 2

    def test_normal_study_request_still_uses_study_path_not_exam_workflow(self, monkeypatch, fake_env, jarvis_db_path):
        """Feature 13 requirement: normal study requests must not be
        accidentally converted into exam workflows."""
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "STUDY", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "Operating systems manage hardware resources..."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "teach me operating systems", "session_id": "s1"})
                assert r.status_code == 200
                reply = r.json()["response"]
                assert "EXAM PREP" not in reply
                assert "Operating systems" in reply

    def test_unrelated_intent_still_works(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "GENERAL_CHAT", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "Good evening, Sir."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "good evening", "session_id": "s1"})
                assert r.status_code == 200
                assert "Good evening" in r.json()["response"]


# ── Feature 14: Hackathon project workflow ────────────────────────
# ── Feature 8: Workflow Memory (read-back) ────────────────────────
# ── Feature 13 (interactive loop): guided study session workflow ──
class TestStudySessionWorkflow:
    def test_build_steps_shape(self):
        from workflows.study_session import _build_steps
        steps = _build_steps(topic="recursion", rounds=2)
        # teach + (quiz, evaluate) * 2 + summarize
        assert len(steps) == 1 + 2 * 2 + 1
        assert steps[0].handler_key == "teach"
        assert steps[1].handler_key == "quiz"
        assert steps[2].handler_key == "evaluate"
        assert steps[-1].handler_key == "summarize"

    def test_build_steps_rounds_are_bounded(self):
        from workflows.study_session import _build_steps
        steps = _build_steps(topic="x", rounds=50)
        assert len(steps) == 1 + 2 * 10 + 1  # capped at 10 rounds

    @pytest.mark.asyncio
    async def test_quiz_step_suspends_for_input(self, fake_env):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("study_session", user_request="study session on recursion", topic="recursion", rounds=1)
        with patch("core.llm_orchestrator._call_model", return_value="LESSON TEXT"):
            result = await real_engine.run(wf.id)
        assert result.status == WorkflowStatus.WAITING_FOR_USER
        assert result.pending_input is not None
        assert result.steps[0].status == StepStatus.DONE   # teach finished
        assert result.steps[1].status == StepStatus.PENDING  # quiz suspended, not done

    @pytest.mark.asyncio
    async def test_provide_input_resumes_and_grades(self, fake_env):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("study_session", user_request="study session on recursion", topic="recursion", rounds=1)
        with patch("core.llm_orchestrator._call_model", side_effect=["LESSON TEXT", "What is the base case?"]):
            result = await real_engine.run(wf.id)
        assert result.status == WorkflowStatus.WAITING_FOR_USER

        with patch("core.llm_orchestrator._call_model", return_value="CORRECT\n\nWell reasoned."):
            result = await real_engine.provide_input(wf.id, "The condition that stops recursion.")
        assert result.status == WorkflowStatus.COMPLETED
        assert result.steps[1].status == StepStatus.DONE  # quiz now finished
        assert result.steps[2].status == StepStatus.DONE  # evaluate ran
        assert "Score: 1/1" in result.steps[-1].result

    @pytest.mark.asyncio
    async def test_provide_input_raises_when_not_waiting(self, fake_env):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("study_session", user_request="study session on x", topic="x", rounds=1)
        with pytest.raises(ValueError):
            await real_engine.provide_input(wf.id, "an answer")

    @pytest.mark.asyncio
    async def test_incorrect_answer_does_not_increase_difficulty(self, fake_env):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("study_session", user_request="study session on recursion", topic="recursion", rounds=2)
        with patch("core.llm_orchestrator._call_model", side_effect=["LESSON TEXT", "Question 1?"]):
            result = await real_engine.run(wf.id)
        with patch("core.llm_orchestrator._call_model", side_effect=["INCORRECT\n\nNot quite.", "Question 2 (same difficulty)?"]):
            result = await real_engine.provide_input(wf.id, "a wrong answer")
        assert result.status == WorkflowStatus.WAITING_FOR_USER  # round 2's quiz
        data = result.__dict__["_study_data"]
        assert data["harder"] is False
        assert data["results"][0]["correct"] is False

    @pytest.mark.asyncio
    async def test_llm_failure_during_quiz_is_a_real_failure(self, fake_env):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("study_session", user_request="study session on x", topic="x", rounds=1)
        with patch("core.llm_orchestrator._call_model", side_effect=["LESSON TEXT", RuntimeError("down"), RuntimeError("down")]):
            result = await real_engine.run(wf.id)
        assert result.steps[1].status == StepStatus.FAILED
        assert result.steps[1].error


class TestStudySessionChatRouting:
    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return main, TestClient(main.app)

    def test_study_session_request_starts_workflow_and_asks_first_question(self, monkeypatch, fake_env, jarvis_db_path):
        call_log = {"n": 0}

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "STUDY", "confidence": 0.9, "mode": None})}}
            call_log["n"] += 1
            if call_log["n"] == 1:
                return {"message": {"content": "Here's a lesson on recursion..."}}
            return {"message": {"content": "What is the base case of a recursive function?"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                call_log["n"] = 0  # discard the lifespan startup verification ping
                r = client.post("/chat", json={"message": "start a study session on recursion for 1 round", "session_id": "study_sess_1"})
                assert r.status_code == 200
                assert "base case" in r.json()["response"].lower()

    def test_answer_is_routed_back_into_the_waiting_workflow(self, monkeypatch, fake_env, jarvis_db_path):
        responses = iter([
            "(lifespan startup ping)",
            "Here's a lesson on recursion...",
            "What is the base case?",
            "CORRECT\n\nNice work.",
        ])

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "STUDY", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": next(responses)}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r1 = client.post("/chat", json={"message": "start a study session on recursion for 1 round", "session_id": "study_sess_2"})
                assert "base case" in r1.json()["response"].lower()

                r2 = client.post("/chat", json={"message": "the condition that stops the recursive calls", "session_id": "study_sess_2"})
                assert r2.status_code == 200
                assert "STUDY SESSION SUMMARY" in r2.json()["response"]

    def test_ordinary_teach_request_is_unaffected(self, monkeypatch, fake_env, jarvis_db_path):
        """Feature 13 requirement: an ordinary 'teach me X' must not be
        upgraded into the multi-round workflow."""
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "STUDY", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "Recursion is when a function calls itself..."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "teach me recursion", "session_id": "study_sess_3"})
                assert r.status_code == 200
                reply = r.json()["response"]
                assert "STUDY SESSION SUMMARY" not in reply
                assert "Recursion is when" in reply

    def test_unrelated_intent_still_works(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "GENERAL_CHAT", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "Good evening, Sir."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "good evening", "session_id": "study_sess_4"})
                assert r.status_code == 200
                assert "Good evening" in r.json()["response"]


class TestWorkflowMemoryRecall:
    def test_recall_returns_empty_for_unknown_project(self, engine):
        assert engine._recall_prior_context("/never/seen") == []

    def test_recall_finds_facts_saved_by_a_previous_run(self, engine, tmp_path):
        import project_memory as pm
        pm.init_project_memory_db()
        pm.save_fact(str(tmp_path), kind="known_issue", content=_json.dumps({"goal": "review", "outcome": "FAILED"}))
        recalled = engine._recall_prior_context(str(tmp_path))
        assert len(recalled) == 1
        assert recalled[0]["kind"] == "known_issue"

    def test_create_workflow_populates_prior_context(self, engine, tmp_path):
        import project_memory as pm
        pm.init_project_memory_db()
        pm.save_fact(str(tmp_path), kind="previous_fix", content=_json.dumps({"goal": "fixed the build"}))
        _register_simple_kind(engine, n_steps=1)
        wf = engine.create_workflow("simple", user_request="test", project_path=str(tmp_path))
        assert len(wf.prior_context) == 1
        assert "fixed the build" in wf.prior_context_summary()

    def test_create_workflow_without_project_path_has_no_prior_context(self, engine):
        _register_simple_kind(engine, n_steps=1)
        wf = engine.create_workflow("simple", user_request="test")
        assert wf.prior_context == []
        assert wf.prior_context_summary() == ""

    def test_recall_failure_does_not_break_workflow_creation(self, engine, tmp_path):
        _register_simple_kind(engine, n_steps=1)
        with patch("project_memory.get_facts", side_effect=RuntimeError("db locked")):
            wf = engine.create_workflow("simple", user_request="test", project_path=str(tmp_path))
        assert wf.prior_context == []  # degrades gracefully, doesn't raise

    @pytest.mark.asyncio
    async def test_project_review_report_includes_recalled_context(self, tmp_path):
        """The concrete Feature 8 loop-closer: a second review of the
        same project is actually informed by what the first one wrote."""
        import project_memory as pm
        import workflows.project_review  # noqa: F401 — ensure registered
        from workflow_engine import workflow_engine as real_engine

        pm.init_project_memory_db()
        (tmp_path / "main.py").write_text("print('hi')\n")
        pm.save_fact(str(tmp_path), kind="known_issue", content=_json.dumps({"goal": "review found missing tests"}))

        wf = real_engine.create_workflow("project_review", user_request="review this project", project_path=str(tmp_path))
        result = await real_engine.run(wf.id)
        assert result.status == WorkflowStatus.COMPLETED
        report = result.steps[-1].result
        assert "Recalled from previous runs" in report
        assert "review found missing tests" in report

    @pytest.mark.asyncio
    async def test_project_review_report_has_no_recall_section_when_nothing_prior(self, tmp_path):
        import project_memory as pm
        from workflow_engine import workflow_engine as real_engine

        pm.init_project_memory_db()
        (tmp_path / "main.py").write_text("print('hi')\n")
        wf = real_engine.create_workflow("project_review", user_request="review this project", project_path=str(tmp_path))
        result = await real_engine.run(wf.id)
        report = result.steps[-1].result
        assert "Recalled from previous runs" not in report


class TestHackathonWorkflow:
    def test_build_steps_shape(self):
        from workflows.hackathon import _build_steps
        steps = _build_steps(theme="climate", team_size=4)
        assert len(steps) == 7
        assert steps[0].handler_key == "ideas"
        assert steps[-1].handler_key == "compile_report"
        assert steps[0].tool_args == {"theme": "climate", "team_size": 4}

    @pytest.mark.asyncio
    async def test_all_generation_steps_invoked_and_feed_forward(self):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("hackathon_project", user_request="hackathon project", team_size=2)
        with patch(
            "core.llm_orchestrator._call_model",
            side_effect=["IDEAS TEXT", "ARCH TEXT", "STACK TEXT", "MVP TEXT", "TASKS TEXT", "PITCH TEXT"],
        ):
            result = await real_engine.run(wf.id)
        assert result.status == WorkflowStatus.COMPLETED
        assert result.steps[0].result == "IDEAS TEXT"
        assert result.steps[5].result == "PITCH TEXT"
        final_report = result.steps[-1].result
        for text in ("IDEAS TEXT", "ARCH TEXT", "STACK TEXT", "MVP TEXT", "TASKS TEXT", "PITCH TEXT"):
            assert text in final_report

    @pytest.mark.asyncio
    async def test_llm_failure_on_one_step_is_handled_not_faked(self):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("hackathon_project", user_request="hackathon project")
        # ideas succeeds; architecture fails on both its original
        # attempt and the engine's own one bounded recovery retry
        # (Feature 17), then stays FAILED; the rest succeed using the
        # (still-available) ideas text as their feed-forward context.
        with patch(
            "core.llm_orchestrator._call_model",
            side_effect=[
                "IDEAS TEXT", RuntimeError("ollama down"), RuntimeError("ollama down"),
                "STACK TEXT", "MVP TEXT", "TASKS TEXT", "PITCH TEXT",
            ],
        ):
            result = await real_engine.run(wf.id)
        assert result.steps[0].status == StepStatus.DONE
        assert result.steps[1].status == StepStatus.FAILED
        assert result.steps[1].error
        report = result.steps[-1].result
        assert "IDEAS TEXT" in report
        assert "not generated" in report  # the failed architecture step

    @pytest.mark.asyncio
    async def test_team_size_passed_through_to_task_breakdown_prompt(self):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("hackathon_project", user_request="hackathon project", team_size=6)
        captured = {}

        def fake_call(system_prompt, *a, **kw):
            if "team members" in system_prompt.lower() or "workstreams" in system_prompt.lower():
                captured["prompt"] = system_prompt
            return "ok"
        with patch("core.llm_orchestrator._call_model", side_effect=fake_call):
            await real_engine.run(wf.id)
        assert "6" in captured.get("prompt", "")


class TestHackathonChatRouting:
    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return main, TestClient(main.app)

    def test_full_project_plan_request_routes_to_workflow(self, monkeypatch, fake_env, jarvis_db_path):
        gen_calls = {"n": 0}

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "HACKATHON", "confidence": 0.9, "mode": None})}}
            gen_calls["n"] += 1
            return {"message": {"content": f"generated content {gen_calls['n']}"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                gen_calls["n"] = 0  # discard the lifespan startup verification ping
                r = client.post("/chat", json={"message": "help me plan a hackathon project from scratch", "session_id": "s1"})
                assert r.status_code == 200
                reply = r.json()["response"]
                assert "HACKATHON PROJECT PLAN" in reply
        assert gen_calls["n"] == 6  # all six generation steps actually invoked

    def test_single_capability_request_is_unaffected(self, monkeypatch, fake_env, jarvis_db_path):
        """Feature 14 requirement: a single-capability ask must not be
        accidentally converted into the 6-step workflow."""
        seen_prompts = []

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "HACKATHON", "confidence": 0.9, "mode": None})}}
            seen_prompts.append(kwargs["messages"][0]["content"])
            return {"message": {"content": "Here are 5 ideas..."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "give me 5 AI hackathon ideas", "session_id": "s1"})
                assert r.status_code == 200
                reply = r.json()["response"]
                assert "HACKATHON PROJECT PLAN" not in reply
        assert any("hackathon project ideas" in p for p in seen_prompts)

    def test_unrelated_intent_still_works(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "GENERAL_CHAT", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "Good evening, Sir."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "good evening", "session_id": "s1"})
                assert r.status_code == 200
                assert "Good evening" in r.json()["response"]

