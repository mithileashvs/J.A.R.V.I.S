"""
JARVIS Phase 4 test suite.

Covers Phase 4 features as they land. Feature 1 (Screen-Aware Error
Analysis) is covered here; Features 2-4 (autonomous investigation
upgrades, background monitoring, Git assistant) get their own test
classes added in the same incremental order they're implemented, per
the Phase 4 brief's "test independently" instruction.

Run with:  pytest test_phase4.py -v
Requires: requirements-dev.txt (pytest, pytest-asyncio), same as
test_phase3.py.

Every test mocks out the platform-specific pieces (pygetwindow, mss,
pytesseract) rather than relying on them actually being installed or
a real window being focused — this suite must be deterministic on any
machine/CI, not just the Windows dev box these modules ultimately
target (same reasoning as test_phase3.py's TestContextManager fix).
"""

import textwrap
import time
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def fake_env(monkeypatch):
    """Same as test_phase3.py's fixture of the same name — config.py's
    validate() raises at import time without these three vars."""
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake_key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fake_secret")


@pytest.fixture
def sample_project(tmp_path):
    """Same fixture as test_phase3.py — no conftest.py in this project, so duplicated here."""
    project_dir = tmp_path / "sample_project"
    project_dir.mkdir()
    broken_file = project_dir / "app.py"
    broken_file.write_text(textwrap.dedent("""
        import os
        import sys

        def calculate_total(items):
            total = 0
            unused_var = "never used"
            for item in items:
                total += item
            return total

        def fetch_data():
            return undefined_helper()
    """))
    clean_file = project_dir / "clean.py"
    clean_file.write_text("def add(a, b):\n    return a + b\n")
    return {"dir": str(project_dir), "broken_file": str(broken_file), "clean_file": str(clean_file)}


@pytest.fixture(autouse=True)
def reset_global_context_manager():
    """Same reasoning as test_phase3.py's fixture of the same name — the
    context_manager singleton is process-wide state that must not leak between tests."""
    import context_manager as cm_mod
    original_path = cm_mod.context_manager.get_active_project_path()
    yield
    cm_mod.context_manager.set_active_project(original_path)


@pytest.fixture(autouse=True)
def reset_terminal_last_result():
    """Same reasoning as test_phase3.py's fixture of the same name."""
    import terminal_tools as tt_mod
    tt_mod.reset_last_result()
    yield
    tt_mod.reset_last_result()


# ── Screen-aware error analysis (Phase 4, Feature 1) ───────────────

class TestScreenAnalysisPrivacy:
    """
    Privacy/permission-boundary tests — these don't need real capture
    machinery at all, they verify the tool is registered CONFIRM (not
    SAFE) and that analyze_screen() never leaves anything captured
    behind, independent of whether mss/pytesseract are installed.
    """

    def test_analyze_screen_tool_requires_confirmation(self, fake_env):
        from tool_registry import tool_registry
        from permissions import PermissionLevel
        spec = tool_registry.get("analyze_screen")
        assert spec is not None
        assert spec.permission == PermissionLevel.CONFIRM

    def test_capture_active_window_tool_requires_confirmation(self, fake_env):
        from tool_registry import tool_registry
        from permissions import PermissionLevel
        spec = tool_registry.get("capture_active_window")
        assert spec.permission == PermissionLevel.CONFIRM

    def test_extract_screen_text_tool_requires_confirmation(self, fake_env):
        from tool_registry import tool_registry
        from permissions import PermissionLevel
        spec = tool_registry.get("extract_screen_text")
        assert spec.permission == PermissionLevel.CONFIRM

    def test_no_active_window_reported_honestly(self):
        """Failure scenario: no active window."""
        import screen_tools
        with patch("awareness.get_active_window", return_value={"available": False, "reason": "No active window detected."}):
            ctx = screen_tools.analyze_screen()
        assert ctx.available is False
        assert "No active window" in ctx.reason

    def test_capture_unavailable_reported_honestly_not_crashed(self):
        """Failure scenario: mss not installed / capture fails."""
        import screen_tools
        with patch("awareness.get_active_window", return_value={"available": True, "title": "app.py - Visual Studio Code"}), \
             patch.object(screen_tools, "_bounding_box_for_active_window", return_value=None), \
             patch.object(screen_tools, "_capture_region", return_value=False):
            ctx = screen_tools.analyze_screen()
        assert ctx.available is False
        assert ctx.application_type == "IDE"
        assert "not available" in ctx.reason.lower() or "failed" in ctx.reason.lower()

    def test_ocr_unavailable_reported_honestly_not_crashed(self):
        """Failure scenario: OCR/Tesseract not installed."""
        import screen_tools
        with patch("awareness.get_active_window", return_value={"available": True, "title": "Windows Terminal"}), \
             patch.object(screen_tools, "_bounding_box_for_active_window", return_value=None), \
             patch.object(screen_tools, "_capture_region", return_value=True), \
             patch.object(screen_tools, "_ocr_extract", return_value=None), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove"):
            ctx = screen_tools.analyze_screen()
        assert ctx.available is False
        assert "ocr" in ctx.reason.lower()

    def test_temp_screenshot_cleaned_up_by_default(self):
        """No screenshot persists on disk unless save_screenshot=True."""
        import screen_tools
        with patch("awareness.get_active_window", return_value={"available": True, "title": "Visual Studio Code"}), \
             patch.object(screen_tools, "_bounding_box_for_active_window", return_value=None), \
             patch.object(screen_tools, "_capture_region", return_value=True), \
             patch.object(screen_tools, "_ocr_extract", return_value="ModuleNotFoundError: No module named 'cv2'\nFile \"app.py\", line 12"), \
             patch("os.remove") as mock_remove, \
             patch("os.path.exists", return_value=True):
            ctx = screen_tools.analyze_screen(save_screenshot=False)
        assert ctx.available is True
        assert ctx.screenshot_path is None
        mock_remove.assert_called_once()

    def test_save_screenshot_keeps_file_when_requested(self):
        import screen_tools
        with patch("awareness.get_active_window", return_value={"available": True, "title": "Visual Studio Code"}), \
             patch.object(screen_tools, "_bounding_box_for_active_window", return_value=None), \
             patch.object(screen_tools, "_capture_region", return_value=True), \
             patch.object(screen_tools, "_ocr_extract", return_value="clean output, no errors"), \
             patch("os.makedirs"), \
             patch("os.replace") as mock_replace, \
             patch("os.path.exists", return_value=False):
            ctx = screen_tools.analyze_screen(save_screenshot=True)
        assert ctx.available is True
        assert ctx.screenshot_path is not None
        mock_replace.assert_called_once()


class TestScreenContentExtraction:
    def test_classifies_ide_from_window_title(self):
        import screen_tools
        assert screen_tools._classify_application("app.py - Visual Studio Code") == "IDE"

    def test_classifies_terminal_from_window_title(self):
        import screen_tools
        assert screen_tools._classify_application("Windows PowerShell") == "TERMINAL"

    def test_classifies_browser_from_window_title(self):
        import screen_tools
        assert screen_tools._classify_application("JARVIS docs - Google Chrome") == "BROWSER"

    def test_unknown_application_type_not_guessed(self):
        """Never dress up a guess as certainty — unmatched titles report UNKNOWN."""
        import screen_tools
        assert screen_tools._classify_application("Random Untitled App") == "UNKNOWN"

    def test_extracts_module_not_found_from_ocr_text(self):
        """Real end-to-end scenario from the Phase 4 brief's own example."""
        import screen_tools
        ocr_text = (
            "PS C:\\project> python app.py\n"
            "Traceback (most recent call last):\n"
            "  File \"app.py\", line 12, in <module>\n"
            "    import cv2\n"
            "ModuleNotFoundError: No module named 'cv2'\n"
        )
        errors, files, lines = screen_tools._extract_error_signal(ocr_text)
        assert any("ModuleNotFoundError" in e for e in errors)
        assert "app.py" in files
        assert 12 in lines

    def test_no_error_text_extracts_nothing(self):
        import screen_tools
        errors, files, lines = screen_tools._extract_error_signal("All tests passed. 42 passed in 1.2s")
        assert errors == []

    def test_extracted_text_truncated_when_oversized(self):
        import screen_tools
        with patch("awareness.get_active_window", return_value={"available": True, "title": "Visual Studio Code"}), \
             patch.object(screen_tools, "_bounding_box_for_active_window", return_value=None), \
             patch.object(screen_tools, "_capture_region", return_value=True), \
             patch.object(screen_tools, "_ocr_extract", return_value="x" * (screen_tools._MAX_EXTRACTED_CHARS + 5000)), \
             patch("os.remove"), patch("os.path.exists", return_value=True):
            ctx = screen_tools.analyze_screen()
        assert ctx.truncated is True
        assert len(ctx.extracted_text) < screen_tools._MAX_EXTRACTED_CHARS + 100


class TestScreenAwareDebugIntegration:
    """
    debug_mode.Investigation never captures the screen itself — it
    only uses a pre-confirmed ScreenContext handed in by the caller.
    """

    @pytest.mark.asyncio
    async def test_investigation_never_captures_screen_on_its_own(self, fake_env, monkeypatch, tmp_path):
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test_jarvis.db"))
        import memory
        memory.init_db()
        import project_memory as pm
        pm.init_project_memory_db()

        import screen_tools
        with patch.object(screen_tools, "analyze_screen") as mock_analyze:
            from debug_mode import Investigation
            investigation = Investigation()
            await investigation.run("why is my app crashing")
            mock_analyze.assert_not_called()

    @pytest.mark.asyncio
    async def test_investigation_uses_supplied_screen_context_as_fallback_evidence(self, fake_env, monkeypatch, tmp_path):
        """When no terminal error / static analysis evidence exists, a
        supplied ScreenContext becomes the (low-confidence) diagnosis."""
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test_jarvis.db"))
        import memory
        memory.init_db()
        import project_memory as pm
        pm.init_project_memory_db()
        import terminal_tools
        terminal_tools.reset_last_result()

        from screen_tools import ScreenContext
        screen_ctx = ScreenContext(
            available=True,
            application_type="TERMINAL",
            window_title="Windows Terminal",
            extracted_text="ModuleNotFoundError: No module named 'cv2'",
            detected_errors=["ModuleNotFoundError: No module named 'cv2'"],
            file_references=[],
            line_references=[],
        )

        from debug_mode import Confidence, Investigation
        investigation = Investigation()
        result = await investigation.run("what's wrong here", screen_context=screen_ctx)
        assert result.diagnosis is not None
        assert "cv2" in result.diagnosis.diagnosis
        assert result.diagnosis.confidence == Confidence.LOW
        assert any(s.name == "Check screen evidence" for s in result.steps)


# ── Advanced Autonomous Debugging (Phase 4, Feature 2) ──────────────

class TestHypothesisBasedDebugging:
    """
    Section 12/13: "do not assume the first error is the root cause" —
    multiple hypotheses get built and ranked, each with its own
    confidence/status, rather than the investigation committing
    silently to whichever evidence source happened to run first.
    """

    @pytest.mark.asyncio
    async def test_terminal_error_outranks_static_analysis(self, fake_env, monkeypatch, tmp_path, sample_project):
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test_jarvis.db"))
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from context_manager import context_manager
        pm.upsert_project(sample_project["dir"], name="Proj", technologies=["Python"])
        context_manager.set_active_project(sample_project["dir"])

        import terminal_tools
        await terminal_tools.run_command("python -c \"import totally_missing_module_xyz\"")

        from debug_mode import Investigation, HypothesisStatus
        inv = Investigation()
        result = await inv.run(f"why is {sample_project['broken_file']} failing")

        assert len(result.hypotheses) >= 2  # terminal_error + static_analysis, at least
        assert result.hypotheses[0].id == "terminal_error"
        assert result.hypotheses[0].status == HypothesisStatus.CONFIRMED
        assert "totally_missing_module_xyz" in result.diagnosis.root_cause

    @pytest.mark.asyncio
    async def test_hypotheses_have_distinct_statuses_and_evidence(self, fake_env, monkeypatch, tmp_path, sample_project):
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test_jarvis.db"))
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from context_manager import context_manager
        pm.upsert_project(sample_project["dir"], name="Proj")
        context_manager.set_active_project(sample_project["dir"])
        pm.save_fact(sample_project["dir"], "known_issue", "Missing __init__.py caused import errors before.")

        from debug_mode import Investigation
        inv = Investigation()
        result = await inv.run(f"why is {sample_project['broken_file']} failing")

        for h in result.hypotheses:
            assert h.evidence, f"hypothesis {h.id} has no evidence attached"
            assert h.description

    @pytest.mark.asyncio
    async def test_no_evidence_produces_no_hypotheses_not_a_fabricated_one(self, fake_env, monkeypatch, tmp_path):
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test_jarvis.db"))
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        import terminal_tools; terminal_tools.reset_last_result()

        from debug_mode import Investigation
        inv = Investigation()
        result = await inv.run("why is anything failing")
        assert result.hypotheses == []
        assert result.diagnosis.root_cause is None


class TestEnvironmentAndPortDiagnostics:
    """
    Section 11's own worked example: a ModuleNotFoundError should
    trigger an environment/dependency check automatically (conditional
    — never runs when there's no evidence suggesting it's relevant).
    """

    @pytest.mark.asyncio
    async def test_module_not_found_triggers_environment_check(self, fake_env, monkeypatch, tmp_path, sample_project):
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test_jarvis.db"))
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from context_manager import context_manager
        pm.upsert_project(sample_project["dir"], name="Proj", technologies=["Python"])
        context_manager.set_active_project(sample_project["dir"])

        import terminal_tools
        await terminal_tools.run_command("python -c \"import totally_missing_module_xyz\"")

        from debug_mode import Investigation
        inv = Investigation()
        result = await inv.run("why isn't this working")
        assert any(s.name == "Check environment/dependencies" for s in result.steps)

    @pytest.mark.asyncio
    async def test_no_terminal_error_skips_environment_check(self, fake_env, monkeypatch, tmp_path):
        """Conditional step — never runs without a reason."""
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test_jarvis.db"))
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        import terminal_tools; terminal_tools.reset_last_result()

        from debug_mode import Investigation
        inv = Investigation()
        result = await inv.run("why is anything failing")
        assert not any(s.name == "Check environment/dependencies" for s in result.steps)
        assert not any(s.name == "Check port usage" for s in result.steps)

    @pytest.mark.asyncio
    async def test_port_conflict_triggers_port_check(self, fake_env, monkeypatch, tmp_path):
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test_jarvis.db"))
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        import terminal_tools

        with patch.object(terminal_tools, "extract_errors") as mock_extract, \
             patch("awareness.check_port_usage", return_value={"available": True, "port": 3000, "in_use": True, "pid": 1234, "process_name": "node"}):
            from terminal_tools import ExtractedError, CommandResult
            mock_extract.return_value = ExtractedError(
                primary_error="Error: listen EADDRINUSE: address already in use, port 3000",
                error_type="port_conflict",
                likely_root_cause="Port 3000 is already in use by another process.",
                relevant_files=[],
            )
            # Directly populate the last-result cache with a failed
            # command rather than actually running one — run_command's
            # own gate for "was there an error" is based on the real
            # exit code/stderr, and this test only cares about
            # Investigation's downstream handling once that gate has
            # already passed.
            terminal_tools._last_result = CommandResult(
                command="npm run dev", exit_code=1, stdout="",
                stderr="Error: listen EADDRINUSE: address already in use, port 3000",
                execution_time=0.1, timed_out=False, truncated=False,
            )
            terminal_tools._last_result_at = time.monotonic()

            from debug_mode import Investigation
            inv = Investigation()
            result = await inv.run("why won't my server start")
        assert any(s.name == "Check port usage" for s in result.steps)


class TestRepeatedActionDetection:
    def test_mark_action_returns_false_on_repeat(self):
        from debug_mode import Investigation
        inv = Investigation()
        assert inv._mark_action("env:python") is True
        assert inv._mark_action("env:python") is False
        assert inv._mark_action("port:3000") is True


class TestInvestigationIncompleteReport:
    """Section: when JARVIS can't determine the cause, it must stop and
    report using the exact INVESTIGATION INCOMPLETE shape, never fabricate a root cause."""

    def test_incomplete_report_lists_evidence_and_causes(self):
        from debug_mode import InvestigationResult, InvestigationStep, Hypothesis, Confidence, HypothesisStatus
        result = InvestigationResult(
            steps=[InvestigationStep(1, "Gather context", "No active project set.")],
            hypotheses=[
                Hypothesis(id="a", description="Wrong Python environment", confidence=Confidence.MEDIUM, status=HypothesisStatus.SUPPORTED),
            ],
            stopped_reason="Step/time budget exhausted after step 1.",
        )
        text = result.to_incomplete_text()
        assert "INVESTIGATION INCOMPLETE" in text
        assert "No active project set." in text
        assert "Wrong Python environment" in text
        assert "Step/time budget exhausted" in text

    def test_incomplete_report_never_fabricates_when_no_hypotheses(self):
        from debug_mode import InvestigationResult
        result = InvestigationResult(steps=[])
        text = result.to_incomplete_text()
        assert "Not enough evidence" in text

    @pytest.mark.asyncio
    async def test_step_limit_produces_incomplete_report_not_a_fake_diagnosis(self, fake_env, monkeypatch, tmp_path):
        monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test_jarvis.db"))
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()

        from debug_mode import Investigation
        inv = Investigation(max_steps=1)
        result = await inv.run("why is anything failing")
        assert result.diagnosis.root_cause is None
        assert "budget exhausted" in result.stopped_reason
        text = result.to_incomplete_text()
        assert "INVESTIGATION INCOMPLETE" in text


# ── Background Build/Error Monitoring (Phase 4, Feature 3) ─────────

class TestBackgroundTaskLifecycle:
    @pytest.mark.asyncio
    async def test_start_task_succeeded_end_to_end(self, fake_env):
        from background_tasks import TaskManager, TaskStatus
        tm = TaskManager()
        task = await tm.start_task("say hi", "python3 -c \"print('hello world')\"")
        assert task.status == TaskStatus.RUNNING
        # Give the monitor coroutine a moment to drain + exit.
        import asyncio
        for _ in range(50):
            if tm.get_task(task.id).status != TaskStatus.RUNNING:
                break
            await asyncio.sleep(0.05)
        finished = tm.get_task(task.id)
        assert finished.status == TaskStatus.SUCCEEDED
        assert "hello world" in finished.stdout_summary
        assert finished.exit_code == 0

    @pytest.mark.asyncio
    async def test_failed_task_detected_and_error_extracted(self, fake_env):
        from background_tasks import TaskManager, TaskStatus
        import asyncio
        tm = TaskManager()
        task = await tm.start_task("broken import", "python3 -c \"import totally_missing_module_xyz\"")
        for _ in range(50):
            if tm.get_task(task.id).status != TaskStatus.RUNNING:
                break
            await asyncio.sleep(0.05)
        finished = tm.get_task(task.id)
        assert finished.status == TaskStatus.FAILED
        assert finished.error_detected is not None
        assert "totally_missing_module_xyz" in finished.error_detected

    @pytest.mark.asyncio
    async def test_command_not_found_reported_honestly(self, fake_env):
        from background_tasks import TaskManager, TaskStatus
        tm = TaskManager()
        task = await tm.start_task("nonsense", "this_command_does_not_exist_xyz")
        assert task.status == TaskStatus.FAILED
        assert "not found" in task.error_detected.lower()

    @pytest.mark.asyncio
    async def test_stop_task_cancels_running_process(self, fake_env):
        from background_tasks import TaskManager, TaskStatus
        import asyncio
        tm = TaskManager()
        task = await tm.start_task("sleeper", "python3 -c \"import time; time.sleep(30)\"")
        assert task.status == TaskStatus.RUNNING
        stopped = await tm.stop_task(task.id)
        assert stopped.status == TaskStatus.CANCELLED
        await asyncio.sleep(0.2)
        # Status must stay CANCELLED, not get overwritten once the killed process's exit is observed.
        assert tm.get_task(task.id).status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_stopping_already_finished_task_reports_honestly(self, fake_env):
        from background_tasks import TaskManager, TaskStatus
        import asyncio
        tm = TaskManager()
        task = await tm.start_task("quick", "python3 -c \"print('done')\"")
        for _ in range(50):
            if tm.get_task(task.id).status != TaskStatus.RUNNING:
                break
            await asyncio.sleep(0.05)
        result = await tm.stop_task(task.id)
        assert result.status == TaskStatus.SUCCEEDED  # not silently re-marked CANCELLED

    @pytest.mark.asyncio
    async def test_max_concurrent_tasks_enforced(self, fake_env):
        from background_tasks import TaskManager, _MAX_CONCURRENT_TASKS
        tm = TaskManager()
        for i in range(_MAX_CONCURRENT_TASKS):
            await tm.start_task(f"sleeper-{i}", "python3 -c \"import time; time.sleep(30)\"")
        with pytest.raises(RuntimeError):
            await tm.start_task("one_too_many", "python3 -c \"import time; time.sleep(30)\"")
        for t in tm.list_tasks():
            await tm.stop_task(t.id)

    def test_unknown_task_id_status_reported_honestly(self):
        from background_tasks import TaskManager
        tm = TaskManager()
        assert tm.get_task("does-not-exist") is None


class TestBackgroundTaskNotifications:
    @pytest.mark.asyncio
    async def test_failure_notifies_once(self, fake_env):
        from background_tasks import TaskManager, TaskStatus
        import asyncio
        tm = TaskManager()
        broadcasts = []
        async def fake_broadcast(data):
            broadcasts.append(data)

        task = await tm.start_task("broken", "python3 -c \"import totally_missing_module_xyz\"", broadcast=fake_broadcast)
        for _ in range(50):
            if tm.get_task(task.id).status != TaskStatus.RUNNING:
                break
            await asyncio.sleep(0.05)

        notifications = [b for b in broadcasts if b["type"] == "background_task_notification"]
        assert len(notifications) == 1
        assert notifications[0]["severity"] == "ERROR"

    @pytest.mark.asyncio
    async def test_duplicate_failure_suppressed(self, fake_env):
        """Section: NOTIFICATION RULES — same error repeated must not re-notify."""
        from background_tasks import TaskManager
        tm = TaskManager()
        broadcasts = []
        async def fake_broadcast(data):
            broadcasts.append(data)

        from background_tasks import BackgroundTask, TaskStatus
        task = BackgroundTask(id="t1", name="build", command="npm run build", project=None, start_time=0.0)
        task.status = TaskStatus.FAILED
        task.error_detected = "Module not found: 'cv2'"
        tm._tasks["t1"] = task
        tm._last_notified_fingerprint["t1"] = None

        await tm._maybe_notify(task, fake_broadcast)
        await tm._maybe_notify(task, fake_broadcast)  # same fingerprint again
        assert len(broadcasts) == 1

    @pytest.mark.asyncio
    async def test_new_error_after_old_one_still_notifies(self, fake_env):
        from background_tasks import TaskManager, BackgroundTask, TaskStatus
        tm = TaskManager()
        broadcasts = []
        async def fake_broadcast(data):
            broadcasts.append(data)

        task = BackgroundTask(id="t1", name="build", command="npm run build", project=None, start_time=0.0)
        task.status = TaskStatus.FAILED
        task.error_detected = "Module not found: 'cv2'"
        tm._tasks["t1"] = task
        tm._last_notified_fingerprint["t1"] = None
        await tm._maybe_notify(task, fake_broadcast)

        task.error_detected = "SyntaxError: unexpected token"
        await tm._maybe_notify(task, fake_broadcast)

        assert len(broadcasts) == 2

    @pytest.mark.asyncio
    async def test_success_notifies_info_severity(self, fake_env):
        from background_tasks import TaskManager, TaskStatus
        import asyncio
        tm = TaskManager()
        broadcasts = []
        async def fake_broadcast(data):
            broadcasts.append(data)
        task = await tm.start_task("ok", "python3 -c \"print('fine')\"", broadcast=fake_broadcast)
        for _ in range(50):
            if tm.get_task(task.id).status != TaskStatus.RUNNING:
                break
            await asyncio.sleep(0.05)
        notifications = [b for b in broadcasts if b["type"] == "background_task_notification"]
        assert len(notifications) == 1
        assert notifications[0]["severity"] == "INFO"


class TestBackgroundTaskToolRegistration:
    def test_start_background_task_uses_terminal_classifier(self, fake_env):
        """Dynamic classification reuses terminal_tools.classify_command — not a second scheme."""
        from tool_registry import tool_registry
        from permissions import PermissionLevel
        spec = tool_registry.get("start_background_task")
        assert spec is not None
        assert spec.dynamic_classifier is not None
        level, reason = spec.dynamic_classifier({"command": "python app.py"})
        assert level == PermissionLevel.CONFIRM

    def test_start_background_task_blocks_injection_attempts(self, fake_env):
        from tool_registry import tool_registry
        from permissions import PermissionLevel
        spec = tool_registry.get("start_background_task")
        level, reason = spec.dynamic_classifier({"command": "npm run dev && rm -rf /"})
        assert level == PermissionLevel.BLOCKED

    def test_stop_background_task_requires_confirmation(self, fake_env):
        from tool_registry import tool_registry
        from permissions import PermissionLevel
        spec = tool_registry.get("stop_background_task")
        assert spec.permission == PermissionLevel.CONFIRM

    def test_get_background_task_status_is_safe(self, fake_env):
        from tool_registry import tool_registry
        from permissions import PermissionLevel
        spec = tool_registry.get("get_background_task_status")
        assert spec.permission == PermissionLevel.SAFE


# ── Git Assistant (Phase 4, Feature 4) ──────────────────────────────

@pytest.fixture
def git_repo(tmp_path):
    """A real git repo — matches this suite's own convention of using
    real subprocess/tool behavior over mocks wherever practical (see
    test_phase3.py's TestCodeAnalysis using real pyflakes)."""
    import subprocess
    repo_dir = tmp_path / "gitrepo"
    repo_dir.mkdir()
    run = lambda *args: subprocess.run(["git", *args], cwd=str(repo_dir), check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "test@test.com")
    run("config", "user.name", "Test")
    (repo_dir / "app.py").write_text("print('hi')\n")
    (repo_dir / "requirements.txt").write_text("flask==2.0.0\n")
    run("add", ".")
    run("commit", "-q", "-m", "init")
    return str(repo_dir)


class TestGitReadOnlyOperations:
    @pytest.mark.asyncio
    async def test_git_status_clean_repo(self, fake_env, git_repo):
        import git_tools
        result = await git_tools.git_status(git_repo)
        assert result.available is True
        assert result.entries == []
        assert "clean" in result.to_text().lower()

    @pytest.mark.asyncio
    async def test_git_status_detects_modified_and_untracked(self, fake_env, git_repo):
        import git_tools
        with open(f"{git_repo}/app.py", "a") as f:
            f.write("print('more')\n")
        with open(f"{git_repo}/new.txt", "w") as f:
            f.write("new file\n")
        result = await git_tools.git_status(git_repo)
        statuses = {e.path: e.status for e in result.entries}
        assert statuses["app.py"] == "modified"
        assert statuses["new.txt"] == "untracked"

    @pytest.mark.asyncio
    async def test_git_status_not_a_repo_reported_honestly(self, fake_env, tmp_path):
        import git_tools
        plain_dir = tmp_path / "not_a_repo"
        plain_dir.mkdir()
        result = await git_tools.git_status(str(plain_dir))
        assert result.available is False
        assert "not a git repository" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_git_diff_shows_real_changes(self, fake_env, git_repo):
        import git_tools
        with open(f"{git_repo}/app.py", "a") as f:
            f.write("print('more')\n")
        result = await git_tools.git_diff(git_repo)
        assert result.available is True
        assert "more" in result.diff_text
        assert "app.py" in result.stat_summary

    @pytest.mark.asyncio
    async def test_git_log_returns_real_commits(self, fake_env, git_repo):
        import git_tools
        result = await git_tools.git_log(git_repo)
        assert result["available"] is True
        assert len(result["commits"]) == 1
        assert result["commits"][0]["message"] == "init"

    @pytest.mark.asyncio
    async def test_git_branch_shows_current(self, fake_env, git_repo):
        import git_tools
        result = await git_tools.git_branch(git_repo)
        assert result["available"] is True
        assert result["current"] in ("master", "main")

    @pytest.mark.asyncio
    async def test_no_project_path_reported_honestly(self, fake_env, monkeypatch):
        """Failure scenario: no cwd given and no active project set."""
        import git_tools
        import context_manager as cm_mod
        monkeypatch.setattr(cm_mod.context_manager, "get_active_project_path", lambda: None)
        result = await git_tools.git_status(None)
        assert result.available is False
        assert "no active project" in result.reason.lower() or "no project path" in result.reason.lower()


class TestGitChangeSummary:
    @pytest.mark.asyncio
    async def test_summary_groups_files_by_category(self, fake_env, git_repo):
        import git_tools
        with open(f"{git_repo}/app.py", "a") as f:
            f.write("print('more')\n")
        (open(f"{git_repo}/test_app.py", "w")).write("def test_x(): pass\n")
        result = await git_tools.generate_change_summary(git_repo)
        assert result["available"] is True
        assert "app.py" in result["groups"]["code"]
        assert "test_app.py" in result["groups"]["tests"]

    @pytest.mark.asyncio
    async def test_clean_tree_reports_no_changes(self, fake_env, git_repo):
        import git_tools
        result = await git_tools.generate_change_summary(git_repo)
        assert result["available"] is True
        assert result["file_count"] == 0
        assert "no changes" in result["summary_text"].lower()

    @pytest.mark.asyncio
    async def test_unpinned_dependency_flagged_as_concern(self, fake_env, git_repo):
        import git_tools
        with open(f"{git_repo}/requirements.txt", "a") as f:
            f.write("numpy\n")  # no pinned version
        result = await git_tools.generate_change_summary(git_repo)
        assert any("pinned version" in c for c in result["concerns"])

    @pytest.mark.asyncio
    async def test_never_exposes_raw_diff_in_summary(self, fake_env, git_repo):
        """Section: 'Do not expose raw massive diffs unnecessarily.'"""
        import git_tools
        with open(f"{git_repo}/app.py", "a") as f:
            f.write("this_is_a_unique_diff_line_marker\n")
        result = await git_tools.generate_change_summary(git_repo)
        assert "this_is_a_unique_diff_line_marker" not in result["summary_text"]


class TestGitCommitMessageGeneration:
    @pytest.mark.asyncio
    async def test_generates_conventional_commit_style_message(self, fake_env, git_repo):
        import git_tools
        with open(f"{git_repo}/app.py", "a") as f:
            f.write("print('more')\n")
        result = await git_tools.generate_commit_message(git_repo)
        assert result["available"] is True
        assert result["message"].startswith(("feat", "fix", "chore", "docs", "test"))
        assert "app.py" in result["message"]

    @pytest.mark.asyncio
    async def test_never_commits_automatically(self, fake_env, git_repo):
        """The function must only ever return text — verify the working tree is untouched afterward."""
        import git_tools, subprocess
        with open(f"{git_repo}/app.py", "a") as f:
            f.write("print('more')\n")
        await git_tools.generate_commit_message(git_repo)
        status_after = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True).stdout
        assert "app.py" in status_after  # still uncommitted/modified — nothing was committed

    @pytest.mark.asyncio
    async def test_no_changes_reports_honestly(self, fake_env, git_repo):
        import git_tools
        result = await git_tools.generate_commit_message(git_repo)
        assert result["available"] is True
        assert result["message"] is None

    @pytest.mark.asyncio
    async def test_prefers_staged_changes_when_present(self, fake_env, git_repo):
        import git_tools, subprocess
        with open(f"{git_repo}/app.py", "a") as f:
            f.write("print('staged')\n")
        with open(f"{git_repo}/unstaged.txt", "w") as f:
            f.write("unstaged\n")
        subprocess.run(["git", "add", "app.py"], cwd=git_repo, check=True)
        result = await git_tools.generate_commit_message(git_repo)
        assert result["source"] == "staged"
        assert "app.py" in result["message"]
        assert "unstaged.txt" not in result["message"]


class TestMergeConflictExplanation:
    @pytest.mark.asyncio
    async def test_parses_conflict_markers_and_explains_both_sides(self, fake_env, tmp_path):
        import git_tools
        f = tmp_path / "conflict.py"
        f.write_text(
            "def hello():\n"
            "<<<<<<< HEAD\n"
            "    print('ours')\n"
            "=======\n"
            "    print('theirs')\n"
            ">>>>>>> feature-branch\n"
        )
        result = await git_tools.analyze_merge_conflict(str(f))
        assert result.available is True
        assert len(result.blocks) == 1
        assert "ours" in result.blocks[0].ours
        assert "theirs" in result.blocks[0].theirs
        assert result.blocks[0].theirs_label == "feature-branch"

    @pytest.mark.asyncio
    async def test_no_markers_reported_honestly(self, fake_env, tmp_path):
        import git_tools
        f = tmp_path / "clean.py"
        f.write_text("def hello():\n    print('fine')\n")
        result = await git_tools.analyze_merge_conflict(str(f))
        assert result.available is True
        assert result.blocks == []
        assert "no conflict markers" in result.to_text().lower()

    @pytest.mark.asyncio
    async def test_missing_file_raises_honest_reason(self, fake_env, tmp_path):
        import git_tools
        result = await git_tools.analyze_merge_conflict(str(tmp_path / "does_not_exist.py"))
        assert result.available is False
        assert "not found" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_never_resolves_or_modifies_the_file(self, fake_env, tmp_path):
        import git_tools
        f = tmp_path / "conflict.py"
        original = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
        f.write_text(original)
        await git_tools.analyze_merge_conflict(str(f))
        assert f.read_text() == original


class TestGitRequestRouting:
    """Section: user examples map to the right action without a second LLM classification pass."""

    def test_what_changed_routes_to_summary(self):
        import git_tools
        assert git_tools.route_git_request("Jarvis, what changed?") == "summary"

    def test_explain_these_changes_routes_to_summary(self):
        import git_tools
        assert git_tools.route_git_request("Explain these changes.") == "summary"

    def test_generate_commit_message_routes_correctly(self):
        import git_tools
        assert git_tools.route_git_request("Generate a commit message.") == "commit_message"

    def test_merge_conflict_routes_correctly(self):
        import git_tools
        assert git_tools.route_git_request("Explain this merge conflict.") == "merge_conflict"

    def test_unmatched_message_defaults_to_summary(self):
        import git_tools
        assert git_tools.route_git_request("blah blah nothing specific") == "summary"


class TestGitToolRegistration:
    @pytest.mark.parametrize("tool_name", [
        "git_status", "git_diff", "git_log", "git_branch",
        "generate_commit_summary", "generate_commit_message", "analyze_merge_conflict",
    ])
    def test_git_tools_are_all_safe(self, fake_env, tool_name):
        """Section GIT PERMISSIONS: status/diff/log/branch are SAFE;
        nothing this module exposes ever stages/commits/pushes/merges."""
        from tool_registry import tool_registry
        from permissions import PermissionLevel
        spec = tool_registry.get(tool_name)
        assert spec is not None, f"{tool_name} not registered"
        assert spec.permission == PermissionLevel.SAFE



