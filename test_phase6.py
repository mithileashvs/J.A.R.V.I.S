"""
JARVIS Phase 6 test suite — Central Workflow Engine.

Run with: pytest test_phase6.py -v
Requires: requirements-dev.txt (pytest, pytest-asyncio), same as
test_phase3.py/test_phase4.py/test_phase5.py.
"""

import asyncio
import textwrap
from unittest.mock import patch

import pytest

import workflows  # noqa: F401 — registers all workflow kinds (project_review, dev_env_prep, ...)
                    # up front so test order never matters, rather than relying on an
                    # incidental `import workflows.project_review` inside one test method.

from workflow_engine import (
    AutonomyLevel,
    StepOutcome,
    StepResult,
    StepStatus,
    Workflow,
    WorkflowEngine,
    WorkflowKindSpec,
    WorkflowStatus,
    WorkflowStep,
)


# ── Fixtures ─────────────────────────────────────────────────────
@pytest.fixture
def jarvis_db_path(monkeypatch, tmp_path):
    """Matches test_phase3.py's fixture of the same name — kept here too
    since test files don't share fixtures across modules in this suite."""
    db_path = str(tmp_path / "test_jarvis_chat.db")
    monkeypatch.setenv("JARVIS_DB_PATH", db_path)
    return db_path


@pytest.fixture(autouse=True)
def isolate_shared_db(tmp_path, monkeypatch):
    """
    memory.py and project_memory.py each do `from config import DB_PATH`,
    binding that name at import time — by the time test_phase6.py runs,
    both modules are almost certainly already imported (by test_phase3.py
    et al.), so monkeypatching the JARVIS_DB_PATH env var alone (as the
    jarvis_db_path fixture in test_phase3.py does) would have no effect
    here. Patch the already-bound module attributes directly instead, so
    every real workflow run in this file (project_review persists facts
    via project_memory.save_fact, workflow_engine audits via
    memory.log_event) writes to an isolated per-test DB and never touches
    the real jarvis_memory.db or another test file's data.
    """
    db_path = str(tmp_path / "test_phase6.db")
    monkeypatch.setenv("JARVIS_DB_PATH", db_path)
    import memory
    import project_memory
    monkeypatch.setattr(memory, "DB_PATH", db_path)
    monkeypatch.setattr(project_memory, "DB_PATH", db_path)
    memory.init_db()
    project_memory.init_project_memory_db()


@pytest.fixture
def fake_env(monkeypatch):
    """config.py's validate() raises at import time without these — matches
    the fixture of the same name in test_phase3.py/test_phase4.py/test_phase5.py."""
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake_key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fake_secret")


@pytest.fixture
def engine():
    """A fresh engine per test — the module-level singleton is for
    production wiring; tests get their own instance so registering
    test-only kinds never leaks between tests."""
    return WorkflowEngine()


def _register_simple_kind(engine, n_steps=3, fail_at=None, verified=True):
    """Registers a kind whose steps just record that they ran, optionally
    failing (every time) at a given index — used for repeated-action tests."""
    async def handler(workflow, step):
        idx = int(step.description.split()[-1])
        if fail_at is not None and idx == fail_at:
            return StepResult(summary="failed on purpose", verified_success=False, error="synthetic failure")
        return StepResult(summary=f"did step {idx}", verified_success=verified, evidence=f"evidence for step {idx}")

    def build_steps(**kwargs):
        return [WorkflowStep(f"do thing {i}", handler_key="h") for i in range(n_steps)]

    engine.register_kind(WorkflowKindSpec(name="simple", build_steps=build_steps, handlers={"h": handler}))


# ── Workflow creation / lifecycle ────────────────────────────────
class TestWorkflowLifecycle:
    def test_create_workflow_unknown_kind_raises(self, engine):
        with pytest.raises(ValueError):
            engine.create_workflow("nonexistent_kind", user_request="do it")

    def test_create_workflow_starts_in_created_status(self, engine):
        _register_simple_kind(engine)
        wf = engine.create_workflow("simple", user_request="test")
        assert wf.status == WorkflowStatus.CREATED
        assert wf.current_step == 0
        assert len(wf.steps) == 3

    @pytest.mark.asyncio
    async def test_run_to_completion(self, engine):
        _register_simple_kind(engine)
        wf = engine.create_workflow("simple", user_request="test")
        result = await engine.run(wf.id)
        assert result.status == WorkflowStatus.COMPLETED
        assert all(s.status == StepStatus.DONE for s in result.steps)
        assert all(s.outcome == StepOutcome.VERIFIED_SUCCESS for s in result.steps)

    @pytest.mark.asyncio
    async def test_run_records_evidence(self, engine):
        _register_simple_kind(engine)
        wf = engine.create_workflow("simple", user_request="test")
        result = await engine.run(wf.id)
        assert len(result.evidence) == 3
        assert "evidence for step 0" in result.evidence

    @pytest.mark.asyncio
    async def test_unhandled_handler_exception_fails_workflow_not_process(self, engine):
        async def bad_handler(workflow, step):
            raise RuntimeError("boom")

        engine.register_kind(WorkflowKindSpec(
            name="explode",
            build_steps=lambda **kw: [WorkflowStep("explode step", handler_key="h")],
            handlers={"h": bad_handler},
        ))
        wf = engine.create_workflow("explode", user_request="test")
        result = await engine.run(wf.id)
        assert result.steps[0].status == StepStatus.FAILED
        assert "boom" in result.steps[0].error


# ── Safety limits (Feature 15) ───────────────────────────────────
class TestSafetyLimits:
    @pytest.mark.asyncio
    async def test_max_steps_enforced(self, engine):
        _register_simple_kind(engine, n_steps=10)
        wf = engine.create_workflow("simple", user_request="test", max_steps=2)
        result = await engine.run(wf.id)
        assert result.status == WorkflowStatus.FAILED
        assert "Maximum step count" in result.stopped_reason
        assert result.current_step <= 2

    @pytest.mark.asyncio
    async def test_max_tool_calls_enforced(self, engine):
        # Each step maps to a SAFE tool so it actually increments tool_calls_made.
        def build_steps(**kw):
            return [
                WorkflowStep(f"safe tool call {i}", tool_name="get_weather", tool_args={"city": "London"})
                for i in range(10)
            ]
        engine.register_kind(WorkflowKindSpec(name="toolheavy", build_steps=build_steps))

        async def fake_run_tool(name, args, broadcast=None, auto_approved=False):
            return {"status": "ok", "result": "72F"}

        import tool_registry
        orig = tool_registry.tool_registry.run_tool
        tool_registry.tool_registry.run_tool = fake_run_tool
        try:
            wf = engine.create_workflow("toolheavy", user_request="test", max_tool_calls=3, max_steps=10)
            result = await engine.run(wf.id)
        finally:
            tool_registry.tool_registry.run_tool = orig

        assert result.status == WorkflowStatus.FAILED
        assert "Maximum tool-call count" in result.stopped_reason
        assert result.tool_calls_made == 3

    @pytest.mark.asyncio
    async def test_timeout_enforced(self, engine):
        _register_simple_kind(engine, n_steps=5)
        wf = engine.create_workflow("simple", user_request="test", timeout_seconds=0.0)
        result = await engine.run(wf.id)
        assert result.status == WorkflowStatus.TIMED_OUT

    @pytest.mark.asyncio
    async def test_repeated_failure_stops_workflow_safely(self, engine):
        """Feature 15's literal example: the same action failing 3x in a row stops the workflow."""
        async def always_fails(workflow, step):
            return StepResult(summary="nope", verified_success=False, error="same error every time")

        engine.register_kind(WorkflowKindSpec(
            name="looping",
            build_steps=lambda **kw: [WorkflowStep("retry the same thing", handler_key="h") for _ in range(5)],
            handlers={"h": always_fails},
        ))
        wf = engine.create_workflow("looping", user_request="test", max_steps=5)
        result = await engine.run(wf.id)
        assert result.status == WorkflowStatus.FAILED
        assert "Repeated action detected" in result.stopped_reason
        # Stopped after 3 attempts, not all 5 — this is the point of the safety limit.
        assert result.current_step < 5

    @pytest.mark.asyncio
    async def test_step_handler_timeout_recorded_as_failed_step(self, engine, monkeypatch):
        async def hangs(workflow, step):
            await asyncio.sleep(9999)

        engine.register_kind(WorkflowKindSpec(
            name="hangs",
            build_steps=lambda **kw: [WorkflowStep("hang forever", handler_key="h")],
            handlers={"h": hangs},
        ))
        import workflow_engine as we
        monkeypatch.setattr(we, "DEFAULT_PER_STEP_TIMEOUT_SECONDS", 0.05)
        wf = engine.create_workflow("hangs", user_request="test")
        result = await engine.run(wf.id)
        assert result.steps[0].status == StepStatus.FAILED
        assert "timed out" in result.steps[0].error


# ── Pause / resume / cancel (Feature 11) ─────────────────────────
class TestPauseResumeCancel:
    def test_cancel_unknown_workflow_returns_false(self, engine):
        assert engine.cancel("does-not-exist") is False

    def test_cancel_is_idempotent(self, engine):
        _register_simple_kind(engine)
        wf = engine.create_workflow("simple", user_request="test")
        assert engine.cancel(wf.id) is True
        assert engine.cancel(wf.id) is True  # second call: still safe, no crash

    @pytest.mark.asyncio
    async def test_cancel_before_run_stops_immediately(self, engine):
        _register_simple_kind(engine, n_steps=5)
        wf = engine.create_workflow("simple", user_request="test")
        engine.cancel(wf.id)
        result = await engine.run(wf.id)
        assert result.status == WorkflowStatus.CANCELLED
        assert result.current_step == 0  # no steps ran

    @pytest.mark.asyncio
    async def test_cancel_preserves_evidence_collected_so_far(self, engine):
        """Cancellation between steps must not discard already-collected evidence."""
        cancel_after_step = {"n": 0}

        async def handler(workflow, step):
            cancel_after_step["n"] += 1
            if cancel_after_step["n"] == 2:
                engine.cancel(workflow.id)
            return StepResult(summary="ok", verified_success=True, evidence=f"evidence {cancel_after_step['n']}")

        engine.register_kind(WorkflowKindSpec(
            name="cancel_mid",
            build_steps=lambda **kw: [WorkflowStep(f"step {i}", handler_key="h") for i in range(5)],
            handlers={"h": handler},
        ))
        wf = engine.create_workflow("cancel_mid", user_request="test")
        result = await engine.run(wf.id)
        assert result.status == WorkflowStatus.CANCELLED
        assert len(result.evidence) >= 2  # steps that already ran kept their evidence

    def test_pause_unknown_workflow_returns_false(self, engine):
        assert engine.pause("does-not-exist") is False

    def test_pause_terminal_workflow_returns_false(self, engine):
        _register_simple_kind(engine)
        wf = engine.create_workflow("simple", user_request="test")
        wf.status = WorkflowStatus.COMPLETED
        assert engine.pause(wf.id) is False

    @pytest.mark.asyncio
    async def test_pause_stops_before_next_step_and_resume_continues(self, engine):
        pause_after_step = {"n": 0}

        async def handler(workflow, step):
            pause_after_step["n"] += 1
            if pause_after_step["n"] == 1:
                engine.pause(workflow.id)
            return StepResult(summary="ok", verified_success=True, evidence=f"e{pause_after_step['n']}")

        engine.register_kind(WorkflowKindSpec(
            name="pausable",
            build_steps=lambda **kw: [WorkflowStep(f"step {i}", handler_key="h") for i in range(3)],
            handlers={"h": handler},
        ))
        wf = engine.create_workflow("pausable", user_request="test")
        result = await engine.run(wf.id)
        assert result.status == WorkflowStatus.PAUSED
        assert result.current_step == 1  # only the first step ran before the pause took effect

        # Resume — pause_requested is cleared and the loop continues from current_step.
        task = engine.resume(wf.id)
        assert task is True
        # Give the spawned asyncio.Task a turn to actually run.
        await asyncio.sleep(0.05)
        final = engine.get(wf.id)
        assert final.status == WorkflowStatus.COMPLETED
        assert final.current_step == 3


# ── Verification semantics (Feature 9) ───────────────────────────
class TestVerification:
    @pytest.mark.asyncio
    async def test_unverified_step_reports_attempted_not_success(self, engine):
        async def handler(workflow, step):
            return StepResult(summary="ran it, didn't check", verified_success=None)

        engine.register_kind(WorkflowKindSpec(
            name="unverified",
            build_steps=lambda **kw: [WorkflowStep("do a thing", handler_key="h")],
            handlers={"h": handler},
        ))
        wf = engine.create_workflow("unverified", user_request="test")
        result = await engine.run(wf.id)
        assert result.steps[0].outcome == StepOutcome.ATTEMPTED
        assert result.steps[0].outcome != StepOutcome.VERIFIED_SUCCESS

    @pytest.mark.asyncio
    async def test_to_report_never_claims_success_with_unresolved_errors(self, engine):
        async def handler(workflow, step):
            return StepResult(summary="broke", verified_success=False, error="it broke")

        engine.register_kind(WorkflowKindSpec(
            name="broken",
            build_steps=lambda **kw: [WorkflowStep("do a thing", handler_key="h")],
            handlers={"h": handler},
        ))
        wf = engine.create_workflow("broken", user_request="test", max_steps=5)
        result = await engine.run(wf.id)
        report = result.to_report()
        assert "VERIFIED SUCCESS" not in report


# ── Permission integration (Rule 2/3) ────────────────────────────
class TestPermissionIntegration:
    @pytest.mark.asyncio
    async def test_confirm_tool_step_pauses_workflow_for_permission(self, engine, fake_env):
        """A step naming a CONFIRM-level tool must not execute — the workflow
        must stop and wait, exactly like any other JARVIS tool call."""
        def build_steps(**kw):
            return [WorkflowStep("do something risky", tool_name="run_terminal_command",
                                  tool_args={"command": "rm -rf somedir"})]
        engine.register_kind(WorkflowKindSpec(name="risky", build_steps=build_steps))

        wf = engine.create_workflow("risky", user_request="test")
        result = await engine.run(wf.id, auto_approved=False)
        assert result.status == WorkflowStatus.WAITING_FOR_PERMISSION
        assert len(result.permissions_requested) == 1
        # The step itself must not have been marked DONE — it didn't run.
        assert result.steps[0].status != StepStatus.DONE

    @pytest.mark.asyncio
    async def test_blocked_tool_step_never_executes(self, engine, fake_env):
        # terminal_tools.classify_command has no REJECTED/BLOCKED path for a
        # single unchained command — "rm -rf /" tokenizes to program "rm",
        # which is in _DANGEROUS_COMMANDS -> CONFIRM-level DANGEROUS, not
        # BLOCKED. The one genuinely BLOCKED case in this codebase is a
        # shell-metacharacter injection attempt (chaining/redirection),
        # which classify_command REJECTS outright and tool_registry's
        # dynamic_classifier maps to PermissionLevel.BLOCKED.
        def build_steps(**kw):
            return [WorkflowStep("do something forbidden", tool_name="run_terminal_command",
                                  tool_args={"command": "git status; rm -rf /"})]
        engine.register_kind(WorkflowKindSpec(name="forbidden", build_steps=build_steps))

        wf = engine.create_workflow("forbidden", user_request="test")
        result = await engine.run(wf.id, auto_approved=True)  # even auto_approved can't unlock BLOCKED
        assert result.steps[0].status == StepStatus.FAILED
        assert result.status == WorkflowStatus.FAILED


# ── main.py chat integration (Feature 4 + Feature 11) ────────────
class TestChatIntegrationPhase6:
    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return main, TestClient(main.app)

    def test_project_analysis_intent_runs_review_workflow(self, monkeypatch, fake_env, jarvis_db_path, tmp_path):
        import json as _json

        project_dir = tmp_path / "reviewed_project"
        project_dir.mkdir()
        (project_dir / "requirements.txt").write_text("requests\n")
        (project_dir / "main.py").write_text("def run():\n    return 42\n")

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": _json.dumps({"intent": "PROJECT_ANALYSIS", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "should not be called — PROJECT_ANALYSIS is a workflow, not an LLM reply"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        # Route the active project the same way context_manager normally
        # would once a project is selected — set it directly rather than
        # depending on project-detection heuristics this test isn't about.
        main.context_manager.set_active_project(str(project_dir))
        try:
            with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
                with client_factory as client:
                    r = client.post("/chat", json={"message": "review my project", "session_id": "s1"})
                    assert r.status_code == 200
                    reply = r.json()["response"]
                    assert "PROJECT HEALTH REPORT" in reply
                    assert "Overall Status: GOOD" in reply
        finally:
            main.context_manager.set_active_project(None)

    def test_cancel_with_no_active_workflow_is_graceful(self, monkeypatch, fake_env, jarvis_db_path):
        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with client_factory as client:
            r = client.post("/chat", json={"message": "cancel", "session_id": "s-no-workflow"})
            assert r.status_code == 200
            assert "nothing running" in r.json()["response"].lower()

    def test_pause_controls_the_latest_session_workflow(self, monkeypatch, fake_env, jarvis_db_path):
        """
        Exercises the actual /chat 'pause' meta-command end-to-end
        against a real workflow tracked for a session — not just the
        engine's pause() method in isolation (already covered above).
        """
        from workflow_engine import workflow_engine as real_engine, WorkflowKindSpec, WorkflowStep, WorkflowStatus

        real_engine.register_kind(WorkflowKindSpec(
            name="parked_for_chat_test",
            build_steps=lambda **kw: [WorkflowStep("a step", handler_key="h")],
            handlers={},
        ))
        # Created but not started — status CREATED, which pause() treats
        # as controllable (not yet terminal), same as a workflow the
        # engine just hasn't gotten to its first step of yet.
        wf = real_engine.create_workflow("parked_for_chat_test", user_request="test", session_id="s-pause")
        assert wf.status != WorkflowStatus.CANCELLED

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with client_factory as client:
            r = client.post("/chat", json={"message": "pause", "session_id": "s-pause"})
            assert r.status_code == 200
            assert "pausing" in r.json()["response"].lower()
        assert real_engine.get(wf.id).pause_requested is True


# ── Project Health Monitor (Feature 6) ───────────────────────────
class TestProjectHealthMonitor:
    @pytest.fixture
    def health_project(self, tmp_path):
        project_dir = tmp_path / "health_project"
        project_dir.mkdir()
        (project_dir / "requirements.txt").write_text("requests\n")
        (project_dir / "main.py").write_text("def run():\n    return 42\n")
        return str(project_dir)

    def test_enable_disable_is_explicit_opt_in(self, health_project):
        from project_health import ProjectHealthMonitor
        monitor = ProjectHealthMonitor()
        assert monitor.is_enabled(health_project) is False
        monitor.enable(health_project)
        assert monitor.is_enabled(health_project) is True
        monitor.disable(health_project)
        assert monitor.is_enabled(health_project) is False

    @pytest.mark.asyncio
    async def test_report_shows_unknown_build_and_tests_with_no_history(self, health_project):
        from project_health import ProjectHealthMonitor, HealthSignal
        monitor = ProjectHealthMonitor()
        monitor.enable(health_project)
        report = await monitor.get_report(health_project)
        assert report.build == HealthSignal.UNKNOWN
        assert report.tests == HealthSignal.UNKNOWN

    @pytest.mark.asyncio
    async def test_repeated_build_failures_flagged_as_attention(self, health_project):
        from project_health import ProjectHealthMonitor, HealthSignal
        import background_tasks as bt

        tm = bt.TaskManager()
        for i in range(3):
            task = bt.BackgroundTask(
                id=f"t{i}", name="build", command="npm run build", project=health_project,
                start_time=float(i), status=bt.TaskStatus.FAILED,
            )
            tm._tasks[task.id] = task

        monitor = ProjectHealthMonitor()
        monitor.enable(health_project)
        import project_health
        orig_bt = project_health.__dict__.get("background_tasks")
        import sys
        real_bt_module = sys.modules["background_tasks"]
        sys.modules["background_tasks"] = _FakeModule(task_manager=tm)
        try:
            report = await monitor.get_report(health_project)
        finally:
            sys.modules["background_tasks"] = real_bt_module

        assert report.build == HealthSignal.WARNING
        assert any("build has failed 3 times" in a for a in report.attention)

    @pytest.mark.asyncio
    async def test_successful_latest_build_reports_good_even_after_earlier_failures(self, health_project):
        from project_health import ProjectHealthMonitor, HealthSignal
        import background_tasks as bt
        import sys

        tm = bt.TaskManager()
        statuses = [bt.TaskStatus.FAILED, bt.TaskStatus.FAILED, bt.TaskStatus.SUCCEEDED]
        for i, status in enumerate(statuses):
            task = bt.BackgroundTask(
                id=f"t{i}", name="build", command="npm run build", project=health_project,
                start_time=float(i), status=status,
            )
            tm._tasks[task.id] = task

        monitor = ProjectHealthMonitor()
        monitor.enable(health_project)
        real_bt_module = sys.modules["background_tasks"]
        sys.modules["background_tasks"] = _FakeModule(task_manager=tm)
        try:
            report = await monitor.get_report(health_project)
        finally:
            sys.modules["background_tasks"] = real_bt_module

        assert report.build == HealthSignal.GOOD

    @pytest.mark.asyncio
    async def test_uncommitted_changes_flag_git_warning(self, health_project):
        from project_health import ProjectHealthMonitor, HealthSignal
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=health_project, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=health_project, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=health_project, check=True)
        # An untracked file makes `git status --porcelain` non-empty without needing a commit.
        (open(f"{health_project}/scratch.txt", "w")).write("x")

        monitor = ProjectHealthMonitor()
        monitor.enable(health_project)
        report = await monitor.get_report(health_project)
        assert report.git == HealthSignal.WARNING
        assert any("uncommitted" in a for a in report.attention)

    @pytest.mark.asyncio
    async def test_missing_dependency_flags_dependencies_warning(self, health_project):
        from project_health import ProjectHealthMonitor, HealthSignal
        import tool_registry

        async def fake_run_tool(name, args, broadcast=None, auto_approved=False):
            return {"status": "ok", "result": {"stdout": ""}}  # nothing installed -> requests missing

        orig = tool_registry.tool_registry.run_tool
        tool_registry.tool_registry.run_tool = fake_run_tool
        try:
            monitor = ProjectHealthMonitor()
            monitor.enable(health_project)
            report = await monitor.get_report(health_project)
        finally:
            tool_registry.tool_registry.run_tool = orig

        assert report.dependencies == HealthSignal.WARNING
        assert any("requests" in a for a in report.attention)


class _FakeModule:
    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


# ── Development Environment Agent (Feature 5) ────────────────────
class TestDevEnvPrepWorkflow:
    @pytest.fixture
    def python_project_missing_deps(self, tmp_path):
        project_dir = tmp_path / "py_project"
        project_dir.mkdir()
        (project_dir / "requirements.txt").write_text("requests\nflask\n")
        (project_dir / "main.py").write_text("def run():\n    return 42\n")
        return str(project_dir)

    @pytest.fixture
    def python_project_no_manifest(self, tmp_path):
        project_dir = tmp_path / "py_project_bare"
        project_dir.mkdir()
        (project_dir / "main.py").write_text("def run():\n    return 42\n")
        return str(project_dir)

    def _fake_run_tool(self, installed_packages):
        """Fakes tool_registry.run_tool for the version/list-check calls
        this workflow makes — no real subprocess, no real pip/npm needed.
        Mirrors the real permission gate closely enough for this test:
        install commands need confirmation, read-only ones don't."""
        async def fake(name, args, broadcast=None, auto_approved=False):
            command = args.get("command", "")
            if "install" in command:
                return {"status": "pending_confirmation", "confirmation_id": "fake-confirm-id"}
            if "--version" in command:
                return {"status": "ok", "result": {"stdout": "Python 3.12.3\n"}}
            if "list" in command:
                stdout = "\n".join(installed_packages)
                return {"status": "ok", "result": {"stdout": stdout}}
            return {"status": "ok", "result": {"stdout": ""}}
        return fake

    @pytest.mark.asyncio
    async def test_missing_dependency_requires_permission(self, python_project_missing_deps, monkeypatch):
        import tool_registry
        from workflow_engine import workflow_engine as real_engine
        orig = tool_registry.tool_registry.run_tool
        tool_registry.tool_registry.run_tool = self._fake_run_tool(["requests==2.31.0"])  # flask missing
        try:
            wf = real_engine.create_workflow(
                "dev_env_prep", user_request="prepare this project", project_path=python_project_missing_deps,
            )
            result = await real_engine.run(wf.id)
        finally:
            tool_registry.tool_registry.run_tool = orig

        assert result.status.value == "WAITING_FOR_PERMISSION"
        plan_text = result.steps[-2].result
        assert "flask" in plan_text
        assert "Permission required: YES" in plan_text
        # The install step itself must not have run.
        assert result.steps[-1].status.value != "DONE"

    @pytest.mark.asyncio
    async def test_all_dependencies_present_needs_no_permission(self, python_project_missing_deps, monkeypatch):
        import tool_registry
        from workflow_engine import workflow_engine as real_engine, WorkflowStatus as WS
        orig = tool_registry.tool_registry.run_tool
        tool_registry.tool_registry.run_tool = self._fake_run_tool(["requests==2.31.0", "flask==3.0.0"])
        try:
            wf = real_engine.create_workflow(
                "dev_env_prep", user_request="prepare this project", project_path=python_project_missing_deps,
            )
            result = await real_engine.run(wf.id)
        finally:
            tool_registry.tool_registry.run_tool = orig

        assert result.status == WS.COMPLETED
        plan_text = result.steps[-2].result
        assert "Permission required: NO" in plan_text

    @pytest.mark.asyncio
    async def test_no_manifest_proposes_no_install(self, python_project_no_manifest, monkeypatch):
        import tool_registry
        from workflow_engine import workflow_engine as real_engine, WorkflowStatus as WS
        orig = tool_registry.tool_registry.run_tool
        tool_registry.tool_registry.run_tool = self._fake_run_tool([])
        try:
            wf = real_engine.create_workflow(
                "dev_env_prep", user_request="prepare this project", project_path=python_project_no_manifest,
            )
            result = await real_engine.run(wf.id)
        finally:
            tool_registry.tool_registry.run_tool = orig

        assert result.status == WS.COMPLETED
        assert "Permission required: NO" in result.steps[-2].result

    @pytest.fixture
    def node_project_missing_deps(self, tmp_path):
        project_dir = tmp_path / "node_project"
        project_dir.mkdir()
        (project_dir / "package.json").write_text(
            '{"name": "sample", "dependencies": {"express": "^4.18.0"}, '
            '"devDependencies": {"jest": "^29.0.0"}}'
        )
        return str(project_dir)

    @pytest.mark.asyncio
    async def test_node_project_missing_dependency_requires_permission(self, node_project_missing_deps):
        import tool_registry
        from workflow_engine import workflow_engine as real_engine
        orig = tool_registry.tool_registry.run_tool
        tool_registry.tool_registry.run_tool = self._fake_run_tool(["express@4.18.2"])  # jest missing
        try:
            wf = real_engine.create_workflow(
                "dev_env_prep", user_request="prepare this project", project_path=node_project_missing_deps,
            )
            result = await real_engine.run(wf.id)
        finally:
            tool_registry.tool_registry.run_tool = orig

        assert result.status.value == "WAITING_FOR_PERMISSION"
        plan_text = result.steps[-2].result
        assert "jest" in plan_text
        assert "Permission required: YES" in plan_text
        assert result.steps[-1].tool_args.get("command") == "npm install"

    def test_install_step_starts_unarmed(self):
        """Static check mirroring test_review_is_read_only — the install
        step is only ever tool_name-bound by generate_plan's handler at
        run time, never at build time, so a freshly built (not yet run)
        workflow can never carry a pre-armed install command."""
        from workflow_engine import workflow_engine as real_engine
        spec = real_engine._kinds["dev_env_prep"]
        steps = spec.build_steps()
        assert steps[-1].tool_name is None


class TestProjectReviewWorkflow:
    @pytest.fixture
    def sample_project(self, tmp_path):
        project_dir = tmp_path / "sample_project"
        project_dir.mkdir()
        (project_dir / "requirements.txt").write_text("requests\n")
        (project_dir / "main.py").write_text(textwrap.dedent("""
            import os
            import sys

            def run():
                return undefined_thing()
        """))
        return str(project_dir)

    @pytest.fixture
    def clean_project(self, tmp_path):
        project_dir = tmp_path / "clean_project"
        project_dir.mkdir()
        (project_dir / "requirements.txt").write_text("requests\n")
        (project_dir / "main.py").write_text("def run():\n    return 42\n")
        return str(project_dir)

    @pytest.mark.asyncio
    async def test_review_is_read_only(self, sample_project, monkeypatch):
        """No step in this workflow's kind spec can name a non-SAFE tool."""
        import workflows.project_review as pr
        from workflow_engine import workflow_engine as real_engine
        spec = real_engine._kinds["project_review"]
        steps = spec.build_steps(project_path=sample_project)
        assert all(s.tool_name is None for s in steps), "project_review must only use read-only handlers, no tool calls"

    @pytest.mark.asyncio
    async def test_review_flags_missing_entry_point(self, tmp_path):
        from workflow_engine import workflow_engine as real_engine
        empty_dir = tmp_path / "empty_project"
        empty_dir.mkdir()
        wf = real_engine.create_workflow("project_review", user_request="review it", project_path=str(empty_dir))
        result = await real_engine.run(wf.id)
        assert result.status == WorkflowStatus.COMPLETED
        report = result.steps[-1].result
        assert "CRITICAL" in report
        assert "No recognizable entry point" in report

    @pytest.mark.asyncio
    async def test_review_reports_static_analysis_issue_with_evidence(self, sample_project):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("project_review", user_request="review it", project_path=sample_project)
        result = await real_engine.run(wf.id)
        assert result.status == WorkflowStatus.COMPLETED
        # Evidence for the finding must actually exist somewhere in the recorded evidence.
        assert any("issue" in e.lower() for e in result.evidence)

    @pytest.mark.asyncio
    async def test_clean_project_reports_good_status(self, clean_project):
        from workflow_engine import workflow_engine as real_engine
        wf = real_engine.create_workflow("project_review", user_request="review it", project_path=clean_project)
        result = await real_engine.run(wf.id)
        report = result.steps[-1].result
        assert "Overall Status: GOOD" in report
