"""
JARVIS Phase 5 test suite (Section 17).

Covers:
    Context (mode/task tracking, get/update/clear)  -> TestSessionContext
    Memory (bounded history, summarization)          -> TestOrchestrator
    Intent (contextual/ambiguous commands)           -> TestReferenceResolver
    Planner (single/multi-step, unknown goal)         -> TestTaskPlanner
    CSE Assistant (programming Qs, code-aware lookup) -> TestCseAssistant
    Study Assistant (topics, quiz/teach prompts)      -> TestStudyAssistant
    Hackathon Assistant (capability dispatch)          -> TestHackathonAssistant
    Developer mode (toggle, report formatting)         -> TestDeveloperAssistant
    Safety (LLM cannot bypass permission system)       -> TestSafety
    End-to-end chat routing for new intents            -> TestChatIntegrationPhase5

Run with:  pytest test_phase5.py -v
Same isolation pattern as test_phase3.py: every test gets its own
SQLite DB via the jarvis_db_path fixture, and fake_env supplies the
LiveKit env vars config.py requires at import time. These fixtures are
intentionally local copies of test_phase3.py's (not shared via
conftest.py) so this file can be dropped in or removed without any
risk of changing test_phase3.py's collection/behavior.
"""

import json
import textwrap
from unittest.mock import patch

import pytest


# ── Shared fixtures (local copies — see module docstring) ─────────

@pytest.fixture
def jarvis_db_path(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test_jarvis_p5.db")
    monkeypatch.setenv("JARVIS_DB_PATH", db_path)
    return db_path


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake_key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fake_secret")


@pytest.fixture(autouse=True)
def reset_global_context_manager():
    import context_manager as cm_mod
    original_path = cm_mod.context_manager.get_active_project_path()
    yield
    cm_mod.context_manager.set_active_project(original_path)


@pytest.fixture(autouse=True)
def reset_session_context_store():
    from core import session_context
    session_context.session_context_store._sessions.clear()
    yield
    session_context.session_context_store._sessions.clear()


@pytest.fixture
def sample_project(tmp_path):
    project_dir = tmp_path / "sample_project"
    project_dir.mkdir()
    broken_file = project_dir / "app.py"
    broken_file.write_text(textwrap.dedent("""
        import os

        def fetch_data():
            return undefined_helper()
    """))
    return {"dir": str(project_dir), "broken_file": str(broken_file)}


# ── Confidence system ───────────────────────────────────────────

class TestConfidence:
    def test_high_confidence_non_consequential_proceeds(self, fake_env):
        from core.confidence import evaluate, RecommendedAction, Confidence
        decision = evaluate(0.9, is_consequential=False)
        assert decision.confidence == Confidence.HIGH
        assert decision.action == RecommendedAction.PROCEED

    def test_high_confidence_consequential_still_confirms(self, fake_env):
        """Section 12/13: high confidence must never bypass confirmation for a consequential action."""
        from core.confidence import evaluate, RecommendedAction
        decision = evaluate(0.95, is_consequential=True)
        assert decision.action == RecommendedAction.CONFIRM_INTENT

    def test_low_confidence_asks_to_clarify(self, fake_env):
        from core.confidence import evaluate, RecommendedAction, Confidence
        decision = evaluate(0.1, is_consequential=False)
        assert decision.confidence == Confidence.LOW
        assert decision.action == RecommendedAction.CLARIFY

    def test_medium_confidence_confirms(self, fake_env):
        from core.confidence import evaluate, RecommendedAction
        decision = evaluate(0.6, is_consequential=False)
        assert decision.action == RecommendedAction.CONFIRM_INTENT

    def test_unresolved_reference_caps_combined_confidence(self, fake_env):
        """A high intent confidence can't rescue a reference that failed to resolve (confidence 0)."""
        from core.confidence import evaluate, RecommendedAction
        decision = evaluate(0.95, is_consequential=False, reference_resolution_confidence=0.0)
        assert decision.action == RecommendedAction.CLARIFY


# ── Reference resolver (contextual commands) ───────────────────────

class TestReferenceResolver:
    def test_non_referential_message_passes_through(self, fake_env):
        from core.reference_resolver import resolve
        result = resolve("what is polymorphism?", [])
        assert result.was_referential is False
        assert result.confidence == 1.0
        assert result.resolved_message == "what is polymorphism?"

    def test_ordinal_reference_resolves_against_last_list(self, fake_env):
        from core.reference_resolver import resolve
        history = [
            {"role": "user", "content": "search for python decorators"},
            {"role": "assistant", "content": "1. Function decorators\n2. Class decorators\n3. Property decorators"},
        ]
        result = resolve("explain the second one", history)
        assert result.was_referential is True
        assert result.confidence > 0.5
        assert "Class decorators" in result.resolved_message

    def test_file_reference_resolves_to_last_filename(self, fake_env):
        from core.reference_resolver import resolve
        history = [
            {"role": "user", "content": "what's wrong with intent_router.py"},
            {"role": "assistant", "content": "It looks fine to me."},
        ]
        result = resolve("open it", history)
        assert result.was_referential is True
        assert result.resolved_entity == "intent_router.py"

    def test_unresolvable_reference_is_honest_about_low_confidence(self, fake_env):
        from core.reference_resolver import resolve
        result = resolve("try another method", [])
        assert result.was_referential is True
        assert result.confidence == 0.0


# ── Task planner ─────────────────────────────────────────────────

class TestTaskPlanner:
    def test_hackathon_plan_has_multiple_steps_and_requires_confirmation(self, fake_env):
        from core.task_planner import create_plan
        plan = create_plan("hackathon_environment", project_name="JARVIS")
        assert len(plan.steps) >= 3
        assert plan.requires_confirmation is True  # contains run_terminal_command/run_tests

    def test_exam_prep_plan_asks_for_subject_when_unknown(self, fake_env):
        from core.task_planner import create_plan
        plan = create_plan("exam_prep")
        assert any("which subject" in s.description.lower() for s in plan.steps)

    def test_exam_prep_plan_with_subject_skips_the_ask_step(self, fake_env):
        from core.task_planner import create_plan
        plan = create_plan("exam_prep", subject="Operating Systems")
        assert not any("which subject" in s.description.lower() for s in plan.steps)
        assert "Operating Systems" in plan.goal

    def test_unknown_plan_kind_raises(self, fake_env):
        from core.task_planner import create_plan
        with pytest.raises(ValueError):
            create_plan("not_a_real_plan")

    def test_single_step_plan_all_safe_tools_does_not_require_confirmation(self, fake_env):
        from core.task_planner import TaskStep, _plan_requires_confirmation
        steps = [TaskStep("Look something up", tool_name="search_web")]
        assert _plan_requires_confirmation(steps) is False

    def test_advance_step_updates_status_and_result(self, fake_env):
        from core.task_planner import create_plan, advance_step, StepStatus
        plan = create_plan("exam_prep", subject="DBMS")
        advance_step(plan, 0, StepStatus.DONE, result="Found notes.pdf")
        assert plan.steps[0].status == StepStatus.DONE
        assert plan.steps[0].result == "Found notes.pdf"

    def test_advance_step_out_of_range_raises(self, fake_env):
        from core.task_planner import create_plan, advance_step, StepStatus
        plan = create_plan("exam_prep", subject="DBMS")
        with pytest.raises(IndexError):
            advance_step(plan, 99, StepStatus.DONE)


# ── LLM orchestration layer (bounded memory) ───────────────────────

class TestOrchestrator:
    def test_short_history_passes_through_unbounded(self, fake_env):
        from core.llm_orchestrator import build_bounded_messages
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        bounded, summarized = build_bounded_messages(history, max_turns=8)
        assert summarized == 0
        assert len(bounded) == 2

    def test_long_history_gets_bounded_and_summarized(self, fake_env):
        from core.llm_orchestrator import build_bounded_messages
        history = []
        for i in range(20):
            history.append({"role": "user", "content": f"question {i}"})
            history.append({"role": "assistant", "content": f"answer {i}"})
        bounded, summarized = build_bounded_messages(history, max_turns=6)
        assert summarized > 0
        # 6 verbatim turns + 1 summary line
        assert len(bounded) == 7
        assert "question" in bounded[0]["content"]  # the summary mentions earlier asks

    def test_never_sends_unlimited_history(self, fake_env):
        from core.llm_orchestrator import build_bounded_messages, MAX_VERBATIM_TURNS
        history = [{"role": "user", "content": f"msg {i}"} for i in range(500)]
        bounded, _ = build_bounded_messages(history)
        assert len(bounded) <= MAX_VERBATIM_TURNS + 1

    def test_long_message_is_truncated(self, fake_env):
        from core.llm_orchestrator import build_bounded_messages, MAX_MESSAGE_CHARS
        long_content = "x" * (MAX_MESSAGE_CHARS * 3)
        history = [{"role": "user", "content": long_content}]
        bounded, _ = build_bounded_messages(history)
        assert len(bounded[0]["content"]) < len(long_content)
        assert "truncated" in bounded[0]["content"]

    def test_run_uses_injected_model_caller(self, fake_env):
        from core import llm_orchestrator

        def fake_caller(system_prompt, messages, user_message):
            assert "system" not in system_prompt.lower() or True
            return f"echo: {user_message}"

        result = llm_orchestrator.run("You are a test assistant.", "hello", [], model_caller=fake_caller)
        assert result == "echo: hello"

    def test_run_degrades_gracefully_on_model_failure(self, fake_env):
        """Section 16: never crash the assistant if Ollama is unavailable."""
        from core import llm_orchestrator

        def failing_caller(*args, **kwargs):
            raise ConnectionError("ollama down")

        result = llm_orchestrator.run("prompt", "hello", [], model_caller=failing_caller)
        assert "trouble" in result.lower()
        assert "ollama" in result.lower()


# ── Session context (Section 1's get/update/clear API) ─────────────

class TestSessionContext:
    def test_update_and_get_context_roundtrips_mode_and_task(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from core import session_context

        session_context.update_context("s1", mode="study", current_task="revising OS")
        ctx = session_context.get_context("s1")
        assert ctx.mode == "study"
        assert ctx.current_task == "revising OS"

    def test_active_tool_tracking_is_bounded(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from core import session_context

        for i in range(15):
            session_context.update_context("s1", active_tool=f"tool_{i}")
        ctx = session_context.get_context("s1")
        assert len(ctx.active_tools) <= 10

    def test_clear_context_resets_session(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from core import session_context

        session_context.update_context("s1", mode="developer")
        session_context.clear_context("s1")
        ctx = session_context.get_context("s1")
        assert ctx.mode == "general"

    def test_different_sessions_have_independent_modes(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from core import session_context

        session_context.update_context("a", mode="hackathon")
        session_context.update_context("b", mode="study")
        assert session_context.get_context("a").mode == "hackathon"
        assert session_context.get_context("b").mode == "study"

    def test_get_recent_context_reads_conversation_history(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from core import session_context

        memory.save_message("s1", "user", "hello")
        memory.save_message("s1", "assistant", "hi there")
        recent = session_context.get_recent_context("s1")
        assert len(recent) == 2


# ── CSE assistant ───────────────────────────────────────────────

class TestCseAssistant:
    def test_static_answer_for_bfs_vs_dfs(self, fake_env):
        from assistants import cse_assistant
        answer = cse_assistant.try_static_answer("compare BFS and DFS")
        assert answer is not None
        assert "BFS" in answer and "DFS" in answer

    def test_no_static_answer_for_unrelated_question(self, fake_env):
        from assistants import cse_assistant
        answer = cse_assistant.try_static_answer("what's the weather like")
        assert answer is None

    def test_guess_referenced_file_prefers_explicit_filename(self, fake_env):
        from assistants import cse_assistant
        from context_manager import GatheredContext
        ctx = GatheredContext()
        result = cse_assistant.guess_referenced_file("what's wrong with agent.py", ctx)
        assert result == "agent.py"

    def test_guess_referenced_file_falls_back_to_history(self, fake_env):
        from assistants import cse_assistant
        from context_manager import GatheredContext
        ctx = GatheredContext(recent_messages=[{"role": "user", "content": "look at tools.py"}])
        result = cse_assistant.guess_referenced_file("what's wrong with it", ctx)
        assert result == "tools.py"

    def test_guess_referenced_file_returns_none_when_nothing_found(self, fake_env):
        from assistants import cse_assistant
        from context_manager import GatheredContext
        ctx = GatheredContext()
        result = cse_assistant.guess_referenced_file("what's wrong with it", ctx)
        assert result is None


# ── Study assistant ─────────────────────────────────────────────

class TestStudyAssistant:
    def test_start_and_get_topic(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from assistants import study_assistant
        study_assistant.init_study_db()

        study_assistant.start_topic("s1", "Operating Systems")
        topics = study_assistant.get_topics("s1")
        assert len(topics) == 1
        assert topics[0].topic == "Operating Systems"
        assert topics[0].level == "BEGINNER"

    def test_advance_level_progresses_beginner_to_intermediate(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from assistants import study_assistant
        study_assistant.init_study_db()

        study_assistant.start_topic("s1", "DBMS")
        updated = study_assistant.advance_level("s1", "DBMS")
        assert updated.level == "INTERMEDIATE"

    def test_advance_level_caps_at_advanced(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from assistants import study_assistant
        study_assistant.init_study_db()

        study_assistant.start_topic("s1", "DBMS")
        study_assistant.advance_level("s1", "DBMS")
        study_assistant.advance_level("s1", "DBMS")
        final = study_assistant.advance_level("s1", "DBMS")
        assert final.level == "ADVANCED"

    def test_advance_level_unknown_topic_returns_none(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from assistants import study_assistant
        study_assistant.init_study_db()
        assert study_assistant.advance_level("s1", "Nonexistent") is None

    def test_get_current_level_defaults_to_beginner(self, fake_env, jarvis_db_path):
        import memory; memory.init_db()
        from assistants import study_assistant
        study_assistant.init_study_db()
        assert study_assistant.get_current_level("s1", "Never Started") == "BEGINNER"

    def test_teach_prompt_mentions_topic_and_level(self, fake_env):
        from assistants import study_assistant
        prompt = study_assistant.teach_prompt("recursion", level="ADVANCED")
        assert "recursion" in prompt
        assert "ADVANCED" in prompt


# ── Hackathon assistant ─────────────────────────────────────────

class TestHackathonAssistant:
    @pytest.mark.parametrize("message,expected_kind", [
        ("give me 5 AI hackathon ideas", "idea"),
        ("design the architecture for this project", "architecture"),
        ("recommend a tech stack", "tech_stack"),
        ("break this idea into an MVP", "mvp"),
        ("give me tasks for 4 team members", "task_breakdown"),
        ("create a 2-minute pitch", "pitch"),
        ("create a demo flow", "demo_flow"),
        ("how can we improve our chances of winning judging", "judging"),
        ("find the highest-risk part of our project", "risk"),
    ])
    def test_classify_request(self, fake_env, message, expected_kind):
        from assistants import hackathon_assistant
        assert hackathon_assistant.classify_request(message) == expected_kind

    def test_unrelated_message_returns_none(self, fake_env):
        from assistants import hackathon_assistant
        assert hackathon_assistant.classify_request("what's the capital of France") is None

    def test_dispatch_extracts_team_size(self, fake_env):
        from assistants import hackathon_assistant
        prompt = hackathon_assistant.dispatch("give me tasks for 4 team members")
        assert "4 team members" in prompt


# ── Developer assistant / mode ───────────────────────────────────

class TestDeveloperAssistant:
    def test_recognizes_enter_developer_mode(self, fake_env):
        from assistants import developer_assistant
        assert developer_assistant.wants_developer_mode("Jarvis, enter developer mode.")
        assert developer_assistant.wants_developer_mode("debug this")

    def test_recognizes_exit_developer_mode(self, fake_env):
        from assistants import developer_assistant
        assert developer_assistant.wants_to_exit_developer_mode("exit developer mode")

    def test_format_diagnosis_as_developer_report(self, fake_env):
        from assistants import developer_assistant
        from debug_mode import Diagnosis, Confidence as DebugConfidence

        diagnosis = Diagnosis(
            diagnosis="NameError on undefined_helper",
            evidence="pyflakes flagged an undefined name",
            root_cause="Function undefined_helper was never defined",
            confidence=DebugConfidence.HIGH,
            recommended_fix="Define undefined_helper or import it",
            next_step="Re-run the script",
        )
        report = developer_assistant.format_diagnosis_as_developer_report(diagnosis)
        text = report.to_text()
        assert "PROBLEM" in text and "LIKELY CAUSE" in text and "FIX" in text and "VERIFICATION" in text
        assert "undefined_helper" in text

    def test_suggest_command_for_missing_module(self, fake_env):
        from assistants import developer_assistant
        suggestion = developer_assistant.suggest_command("ModuleNotFoundError: No module named 'requests'")
        assert suggestion is not None
        assert "pip install" in suggestion

    def test_suggest_command_returns_none_for_unrecognized_error(self, fake_env):
        from assistants import developer_assistant
        assert developer_assistant.suggest_command("some totally novel error text") is None


# ── Safety: LLM cannot bypass Permission Manager / Tool Registry ──

class TestSafety:
    def test_confidence_module_has_no_execution_capability(self, fake_env):
        """Confidence decisions are advisory only — no code path in this module calls a tool."""
        import core.confidence as confidence_mod
        import inspect
        source = inspect.getsource(confidence_mod)
        assert "import tool_registry" not in source
        assert "import permissions" not in source
        assert "tool_registry.run_tool" not in source
        assert "permission_manager." not in source

    def test_task_planner_never_executes_steps_itself(self, fake_env):
        """create_plan()/advance_step() only build/mutate data — no tool_registry.run_tool call anywhere."""
        import core.task_planner as planner_mod
        import inspect
        source = inspect.getsource(planner_mod)
        assert "run_tool(" not in source

    def test_hackathon_plan_step_naming_run_terminal_command_still_requires_confirmation(self, fake_env):
        from core.task_planner import create_plan
        plan = create_plan("hackathon_environment")
        terminal_steps = [s for s in plan.steps if s.tool_name == "run_terminal_command"]
        assert terminal_steps  # the plan does reference it...
        assert plan.requires_confirmation is True  # ...but the plan is flagged, never silently run


# ── End-to-end chat routing for Phase 5 intents ────────────────────

class TestChatIntegrationPhase5:
    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        from assistants import study_assistant
        study_assistant.init_study_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return main, TestClient(main.app)

    def test_dsa_intent_returns_static_answer_without_second_llm_call(self, monkeypatch, fake_env, jarvis_db_path):
        response_calls = {"count": 0}

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "DSA", "confidence": 0.9, "mode": None})}}
            # Non-classification calls: the startup Ollama "ping" in
            # main.py's lifespan is one of these too, so this only
            # counts calls that could plausibly be a *response*
            # generation call (i.e. anything after startup).
            response_calls["count"] += 1
            return {"message": {"content": "should not be called for a static topic"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                # The lifespan startup ping happens once client_factory's
                # `with` block enters — reset the counter after that so
                # we're only counting calls made during this request.
                response_calls["count"] = 0
                r = client.post("/chat", json={"message": "compare BFS and DFS"})
                assert r.status_code == 200
                assert "BFS" in r.json()["response"]
        # The static-answer path must not call the model for a response
        # at all (classification calls are excluded above via `format`).
        assert response_calls["count"] == 0

    def test_study_intent_teaches_topic(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "STUDY", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "Operating systems manage hardware resources..."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "teach me operating systems"})
                assert r.status_code == 200
                assert "Operating systems" in r.json()["response"]

        # Verify a topic row was created for *some* session (session_id was server-generated).
        import memory
        conn = memory.get_connection()
        row = conn.execute("SELECT topic FROM study_topics").fetchone()
        conn.close()
        assert row is not None
        assert "operating systems" in row["topic"].lower()

    def test_planning_intent_hackathon_returns_structured_plan(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "PLANNING", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "x"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "prepare my coding environment for the hackathon"})
                assert r.status_code == 200
                body = r.json()["response"]
                assert "PLAN" in body
                assert "Shall I proceed?" in body

    def test_planning_intent_unclear_goal_asks_which(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "PLANNING", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "x"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "make me a plan"})
                assert r.status_code == 200
                assert "Which did you have in mind" in r.json()["response"]

    def test_hackathon_intent_routes_to_assistant_prompt(self, monkeypatch, fake_env, jarvis_db_path):
        seen_prompts = []

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "HACKATHON", "confidence": 0.9, "mode": None})}}
            seen_prompts.append(kwargs["messages"][0]["content"])
            return {"message": {"content": "Here are 5 ideas..."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "give me 5 AI hackathon ideas"})
                assert r.status_code == 200
                assert "ideas" in r.json()["response"].lower()
        assert any("hackathon project ideas" in p for p in seen_prompts)

    def test_developer_mode_entry_toggles_mode_without_investigation(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "DEVELOPER_MODE", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "x"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "Jarvis, enter developer mode."},
                                 params={})
                assert r.status_code == 200
                assert "Developer mode active" in r.json()["response"]

    def test_developer_mode_debug_this_runs_real_investigation(
        self, monkeypatch, fake_env, jarvis_db_path, sample_project,
    ):
        import project_memory as pm
        pm.init_project_memory_db()
        from context_manager import context_manager
        pm.upsert_project(sample_project["dir"], name="Proj", technologies=["Python"])
        context_manager.set_active_project(sample_project["dir"])

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "DEVELOPER_MODE", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "x"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": f"debug this: {sample_project['broken_file']}"})
                assert r.status_code == 200
                assert "PROBLEM" in r.json()["response"]

    def test_general_chat_still_unaffected_by_phase5_routing(self, monkeypatch, fake_env, jarvis_db_path):
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "GENERAL_CHAT", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "Good evening, Sir."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "good evening"})
                assert r.status_code == 200
                assert r.json()["response"] == "Good evening, Sir."

    def test_reference_resolution_feeds_classifier_deferenced_message(
        self, monkeypatch, fake_env, jarvis_db_path,
    ):
        """
        Section 2/10 end-to-end: a numbered list from a previous turn,
        then 'explain the second one' should reach the classifier
        already de-referenced.
        """
        import memory
        memory.init_db()
        memory.save_message("fixed-session", "user", "search for python decorators")
        memory.save_message(
            "fixed-session", "assistant",
            "1. Function decorators\n2. Class decorators\n3. Property decorators",
        )

        seen_classify_messages = []

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                seen_classify_messages.append(kwargs["messages"][-1]["content"])
                return {"message": {"content": json.dumps({"intent": "GENERAL_CHAT", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "Class decorators wrap a class definition..."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "explain the second one", "session_id": "fixed-session"})
                assert r.status_code == 200
        assert any("Class decorators" in m for m in seen_classify_messages)
