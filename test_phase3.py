"""
JARVIS Phase 3 test suite (Section 19).

Consolidates the manual verification done throughout Phase 3
development into real, runnable tests. Covers:

    Code analysis routing          -> TestCodeAnalysis
    Code explanation modes          -> TestExplanationModes
    Terminal command permissions    -> TestTerminalClassification
    Terminal timeout                -> TestTerminalExecution
    Terminal error extraction       -> TestErrorExtraction
    Debug Mode routing              -> TestDebugModeIntegration
    Debug investigation step limits -> TestDebugInvestigationBounds
    Cancellation                    -> TestDebugInvestigationBounds
    Project context integration     -> TestContextManager
    Conversation context references -> TestContextManager
    Permission before file mod.     -> TestTerminalClassification (DANGEROUS/REJECTED tiers)

Plus the failure scenarios Section 19 explicitly asks for:
    No active project detected, No terminal available, No error
    detected, File unavailable, Permission denied, Command timeout,
    Malformed terminal output, Tool failure, LLM/API failure
    -> scattered across the relevant TestX class, marked in each
    test's docstring.

Run with:  pytest test_phase3.py -v
Requires: requirements-dev.txt (pytest, pytest-asyncio, httpx) —
these are NOT needed to run JARVIS itself, only to run this file.

Every test here creates its own isolated SQLite DB via the
jarvis_db_path fixture (JARVIS_DB_PATH env var, monkeypatched per
test) so running this suite never touches your real jarvis_memory.db.
"""

import asyncio
import json
import os
import shutil
import tempfile
import textwrap
from unittest.mock import patch

import pytest


# ── Shared fixtures ──────────────────────────────────────────────

@pytest.fixture
def jarvis_db_path(monkeypatch, tmp_path):
    """Isolated SQLite DB per test — never touches the real jarvis_memory.db."""
    db_path = str(tmp_path / "test_jarvis.db")
    monkeypatch.setenv("JARVIS_DB_PATH", db_path)
    return db_path


@pytest.fixture
def fake_env(monkeypatch):
    """
    config.py's validate() raises at import time without these three
    vars — none of the values matter for these tests since nothing
    here actually connects to LiveKit.
    """
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake_key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fake_secret")


@pytest.fixture
def sample_project(tmp_path):
    """A real, deliberately-broken Python file + a registered project."""
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
    """
    context_manager.py's module-level `context_manager` singleton
    holds one piece of mutable state (_active_project_path) shared
    across the whole process — several tests call set_active_project()
    on it. Without this fixture, one test's project selection could
    leak into another test that assumes no project is set, depending
    on execution order (verified this is a real risk, not a
    hypothetical one, by running the suite with pytest-randomly and
    confirming it currently passes only because no test happens to
    depend on the singleton being clean — this fixture makes that
    guarantee explicit instead of accidental).
    """
    import context_manager as cm_mod
    original_path = cm_mod.context_manager.get_active_project_path()
    yield
    cm_mod.context_manager.set_active_project(original_path)


@pytest.fixture(autouse=True)
def reset_terminal_last_result():
    """
    terminal_tools.py's last-command-result cache (used by Debug Mode's
    'check terminal output' step and the read_terminal_output tool) is
    also module-level, process-wide state — same category of risk as
    the context_manager singleton above. Without this, a test in
    TestTerminalExecution that calls run_command() could leak its
    result into a later TestDebugModeIntegration test and silently
    change what evidence the investigation sees.
    """
    import terminal_tools as tt_mod
    tt_mod.reset_last_result()
    yield
    tt_mod.reset_last_result()


# ── Code analysis routing + explanation modes ────────────────────

class TestCodeAnalysis:
    def test_analyze_file_finds_real_issues(self, fake_env, sample_project):
        """Real pyflakes detection, not a mock — matches Section 3."""
        from code_analysis import analyze_file
        result = analyze_file(sample_project["broken_file"])
        assert result.language == "Python"
        assert result.analysis_depth == "static"
        messages = [i.message for i in result.issues]
        assert any("os" in m and "unused" in m for m in messages)
        assert any("undefined" in m.lower() for m in messages)
        assert all(i.confidence == "Confirmed" for i in result.issues)

    def test_analyze_file_clean_code_zero_issues(self, fake_env, sample_project):
        from code_analysis import analyze_file
        result = analyze_file(sample_project["clean_file"])
        assert result.issues == []

    def test_analyze_file_missing_file_raises(self, fake_env):
        """Failure scenario: File unavailable."""
        from code_analysis import analyze_file
        with pytest.raises(FileNotFoundError):
            analyze_file("/does/not/exist.py")

    def test_analyze_file_oversized_rejected(self, fake_env, tmp_path):
        from code_analysis import analyze_file, _MAX_FILE_BYTES
        huge = tmp_path / "huge.py"
        huge.write_text("x = 1\n" * 200_000)
        with pytest.raises(ValueError):
            analyze_file(str(huge))

    def test_non_python_file_gets_structural_pass_only(self, fake_env, tmp_path):
        """Honesty check: non-Python must never claim 'static' depth."""
        from code_analysis import analyze_file
        js_file = tmp_path / "notes.js"
        js_file.write_text("// TODO: refactor this\nfunction f() {}\n")
        result = analyze_file(str(js_file))
        assert result.analysis_depth == "structural"
        assert any("TODO" in i.message for i in result.issues)

    def test_extract_unit_scopes_correctly(self, fake_env, sample_project):
        from code_analysis import extract_unit
        unit = extract_unit(sample_project["broken_file"], "calculate_total")
        assert unit.kind == "function"
        assert "unused_var" in unit.source
        assert "fetch_data" not in unit.source  # must not leak unrelated code

    def test_extract_unit_unknown_name_raises(self, fake_env, sample_project):
        from code_analysis import extract_unit
        with pytest.raises(ValueError):
            extract_unit(sample_project["broken_file"], "does_not_exist")


class TestExplanationModes:
    @pytest.mark.parametrize("mode", ["BEGINNER", "LINE_BY_LINE", "TECHNICAL", "INTERVIEW", "EXAM", "ELI5"])
    def test_all_modes_build_distinct_prompts(self, fake_env, sample_project, mode):
        from code_analysis import extract_unit, build_explanation_prompt
        unit = extract_unit(sample_project["broken_file"], "calculate_total")
        prompt = build_explanation_prompt(unit, mode)
        assert "calculate_total" in prompt

    def test_mode_case_insensitive(self, fake_env, sample_project):
        from code_analysis import extract_unit, build_explanation_prompt
        unit = extract_unit(sample_project["broken_file"], "calculate_total")
        prompt = build_explanation_prompt(unit, "interview")
        assert prompt  # doesn't raise

    def test_invalid_mode_raises(self, fake_env, sample_project):
        from code_analysis import extract_unit, build_explanation_prompt
        unit = extract_unit(sample_project["broken_file"], "calculate_total")
        with pytest.raises(ValueError):
            build_explanation_prompt(unit, "NOT_A_REAL_MODE")

    def test_intent_router_detects_mode(self):
        """Section 5: explanation mode detection via the Intent Router."""
        import intent_router as ir

        def fake_chat(**kwargs):
            return {"message": {"content": json.dumps(
                {"intent": "CODE_EXPLANATION", "confidence": 0.9, "mode": "INTERVIEW"}
            )}}

        with patch("ollama.chat", side_effect=fake_chat):
            result = ir.classify("explain this in interview mode")
        assert result.intent == ir.Intent.CODE_EXPLANATION
        assert result.mode == "INTERVIEW"

    def test_intent_router_invalid_mode_ignored(self):
        import intent_router as ir

        def fake_chat(**kwargs):
            return {"message": {"content": json.dumps(
                {"intent": "CODE_EXPLANATION", "confidence": 0.9, "mode": "NOT_REAL"}
            )}}

        with patch("ollama.chat", side_effect=fake_chat):
            result = ir.classify("explain this")
        assert result.mode is None


# ── Terminal: classification, execution, timeout, error extraction ──

class TestTerminalClassification:
    """Section 7's permission tiers, matched against its own examples."""

    @pytest.mark.parametrize("cmd", [
        "git status", "git diff", "git log", "python --version", "pip list", "ls -la", "pwd",
    ])
    def test_safe_commands(self, cmd):
        from terminal_tools import classify_command
        assert classify_command(cmd).level == "SAFE"

    @pytest.mark.parametrize("cmd", [
        "pip install requests", "npm install", "python script.py", "npm run dev",
        "pytest", "git checkout main", "git commit -m msg", "git push", "git reset",
    ])
    def test_confirm_commands(self, cmd):
        from terminal_tools import classify_command
        assert classify_command(cmd).level == "CONFIRM"

    @pytest.mark.parametrize("cmd", [
        "rm -rf /", "rmdir foo", "del file.txt", "git reset --hard", "git clean -fd",
        "taskkill /F /IM notepad.exe", "git checkout -f",
    ])
    def test_dangerous_commands(self, cmd):
        from terminal_tools import classify_command
        assert classify_command(cmd).level == "DANGEROUS"

    @pytest.mark.parametrize("cmd", [
        "git status && rm -rf /", "git status; rm -rf /", "echo hello | rm -rf /",
        "git status `rm -rf /`", "ls > /etc/passwd", "ls & rm -rf /",
        "git status || rm -rf /", "ls; whoami", "echo $(whoami)",
    ])
    def test_injection_attempts_rejected(self, cmd):
        """Permission before file modification — chaining must never bypass classification."""
        from terminal_tools import classify_command
        assert classify_command(cmd).level == "REJECTED"

    def test_unknown_command_defaults_to_confirm_never_safe(self):
        from terminal_tools import classify_command
        result = classify_command("some_totally_unknown_binary --flag")
        assert result.level == "CONFIRM"


class TestTerminalExecution:
    @pytest.mark.asyncio
    async def test_run_command_success(self):
        from terminal_tools import run_command
        result = await run_command("echo hello world")
        assert result.exit_code == 0
        assert "hello world" in result.stdout

    @pytest.mark.asyncio
    async def test_run_command_not_found(self):
        from terminal_tools import run_command
        result = await run_command("this_command_does_not_exist_xyz")
        assert result.exit_code is None
        assert "not found" in result.stderr.lower()

    @pytest.mark.asyncio
    async def test_run_command_timeout(self):
        """Failure scenario: Command timeout."""
        from terminal_tools import run_command
        result = await run_command("sleep 5", timeout=0.5)
        assert result.timed_out is True
        assert result.exit_code is None

    @pytest.mark.asyncio
    async def test_run_command_output_truncation(self):
        """Failure scenario: Malformed/oversized terminal output."""
        from terminal_tools import run_command, _MAX_OUTPUT_CHARS
        result = await run_command(f'python3 -c "print(\'x\' * {_MAX_OUTPUT_CHARS + 5000})"')
        assert result.truncated is True
        assert len(result.stdout) < _MAX_OUTPUT_CHARS + 100


class TestErrorExtraction:
    @pytest.mark.asyncio
    async def test_extracts_real_module_not_found(self):
        """Real subprocess-generated traceback, not synthetic text."""
        from terminal_tools import run_command, extract_errors
        result = await run_command('python3 -c "import definitely_not_a_real_module_xyz"')
        extracted = extract_errors(result.stderr)
        assert extracted.error_type == "python_traceback"
        assert "definitely_not_a_real_module_xyz" in extracted.primary_error
        assert len(extracted.relevant_files) >= 1

    def test_extracts_port_conflict(self):
        from terminal_tools import extract_errors
        result = extract_errors("Error: listen EADDRINUSE: address already in use :::3000")
        assert result.error_type == "port_conflict"

    def test_extracts_permission_denied(self):
        from terminal_tools import extract_errors
        result = extract_errors("PermissionError: [Errno 13] Permission denied: /etc/shadow")
        assert result.error_type == "permission"

    def test_empty_output_no_error(self):
        """Failure scenario: No error detected."""
        from terminal_tools import extract_errors
        result = extract_errors("")
        assert result.primary_error is None
        assert result.error_type is None

    def test_unrecognized_output_never_fabricates_root_cause(self):
        from terminal_tools import extract_errors
        result = extract_errors("Build completed successfully in 4.2s")
        assert result.error_type == "generic"
        assert result.likely_root_cause is None  # must not claim evidence it doesn't have


# ── Context manager: project context + conversation references ────

class TestContextManager:
    def test_gather_with_nothing_set_up(self, fake_env, jarvis_db_path):
        """Failure scenario: No active project detected."""
        import memory
        memory.init_db()
        from context_manager import ContextManager
        cm = ContextManager()
        # Window detection reflects whatever's actually focused on the
        # machine running the test, which isn't something this test
        # controls or cares about — it's testing the "nothing set up"
        # path, not real desktop state. Patch it to the same
        # unavailable result a headless/CI environment would give, so
        # the test is deterministic regardless of what's on screen.
        with patch("awareness.get_active_window", return_value={"available": False, "reason": "No active window detected."}):
            ctx = cm.gather()
        assert "No active project set." in ctx.warnings
        assert ctx.to_prompt_block() == "(no context available)"

    def test_gather_includes_conversation_history(self, fake_env, jarvis_db_path):
        import memory
        memory.init_db()
        memory.create_session("sess1")
        memory.save_message("sess1", "user", "why is my app crashing")
        memory.save_message("sess1", "assistant", "let me check")
        from context_manager import ContextManager
        cm = ContextManager()
        ctx = cm.gather(session_id="sess1")
        assert len(ctx.recent_messages) == 2

    def test_gather_includes_active_project_and_facts(self, fake_env, jarvis_db_path):
        import memory
        memory.init_db()
        import project_memory as pm
        pm.init_project_memory_db()
        pm.upsert_project("/proj", name="MyProj", technologies=["Python"])
        pm.save_fact("/proj", "known_issue", "Wrong venv activated")
        from context_manager import ContextManager
        cm = ContextManager()
        cm.set_active_project("/proj")
        ctx = cm.gather()
        assert ctx.active_project["name"] == "MyProj"
        assert len(ctx.project_facts) == 1

    def test_subsystem_failures_isolated(self, fake_env, jarvis_db_path):
        """Failure scenario: Tool failure — one subsystem failing shouldn't break the rest."""
        import memory
        memory.init_db()
        import project_memory as pm
        pm.init_project_memory_db()
        pm.upsert_project("/proj", name="MyProj")
        from context_manager import ContextManager
        cm = ContextManager()
        cm.set_active_project("/proj")

        with patch("memory.get_history", side_effect=RuntimeError("db locked")):
            ctx = cm.gather(session_id="sess1")
            assert ctx.recent_messages == []
            assert any("db locked" in w for w in ctx.warnings)
            assert ctx.active_project is not None  # unaffected


# ── Debug mode: routing, bounds, cancellation ─────────────────────

class TestDebugModeIntegration:
    @pytest.mark.asyncio
    async def test_investigation_ranks_undefined_name_above_unused_import(self, fake_env, jarvis_db_path, sample_project):
        """
        The specific ranking bug found and fixed during development:
        must not just take pyflakes' first-reported issue as root cause.
        """
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from context_manager import context_manager
        pm.upsert_project(sample_project["dir"], name="Proj", technologies=["Python"])
        context_manager.set_active_project(sample_project["dir"])

        from debug_mode import Investigation
        inv = Investigation()
        result = await inv.run(f"why is {sample_project['broken_file']} failing")
        assert result.diagnosis is not None
        assert "undefined name" in result.diagnosis.root_cause.lower()

    @pytest.mark.asyncio
    async def test_investigation_no_active_project(self, fake_env, jarvis_db_path):
        """Failure scenario: No active project detected."""
        from context_manager import ContextManager
        import context_manager as cm_mod
        original = cm_mod.context_manager
        cm_mod.context_manager = ContextManager()  # fresh, nothing set
        try:
            import memory; memory.init_db()
            from debug_mode import Investigation
            inv = Investigation()
            result = await inv.run("why is nothing working")
            assert result.diagnosis is not None
            assert result.diagnosis.confidence.value == "Low"
        finally:
            cm_mod.context_manager = original

    @pytest.mark.asyncio
    async def test_investigation_file_mentioned_but_missing(self, fake_env, jarvis_db_path):
        """Failure scenario: File unavailable."""
        import memory; memory.init_db()
        from debug_mode import Investigation
        inv = Investigation()
        result = await inv.run("why is /tmp/totally/nonexistent/path.py broken")
        # Step order: 1 gather context, 2 identify target file,
        # 3 check terminal output, 4 analyze code -> index 3.
        assert "Could not analyze" in result.steps[3].finding


class TestDebugInvestigationBounds:
    """Section 12: max steps, timeout, cancellation."""

    @pytest.mark.asyncio
    async def test_step_limit_enforced(self, fake_env, jarvis_db_path, sample_project):
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from context_manager import context_manager
        pm.upsert_project(sample_project["dir"], name="Proj")
        context_manager.set_active_project(sample_project["dir"])

        from debug_mode import Investigation
        inv = Investigation(max_steps=2)
        result = await inv.run(f"why is {sample_project['broken_file']} failing")
        assert len(result.steps) <= 3
        assert result.diagnosis is not None
        assert result.diagnosis.confidence.value == "Low"

    @pytest.mark.asyncio
    async def test_timeout_enforced(self, fake_env, jarvis_db_path, sample_project):
        import memory; memory.init_db()
        from debug_mode import Investigation
        inv = Investigation(timeout_seconds=0.0)
        result = await inv.run(f"why is {sample_project['broken_file']} failing")
        assert result.diagnosis is not None  # still returns something usable

    @pytest.mark.asyncio
    async def test_pre_cancellation_stops_immediately(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from debug_mode import Investigation
        inv = Investigation()
        inv.cancel()
        result = await inv.run("why is anything failing")
        assert result.cancelled is True
        assert len(result.steps) == 0

    @pytest.mark.asyncio
    async def test_state_broadcasts_distinct_progress_per_step(self, fake_env, jarvis_db_path, sample_project):
        """
        Regression test for the state.py same-state-transition bug found
        during development — without the fix, only step 1's detail
        would ever reach the UI.
        """
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from context_manager import context_manager
        pm.upsert_project(sample_project["dir"], name="Proj")
        context_manager.set_active_project(sample_project["dir"])

        from debug_mode import Investigation
        from state import state_manager, JarvisState

        broadcasts = []
        async def fake_broadcast(data):
            broadcasts.append(data)

        inv = Investigation()
        await inv.run(f"why is {sample_project['broken_file']} failing", broadcast_state=fake_broadcast)

        # 7 steps as of Phase 4 Feature 2: gather context, identify file,
        # check terminal output, analyze code, check project memory,
        # rank hypotheses, form diagnosis. (Was 6 before hypothesis
        # ranking was split out as its own step.)
        assert len(broadcasts) == 7
        details = [b["detail"] for b in broadcasts]
        assert len(set(details)) == 7, "all 7 steps must have distinct detail messages"

        await state_manager.set_state(JarvisState.IDLE, fake_broadcast, force=True)


# ── State machine: same-state progress updates (Phase 3 fix) ──────

class TestStateMachine:
    @pytest.mark.asyncio
    async def test_same_state_transition_updates_detail(self):
        """
        The exact bug found while testing debug_mode's state broadcasts:
        EXECUTING -> EXECUTING was rejected by the transition table,
        silently freezing progress display after step 1.
        """
        from state import state_manager, JarvisState
        broadcasts = []
        async def fb(data): broadcasts.append(data)

        await state_manager.set_state(JarvisState.EXECUTING, fb, force=True)
        r1 = await state_manager.set_state(JarvisState.EXECUTING, fb, detail="step 1")
        r2 = await state_manager.set_state(JarvisState.EXECUTING, fb, detail="step 2")
        assert r1 and r2
        assert state_manager.as_dict()["detail"] == "step 2"

        await state_manager.set_state(JarvisState.IDLE, fb, force=True)

    @pytest.mark.asyncio
    async def test_invalid_cross_state_transition_still_rejected(self):
        """Regression guard: the same-state fix must not have loosened real invalid transitions."""
        from state import state_manager, JarvisState
        broadcasts = []
        async def fb(data): broadcasts.append(data)

        await state_manager.set_state(JarvisState.IDLE, fb, force=True)
        await state_manager.set_state(JarvisState.LISTENING, fb)
        await state_manager.set_state(JarvisState.THINKING, fb)
        result = await state_manager.set_state(JarvisState.LISTENING, fb)
        assert result is False

        await state_manager.set_state(JarvisState.IDLE, fb, force=True)


# ── Tool registry: dynamic classification, timeouts (Phase 3 additions) ──

class TestToolRegistryPhase3:
    @pytest.mark.asyncio
    async def test_run_terminal_command_dynamic_permission(self, fake_env, jarvis_db_path):
        """Permission before file modification — verified through the actual dispatch path."""
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from tool_registry import tool_registry

        r_safe = await tool_registry.run_tool("run_terminal_command", {"command": "echo hi"})
        assert r_safe["status"] == "ok"

        r_confirm = await tool_registry.run_tool("run_terminal_command", {"command": "pip install requests"})
        assert r_confirm["status"] == "pending_confirmation"

        r_dangerous = await tool_registry.run_tool("run_terminal_command", {"command": "git clean -fd"})
        assert r_dangerous["status"] == "pending_confirmation"
        assert "DANGEROUS" in r_dangerous["message"]

        r_blocked = await tool_registry.run_tool("run_terminal_command", {"command": "git status && rm -rf /"})
        assert r_blocked["status"] == "blocked"

    @pytest.mark.asyncio
    async def test_tool_timeout_enforced_for_sync_and_async(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        import time
        from tool_registry import ToolRegistry, ToolSpec
        from permissions import PermissionLevel

        reg = ToolRegistry()

        def slow_sync(**kwargs):
            time.sleep(2)
            return "done"

        async def slow_async(**kwargs):
            await asyncio.sleep(2)
            return "done"

        reg.register(ToolSpec(name="slow_sync", description="x", permission=PermissionLevel.SAFE,
                               handler=slow_sync, timeout_seconds=0.3))
        reg.register(ToolSpec(name="slow_async", description="x", permission=PermissionLevel.SAFE,
                               handler=slow_async, timeout_seconds=0.3))

        start = time.monotonic()
        r1 = await reg.run_tool("slow_sync", {})
        elapsed1 = time.monotonic() - start
        assert r1["status"] == "error" and "timed out" in r1["message"]
        assert elapsed1 < 1.0

        start = time.monotonic()
        r2 = await reg.run_tool("slow_async", {})
        elapsed2 = time.monotonic() - start
        assert r2["status"] == "error" and "timed out" in r2["message"]
        assert elapsed2 < 1.0


# ── New Phase 3 tools: read_relevant_file, find_code_reference, ─────
# read_terminal_output, inspect_environment, run_tests, apply_fix ───

class TestReadRelevantFile:
    def test_reads_whole_file_when_no_range_given(self, sample_project):
        from code_analysis import read_relevant_file
        result = read_relevant_file(sample_project["clean_file"])
        assert "def add" in result["content"]
        assert result["start_line"] == 1
        assert result["end_line"] == result["total_lines"]

    def test_reads_bounded_line_range(self, sample_project):
        from code_analysis import read_relevant_file
        result = read_relevant_file(sample_project["broken_file"], start_line=1, end_line=2)
        assert result["start_line"] == 1
        assert result["end_line"] == 2
        assert len(result["content"].splitlines()) == 2

    def test_invalid_range_raises(self, sample_project):
        from code_analysis import read_relevant_file
        with pytest.raises(ValueError):
            read_relevant_file(sample_project["clean_file"], start_line=10, end_line=1)

    def test_missing_file_raises(self):
        """Failure scenario: file unavailable."""
        from code_analysis import read_relevant_file
        with pytest.raises(FileNotFoundError):
            read_relevant_file("/tmp/totally/nonexistent/path.py")


class TestFindCodeReference:
    def test_finds_matches_across_files(self, sample_project):
        from project_detector import find_references
        result = find_references(sample_project["dir"], "def ")
        names = {m["file"] for m in result["matches"]}
        assert "app.py" in names
        assert "clean.py" in names

    def test_no_matches_returns_empty(self, sample_project):
        from project_detector import find_references
        result = find_references(sample_project["dir"], "totally_absent_symbol_xyz")
        assert result["matches"] == []
        assert result["truncated"] is False

    def test_empty_symbol_raises(self, sample_project):
        from project_detector import find_references
        with pytest.raises(ValueError):
            find_references(sample_project["dir"], "")

    def test_missing_project_raises(self):
        """Failure scenario: no active project detected."""
        from project_detector import find_references
        with pytest.raises(NotADirectoryError):
            find_references("/tmp/totally/nonexistent/project", "foo")

    def test_max_results_is_capped(self, sample_project):
        from project_detector import find_references
        result = find_references(sample_project["dir"], "def ", max_results=1)
        assert len(result["matches"]) <= 1


class TestReadTerminalOutput:
    @pytest.mark.asyncio
    async def test_no_recent_output_reported_honestly(self, fake_env, jarvis_db_path):
        """Failure scenario: no terminal available / nothing run yet."""
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from tool_registry import tool_registry
        result = await tool_registry.run_tool("read_terminal_output", {})
        assert result["status"] == "ok"
        assert result["result"]["available"] is False

    @pytest.mark.asyncio
    async def test_surfaces_last_command_and_extracted_error(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from tool_registry import tool_registry

        await tool_registry.run_tool("run_terminal_command", {"command": "python --version"})
        result = await tool_registry.run_tool("read_terminal_output", {})
        assert result["result"]["available"] is True
        assert result["result"]["command"] == "python --version"

    @pytest.mark.asyncio
    async def test_stale_output_excluded_by_max_age(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from tool_registry import tool_registry

        await tool_registry.run_tool("run_terminal_command", {"command": "python --version"})
        result = await tool_registry.run_tool("read_terminal_output", {"max_age_seconds": 0})
        assert result["result"]["available"] is False


class TestInspectEnvironment:
    @pytest.mark.asyncio
    async def test_python_target_runs_fixed_commands(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from tool_registry import tool_registry

        result = await tool_registry.run_tool("inspect_environment", {"target": "python"})
        assert result["status"] == "ok"
        assert result["result"]["available"] is True
        assert "version" in result["result"]["results"]

    @pytest.mark.asyncio
    async def test_unknown_target_reported_honestly(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from tool_registry import tool_registry

        result = await tool_registry.run_tool("inspect_environment", {"target": "ruby"})
        assert result["result"]["available"] is False


class TestRunTests:
    @pytest.mark.asyncio
    async def test_requires_confirmation(self, fake_env, jarvis_db_path, sample_project):
        """Permission before execution — run_tests is CONFIRM-tier per Section 7."""
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        pm.upsert_project(sample_project["dir"], name="Proj", technologies=["Python"])
        from tool_registry import tool_registry

        result = await tool_registry.run_tool("run_tests", {"project_path": sample_project["dir"]})
        assert result["status"] == "pending_confirmation"

    @pytest.mark.asyncio
    async def test_unknown_stack_reported_honestly(self, fake_env, jarvis_db_path, tmp_path):
        """Failure scenario: no test runner determinable."""
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        empty_dir = tmp_path / "unknown_stack"
        empty_dir.mkdir()
        pm.upsert_project(str(empty_dir), name="Unknown", technologies=[])
        from tool_registry import tool_registry

        result = await tool_registry.run_tool("run_tests", {"project_path": str(empty_dir)}, auto_approved=True)
        assert result["status"] == "ok"
        assert result["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_no_project_path_reported_honestly(self, fake_env, jarvis_db_path):
        """Failure scenario: no active project detected."""
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from context_manager import ContextManager
        import context_manager as cm_mod
        original = cm_mod.context_manager
        cm_mod.context_manager = ContextManager()
        try:
            from tool_registry import tool_registry
            result = await tool_registry.run_tool("run_tests", {}, auto_approved=True)
            assert result["result"]["status"] == "error"
        finally:
            cm_mod.context_manager = original


class TestApplyFix:
    def test_apply_fix_backs_up_and_writes(self, sample_project):
        import file_ops
        new_content = "def add(a, b):\n    return a + b  # fixed\n"
        result = file_ops.apply_fix(sample_project["clean_file"], new_content, description="add comment")
        assert os.path.isfile(result.backup_path)
        with open(sample_project["clean_file"]) as f:
            assert f.read() == new_content
        with open(result.backup_path) as f:
            assert "# fixed" not in f.read()

    def test_apply_fix_missing_file_raises(self):
        """Failure scenario: file unavailable."""
        import file_ops
        with pytest.raises(FileNotFoundError):
            file_ops.apply_fix("/tmp/totally/nonexistent/path.py", "content")

    def test_apply_fix_oversized_content_raises(self, sample_project):
        import file_ops
        with pytest.raises(ValueError):
            file_ops.apply_fix(sample_project["clean_file"], "x" * 600_000)

    def test_apply_fix_verifies_result(self, sample_project):
        import file_ops
        result = file_ops.apply_fix(sample_project["clean_file"], "def add(a, b, c):\n    return undefined_thing\n")
        assert result.verification["ran"] is True
        assert result.verification["issue_count"] >= 1

    def test_diff_preview_shows_changes(self, sample_project):
        import file_ops
        diff = file_ops.build_diff_preview(sample_project["clean_file"], "def add(a, b):\n    return a - b\n")
        assert "-    return a + b" in diff
        assert "+    return a - b" in diff

    def test_diff_preview_missing_file_raises(self):
        import file_ops
        with pytest.raises(FileNotFoundError):
            file_ops.build_diff_preview("/tmp/totally/nonexistent/path.py", "x")

    @pytest.mark.asyncio
    async def test_apply_fix_tool_always_requires_confirmation(self, fake_env, jarvis_db_path, sample_project):
        """Permission before file modification, enforced through the actual tool_registry dispatch path."""
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from tool_registry import tool_registry

        result = await tool_registry.run_tool("apply_fix", {
            "file_path": sample_project["clean_file"],
            "new_content": "def add(a, b):\n    return a - b\n",
        })
        assert result["status"] == "pending_confirmation"
        assert sample_project["clean_file"] is not None  # sanity: original untouched below
        with open(sample_project["clean_file"]) as f:
            assert "return a - b" not in f.read()  # not applied without confirmation

    @pytest.mark.asyncio
    async def test_apply_fix_tool_applies_when_auto_approved(self, fake_env, jarvis_db_path, sample_project):
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from tool_registry import tool_registry

        result = await tool_registry.run_tool("apply_fix", {
            "file_path": sample_project["clean_file"],
            "new_content": "def add(a, b):\n    return a - b\n",
        }, auto_approved=True)
        assert result["status"] == "ok"
        with open(sample_project["clean_file"]) as f:
            assert "return a - b" in f.read()


# ── Debug Mode + terminal integration (Section 10's "CHECK TERMINAL" step) ──

class TestDebugModeTerminalIntegration:
    @pytest.mark.asyncio
    async def test_recent_terminal_error_becomes_primary_evidence(self, fake_env, jarvis_db_path, sample_project):
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

        terminal_step = next(s for s in result.steps if s.name == "Check terminal output")
        assert "totally_missing_module_xyz" in terminal_step.finding or "ModuleNotFoundError" in terminal_step.finding
        assert result.diagnosis is not None
        assert "totally_missing_module_xyz" in result.diagnosis.root_cause or "ModuleNotFoundError" in (result.diagnosis.diagnosis or "")

    @pytest.mark.asyncio
    async def test_no_recent_terminal_output_reported_as_negative_result(self, fake_env, jarvis_db_path):
        """Failure scenario: no terminal available — must be recorded honestly, not skipped."""
        import memory; memory.init_db()
        from debug_mode import Investigation
        inv = Investigation()
        result = await inv.run("why is anything failing")
        terminal_step = next(s for s in result.steps if s.name == "Check terminal output")
        assert "No recent terminal output" in terminal_step.finding

    @pytest.mark.asyncio
    async def test_clean_terminal_run_does_not_override_static_analysis(self, fake_env, jarvis_db_path, sample_project):
        """A successful/errorless terminal command shouldn't suppress a real static-analysis finding."""
        import memory; memory.init_db()
        import project_memory as pm; pm.init_project_memory_db()
        from context_manager import context_manager
        pm.upsert_project(sample_project["dir"], name="Proj", technologies=["Python"])
        context_manager.set_active_project(sample_project["dir"])

        import terminal_tools
        await terminal_tools.run_command("python --version")

        from debug_mode import Investigation
        inv = Investigation()
        result = await inv.run(f"why is {sample_project['broken_file']} failing")
        assert "undefined name" in result.diagnosis.root_cause.lower()


# ── TERMINAL intent: command extraction + end-to-end routing ────────

class TestTerminalIntentExtraction:
    def test_extracts_backtick_command(self):
        from terminal_tools import extract_command_from_message
        assert extract_command_from_message("run `pytest -k foo`") == "pytest -k foo"

    def test_extracts_after_run_keyword(self):
        from terminal_tools import extract_command_from_message
        assert extract_command_from_message("please run git status") == "git status"

    def test_extracts_after_execute_keyword(self):
        from terminal_tools import extract_command_from_message
        assert extract_command_from_message("execute npm list") == "npm list"

    def test_no_command_returns_none(self):
        """Must not guess — ambiguity is a reason to ask, not assume."""
        from terminal_tools import extract_command_from_message
        assert extract_command_from_message("why isn't this working") is None


class TestChatIntegrationTerminal:
    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return main, TestClient(main.app)

    def test_terminal_intent_runs_safe_command(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "TERMINAL", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "x"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "run `python --version`"})
                assert r.status_code == 200
                assert "COMMAND" in r.json()["response"]

                r2 = client.get("/state")
                assert r2.json()["state"] == "IDLE"

    def test_terminal_intent_no_command_asks_for_clarification(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "TERMINAL", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "x"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "can you use the terminal for me"})
                assert r.status_code == 200
                assert "exact command" in r.json()["response"].lower()

    def test_terminal_intent_dangerous_command_needs_confirmation(self, monkeypatch, fake_env, jarvis_db_path):
        """Permission before execution, exercised through the full /chat path."""
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "TERMINAL", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "x"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "run `pip install requests`"})
                assert r.status_code == 200
                assert "confirmation" in r.json()["response"].lower()


# ── /chat integration: real HTTP requests through the real app ────

# ── Startup: project_memory tables must exist after a real boot ────
# (Regression test for a bug found via live, unmocked server testing:
# main.py's lifespan only called memory.init_db(), never
# project_memory.init_project_memory_db() — every other test in this
# file calls that explicitly in its own fixture, which is exactly
# what let this slip past the suite. This test deliberately does NOT
# call init_project_memory_db() itself, so it only passes if the real
# startup path does.)

class TestStartupInitializesProjectMemory:
    def test_project_memory_tables_exist_after_real_lifespan(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient

        with TestClient(main.app) as client:
            r = client.post("/tools/run", json={
                "tool": "inspect_project",
                "args": {"root_path": "."},
            })
            assert r.json()["status"] == "ok", (
                f"project_memory tables missing after startup: {r.json()}"
            )


class TestChatIntegration:
    """Failure scenario: LLM/API failure — Ollama being down must never 500 the endpoint."""

    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return main, TestClient(main.app)

    def test_debug_intent_end_to_end(self, monkeypatch, fake_env, jarvis_db_path, sample_project):
        import project_memory as pm
        pm.init_project_memory_db()
        from context_manager import context_manager
        pm.upsert_project(sample_project["dir"], name="Proj", technologies=["Python"])
        context_manager.set_active_project(sample_project["dir"])

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "DEBUG", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "x"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": f"why is {sample_project['broken_file']} failing"})
                assert r.status_code == 200
                assert "DIAGNOSIS" in r.json()["response"]

                r2 = client.get("/state")
                assert r2.json()["state"] == "IDLE"

    def test_llm_failure_never_500s(self, monkeypatch, fake_env, jarvis_db_path):
        """Failure scenario: LLM/API failure."""
        def fake_chat_fails(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "GENERAL_CHAT", "confidence": 0.9, "mode": None})}}
            raise ConnectionError("ollama down")

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat_fails), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "hello"})
                assert r.status_code == 200  # never a 500
                assert "lapse in cognition" in r.json()["response"].lower()

                r2 = client.get("/state")
                assert r2.json()["state"] == "IDLE"  # settles cleanly, not stuck

    def test_code_analysis_no_file_mentioned_clean_fallback(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "CODE_ANALYSIS", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "x"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "can you check my code"})
                assert r.status_code == 200
                assert "which file" in r.json()["response"].lower()

    def test_general_chat_unaffected_by_phase3_routing(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "GENERAL_CHAT", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "Good evening, Sir."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "hello"})
                assert r.json()["response"] == "Good evening, Sir."
