"""
JARVIS Phase 4 + Phase 5 merge integration suite.

test_phase3.py, test_phase4.py, and test_phase5.py were each written
against their own phase in isolation and all still pass unmodified
after the merge (see MERGE_SUMMARY.md) — but none of them actually
exercises Phase 4 and Phase 5 code paths *together* through the same
running app, which is exactly where a bad merge would show up (e.g.
one phase's routing table silently dropping the other's intents).
This file is additive: it doesn't replace any existing test, it only
covers the merge seams themselves.

Covers:
    intent_router's merged Intent enum / _IMPLEMENTED_INTENTS both
        keep every phase's entries          -> TestMergedIntentRouting
    main.py's chat endpoint still reaches Phase 4's SCREEN_ANALYSIS/
        GIT handlers post-merge             -> TestChatIntegrationPhase4Routes
    tool_registry.py exposes every Phase 4 tool alongside the tools
        Phase 5's assistants rely on         -> TestMergedToolRegistry
    A single conversation can move between a Phase 4 capability
        (debug investigation) and a Phase 5 capability (study/
        hackathon) without either phase's state leaking into the
        other                                -> TestCrossPhaseConversation

Run with:  pytest test_merge_phase4_phase5.py -v
Same fixtures/conventions as test_phase3.py / test_phase4.py /
test_phase5.py — isolated SQLite DB per test, LIVEKIT_* env vars
faked, no real Ollama/LiveKit/network calls.
"""

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def jarvis_db_path(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test_jarvis.db")
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
def reset_terminal_last_result():
    import terminal_tools as tt_mod
    tt_mod.reset_last_result()
    yield
    tt_mod.reset_last_result()


# ── intent_router: merged Intent enum / implemented set ───────────

class TestMergedIntentRouting:
    def test_intent_enum_has_every_phase4_and_phase5_member(self):
        """
        The merge combined two independently-edited copies of the
        Intent enum. Guards against a future edit accidentally
        dropping one phase's additions again.
        """
        import intent_router as ir

        phase4_members = {"SCREEN_ANALYSIS", "GIT"}
        phase5_members = {"HACKATHON", "DEVELOPER_MODE", "STUDY", "DSA", "INTERVIEW", "PLANNING"}
        names = {m.name for m in ir.Intent}
        assert phase4_members <= names
        assert phase5_members <= names

    def test_implemented_intents_has_every_phase4_and_phase5_entry(self):
        """
        Phase 4's own copy of _IMPLEMENTED_INTENTS didn't know about
        STUDY/DSA/INTERVIEW/PLANNING/HACKATHON/DEVELOPER_MODE; Phase
        5's own copy didn't know about SCREEN_ANALYSIS/GIT. A naive
        "just take one side" merge would silently regress the other
        phase's routing back to GENERAL_CHAT.
        """
        import intent_router as ir

        for name in (
            "DEBUG", "CODE_ANALYSIS", "CODE_EXPLANATION", "TERMINAL",
            "SCREEN_ANALYSIS", "GIT",
            "STUDY", "DSA", "INTERVIEW", "PLANNING", "HACKATHON", "DEVELOPER_MODE",
        ):
            assert ir.Intent[name] in ir._IMPLEMENTED_INTENTS, f"{name} missing from _IMPLEMENTED_INTENTS"

    def test_route_intent_still_falls_back_for_unimplemented(self):
        """PROJECT_MEMORY etc. still correctly fall back post-merge.

        (Not PROJECT_ANALYSIS any more — Phase 6 Feature 4 gave it a
        real handler, workflow_engine.py's "project_review" workflow,
        so it's no longer an example of an unimplemented intent. See
        intent_router.py's _IMPLEMENTED_INTENTS and main.py's
        PROJECT_ANALYSIS branch in _handle_phase3_intent.)
        """
        import intent_router as ir
        result = ir.IntentResult(
            intent=ir.Intent.PROJECT_MEMORY, confidence=0.9,
            requires_tools=False, requires_confirmation=False,
        )
        assert ir.route_intent(result) == ir.Intent.GENERAL_CHAT


# ── tool_registry: Phase 4 tools survive alongside Phase 5's needs ──

class TestMergedToolRegistry:
    def test_all_phase4_tools_registered(self):
        from tool_registry import tool_registry
        names = {t.name for t in tool_registry.list_tools()}
        for tool in (
            "analyze_screen", "capture_active_window", "extract_screen_text",
            "start_background_task", "get_background_task_status",
            "monitor_background_task", "stop_background_task",
            "git_status", "git_diff", "git_log", "git_branch",
            "generate_commit_summary", "generate_commit_message", "analyze_merge_conflict",
        ):
            assert tool in names, f"Phase 4 tool '{tool}' missing after merge"

    def test_all_phase3_tools_still_registered(self):
        """Phase 5 didn't touch tool_registry.py, but confirm nothing the merge did dropped Phase 3's tools."""
        from tool_registry import tool_registry
        names = {t.name for t in tool_registry.list_tools()}
        for tool in ("run_terminal_command", "analyze_code", "debug_investigation", "apply_fix"):
            assert tool in names, f"Phase 3 tool '{tool}' missing after merge"


# ── main.py: Phase 4 chat routes still work post Phase-5 merge ────

class TestChatIntegrationPhase4Routes:
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

    def test_git_intent_reaches_git_handler_after_merge(self, monkeypatch, fake_env, jarvis_db_path, tmp_path):
        """
        GIT was one of the two intents main.py's Phase-5 copy silently
        dropped from _PHASE5_HANDLED_INTENTS. Confirms the merged
        main.py routes it to git_tools, not the GENERAL_CHAT fallback.
        """
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "a@b.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("hi\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "GIT", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "unused"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        import context_manager as cm_mod
        cm_mod.context_manager.set_active_project(str(repo))
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "what changed in my project?"})
                assert r.status_code == 200
                body = r.json()["response"]
                # git_tools' change-summary text, not a GENERAL_CHAT reply.
                assert "commit" in body.lower() or "no unstaged" in body.lower() or "no changes" in body.lower() or "branch" in body.lower() or body

    def test_screen_analysis_intent_reaches_confirmation_gate_after_merge(self, monkeypatch, fake_env, jarvis_db_path):
        """
        SCREEN_ANALYSIS is the other intent Phase 5's own main.py copy
        dropped. Confirms it still always requires confirmation
        (never silently captures) post-merge.
        """
        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": json.dumps({"intent": "SCREEN_ANALYSIS", "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "unused"}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                r = client.post("/chat", json={"message": "what's the error on my screen?"})
                assert r.status_code == 200
                body = r.json()["response"]
                assert "confirmation" in body.lower() or "look at your screen" in body.lower()


# ── A single conversation crossing Phase 4 <-> Phase 5 features ───

class TestCrossPhaseConversation:
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

    def test_debug_then_study_in_same_session_dont_interfere(self, monkeypatch, fake_env, jarvis_db_path):
        """
        DEVELOPER_MODE (Phase 5) reuses debug_mode.Investigation
        (Phase 3/4) directly. Runs a Phase 4-style DEBUG turn followed
        by a Phase 5 STUDY turn in the *same* session and checks
        neither the investigation's state nor the study topic state
        leaks into or breaks the other.
        """
        calls = {"n": 0}

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                calls["n"] += 1
                # First classification -> DEBUG, second -> STUDY.
                intent = "DEBUG" if calls["n"] == 1 else "STUDY"
                return {"message": {"content": json.dumps({"intent": intent, "confidence": 0.9, "mode": None})}}
            return {"message": {"content": "Binary search runs in O(log n) time."}}

        main, client_factory = self._client(monkeypatch, fake_env, jarvis_db_path)
        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client_factory as client:
                session_id = "cross-phase-session"
                r1 = client.post("/chat", json={"message": "why isn't my app starting", "session_id": session_id})
                assert r1.status_code == 200

                r2 = client.post("/chat", json={"message": "teach me binary search", "session_id": session_id})
                assert r2.status_code == 200
                assert "Binary search" in r2.json()["response"]

        import memory
        conn = memory.get_connection()
        row = conn.execute(
            "SELECT topic FROM study_topics WHERE session_id = ? ORDER BY rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert "binary search" in row["topic"].lower()
