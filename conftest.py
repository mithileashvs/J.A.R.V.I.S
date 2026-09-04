"""
Repo-wide pytest configuration.

## Why this file exists

Every test module in this suite has its own `jarvis_db_path` fixture
that does `monkeypatch.setenv("JARVIS_DB_PATH", ...)`. That only works
the *first* time `config.py`/`memory.py`/`project_memory.py` are
imported in a given pytest process — those modules read the env var
once, at import time (`from config import DB_PATH`), and cache the
resulting path in their own module namespace. Once any test file has
triggered that first import, setting the env var again in a later
test has no effect: every subsequent test silently shares the same
real `jarvis_memory.db` file on disk, including across completely
unrelated test runs if that file is never deleted between `pytest`
invocations.

`test_phase6.py` and `test_phase6_remaining_work.py` already worked
around this locally with their own `isolate_shared_db` autouse
fixture, which patches the `DB_PATH` *attribute* already bound inside
`memory`/`project_memory` directly instead of relying on the env var.
This file makes that same fix apply to every other test module
(`test_phase3.py`, `test_phase4.py`, `test_phase5.py`,
`test_merge_phase4_phase5.py`) without editing each of them — pytest
resolves a fixture defined in a test module before one of the same
name in `conftest.py`, so `test_phase6.py`'s and
`test_phase6_remaining_work.py`'s own definitions continue to take
precedence there, unchanged.

## Verifying the fix

Before this file existed, running the full suite once against a fresh
checkout gave 299/300 (only the one documented flake); running it
again immediately afterward — without deleting `jarvis_memory.db` —
additionally failed `TestContextManager::
test_gather_includes_conversation_history`,
`TestContextManager::test_gather_includes_active_project_and_facts`,
`TestSessionContext::test_get_recent_context_reads_conversation_history`,
and `TestStudyAssistant::test_start_and_get_topic`, all for the same
underlying reason (a "most recent row" query picking up another test's
row). With this fixture in place, repeated back-to-back runs — with or
without a stray `jarvis_memory.db` on disk — are identical.
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_shared_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "conftest_isolated.db")
    monkeypatch.setenv("JARVIS_DB_PATH", db_path)

    import memory
    import project_memory
    monkeypatch.setattr(memory, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(project_memory, "DB_PATH", db_path, raising=False)
    memory.init_db()
    project_memory.init_project_memory_db()
