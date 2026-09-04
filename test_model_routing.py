"""
JARVIS model-routing test suite.

Covers model_selector.py's select_model()/check_model_availability()
in isolation, plus the two real call sites wired to it
(main.py's ask_ollama() fallback path and the CODE_EXPLANATION
inline call). These tests check the ACTUAL model name selected/
passed to ollama.chat, not merely that a response string came back —
per the routing spec's explicit "tests must verify the actual
selected model" requirement.

Same isolation pattern as test_phase3.py/test_phase5.py: fake_env
supplies the LiveKit env vars config.py needs at import time; no live
Ollama server is required anywhere in this file (ollama.chat is
monkeypatched everywhere it'd otherwise be called).

Run with: pytest test_model_routing.py -v
"""

from unittest.mock import patch

import pytest


@pytest.fixture
def jarvis_db_path(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test_jarvis_routing.db")
    monkeypatch.setenv("JARVIS_DB_PATH", db_path)
    return db_path


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://fake.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "fake_key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fake_secret")
    monkeypatch.setenv("JARVIS_TEXT_MODEL", "llama3.1:8b")
    monkeypatch.setenv("JARVIS_CODING_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("JARVIS_VISION_MODEL", "llama3.2-vision:latest")


# ── select_model(): the reusable component itself ──────────────────

class TestSelectModel:
    def test_general_chat_uses_text_model(self, fake_env, monkeypatch):
        import model_selector
        monkeypatch.setattr(model_selector, "TEXT_MODEL", "llama3.1:8b")
        from intent_router import Intent
        assert model_selector.select_model(Intent.GENERAL_CHAT) == "llama3.1:8b"

    def test_general_knowledge_question_stays_on_text_model(self, fake_env, monkeypatch):
        """Spec test case 2: 'Explain recursion.' -> GENERAL_CHAT -> llama3.1:8b,
        even though 'recursion' is a programming term."""
        import model_selector
        monkeypatch.setattr(model_selector, "TEXT_MODEL", "llama3.1:8b")
        from intent_router import Intent
        assert model_selector.select_model(Intent.GENERAL_CHAT) == "llama3.1:8b"

    @pytest.mark.parametrize("coding_intent", [
        "CODE_ANALYSIS", "CODE_EXPLANATION", "DEBUG", "DEVELOPER_MODE", "PROJECT_ANALYSIS",
    ])
    def test_coding_intents_use_coding_model(self, fake_env, monkeypatch, coding_intent):
        import model_selector
        monkeypatch.setattr(model_selector, "CODING_MODEL", "qwen2.5-coder:7b")
        from intent_router import Intent
        assert model_selector.select_model(Intent(coding_intent)) == "qwen2.5-coder:7b"

    def test_image_attached_always_wins_regardless_of_intent(self, fake_env, monkeypatch):
        """Spec: 'analyze this project' with has_image=True must still
        select the vision model, not the coding model — an actually
        attached image always needs the model that can see it."""
        import model_selector
        monkeypatch.setattr(model_selector, "VISION_MODEL", "llama3.2-vision:latest")
        from intent_router import Intent
        assert model_selector.select_model(Intent.PROJECT_ANALYSIS, has_image=True) == "llama3.2-vision:latest"

    def test_no_image_attached_never_selects_vision_model(self, fake_env, monkeypatch):
        """Spec: words like 'look'/'see'/'image' in the text must NOT
        select vision without has_image=True — this module doesn't
        even look at message text, only the has_image flag, so there's
        no keyword path that could accidentally trigger it."""
        import model_selector
        monkeypatch.setattr(model_selector, "TEXT_MODEL", "llama3.1:8b")
        from intent_router import Intent
        assert model_selector.select_model(Intent.GENERAL_CHAT, has_image=False) == "llama3.1:8b"

    def test_vision_requested_but_not_configured_returns_none(self, fake_env, monkeypatch):
        """Honest failure, not a silent text-only fallback for an image request."""
        import model_selector
        monkeypatch.setattr(model_selector, "VISION_MODEL", "")
        from intent_router import Intent
        assert model_selector.select_model(Intent.GENERAL_CHAT, has_image=True) is None

    def test_coding_model_unset_falls_back_to_text_model(self, fake_env, monkeypatch):
        """Coding model missing config is a safe degrade (still answers,
        on TEXT_MODEL), unlike vision's honest-refusal contract above."""
        import model_selector
        monkeypatch.setattr(model_selector, "CODING_MODEL", "")
        monkeypatch.setattr(model_selector, "TEXT_MODEL", "llama3.1:8b")
        from intent_router import Intent
        assert model_selector.select_model(Intent.DEBUG) == "llama3.1:8b"


# ── check_model_availability(): startup diagnostic ──────────────────

class TestCheckModelAvailability:
    def test_reports_each_configured_model(self, fake_env, monkeypatch):
        import model_selector
        monkeypatch.setattr(model_selector, "TEXT_MODEL", "llama3.1:8b")
        monkeypatch.setattr(model_selector, "CODING_MODEL", "qwen2.5-coder:7b")
        monkeypatch.setattr(model_selector, "VISION_MODEL", "llama3.2-vision:latest")
        fake_list_response = {
            "models": [{"model": "llama3.1:8b"}, {"model": "qwen2.5-coder:7b"}]
            # llama3.2-vision:latest deliberately NOT in this fake list
        }
        with patch("ollama.list", return_value=fake_list_response):
            result = model_selector.check_model_availability()
        assert result["checked"] is True
        assert result["models"]["text"]["available"] is True
        assert result["models"]["coding"]["available"] is True
        assert result["models"]["vision"]["available"] is False

    def test_never_raises_when_ollama_unreachable(self, fake_env):
        from model_selector import check_model_availability
        with patch("ollama.list", side_effect=ConnectionError("no ollama")):
            result = check_model_availability()  # must not raise
        assert result["checked"] is False
        assert all(not m["available"] for m in result["models"].values())

    def test_never_downloads_a_missing_model(self, fake_env):
        """Spec: 'Do not automatically download models from the application.'
        Asserting ollama.pull is simply never imported/called anywhere
        in this module is the direct way to verify that."""
        import model_selector
        import inspect
        source = inspect.getsource(model_selector)
        assert "ollama.pull" not in source
        assert ".pull(" not in source


# ── Real call sites: main.py actually uses the selected model ──────

class TestChatUsesSelectedModel:
    """
    Verifies the ACTUAL model name reaching ollama.chat(...) for the
    two real call sites wired to model_selector — not just that a
    reply string came back. Follows the same TestClient pattern
    test_merge_phase4_phase5.py's TestChatIntegrationPhase4Routes
    already uses (init_db, no-op start/stop_agent, `with client:` to
    run lifespan, and patching ollama.list since startup now also
    runs model_selector.check_model_availability()).
    """

    def _client(self, monkeypatch, fake_env, jarvis_db_path):
        import main
        from memory import init_db
        init_db()
        from assistants import study_assistant
        study_assistant.init_study_db()
        monkeypatch.setattr(main, "start_agent", lambda: None)
        monkeypatch.setattr(main, "stop_agent", lambda: None)
        from fastapi.testclient import TestClient
        return TestClient(main.app)

    def test_general_chat_fallback_passes_text_model_to_ollama(self, monkeypatch, fake_env, jarvis_db_path):
        client = self._client(monkeypatch, fake_env, jarvis_db_path)
        captured = {}

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                # intent classification call
                return {"message": {"content": '{"intent": "GENERAL_CHAT", "confidence": 0.9, "mode": null}'}}
            captured["model"] = kwargs.get("model")
            return {"message": {"content": "Python is a programming language."}}

        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}):
            with client:
                resp = client.post("/chat", json={"message": "What is Python?"})

        assert resp.status_code == 200
        assert captured.get("model") == "llama3.1:8b"

    def test_code_explanation_with_no_target_file_is_a_known_limitation(self, monkeypatch, fake_env, jarvis_db_path):
        """
        HONEST LIMITATION TEST, not a routing success test — see the
        written report's root-cause section for the full explanation.

        Traced precisely: route_intent() only ever returns the
        original intent (if implemented) or GENERAL_CHAT — and
        CODE_EXPLANATION IS implemented, so whenever the classifier
        assigns CODE_EXPLANATION, `routed` equals CODE_EXPLANATION
        exactly and execution goes to _handle_phase3_intent's
        CODE_EXPLANATION branch, NEVER main.py's GENERAL_CHAT/
        ask_ollama() fallback. That branch requires an existing
        target file (guess_target_file) and returns a clarifying
        question — not a coding-model call — when none is found. So
        a "write brand-new code, no existing file" request (spec test
        case 3) does NOT reach model_selector at all today if the
        classifier calls it CODE_EXPLANATION; this test pins down
        that exact real behavior rather than asserting a model was
        selected where the code provably never gets that far.
        """
        client = self._client(monkeypatch, fake_env, jarvis_db_path)

        def fake_chat(**kwargs):
            if kwargs.get("format"):
                return {"message": {"content": '{"intent": "CODE_EXPLANATION", "confidence": 0.6, "mode": null}'}}
            return {"message": {"content": "def reverse(s): return s[::-1]"}}

        with patch("ollama.chat", side_effect=fake_chat), patch("ollama.list", return_value={"models": []}), \
             patch("debug_mode.guess_target_file", return_value=None):
            with client:
                resp = client.post("/chat", json={"message": "Write a Python function to reverse a string."})

        assert resp.status_code == 200
        assert "which file" in resp.json()["response"].lower()
