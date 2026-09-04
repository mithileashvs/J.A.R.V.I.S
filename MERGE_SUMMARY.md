# J.A.R.V.I.S — Phase 4 + Phase 5 Merge Report

## 1. MERGE SUMMARY

Both ZIPs turned out to share the exact same Phase 1–3 base. Diffing
the two file trees directly (not guessing from the architecture
brief) showed only **6 of ~35 files actually diverged**:

- `main.py`
- `intent_router.py`
- `debug_mode.py`
- `tool_registry.py`
- `requirements.txt`
- `test_phase3.py`

Everything else common to both ZIPs was byte-identical. Phase 4 added
three whole new files (`screen_tools.py`, `git_tools.py`,
`background_tasks.py`) plus `test_phase4.py`; Phase 5 added a new
`assistants/` package (CSE, developer, hackathon, study assistants), a
new `core/` package (confidence, LLM orchestrator, reference resolver,
session context, task planner), and `test_phase5.py`. Neither phase
touched the other's new files, so there was no file-level collision
there — only the 6 shared files above needed real merge work.

A key simplification, discovered by diffing rather than assumed:
**Phase 4's `tool_registry.py` and `debug_mode.py` are strict
supersets of Phase 5's versions of the same files.** Phase 5 was
built without ever pulling in Phase 4's screen/git/background-task
tool registrations or Phase 4's hypothesis-ranking upgrade to the
debug investigation — Phase 5's copies are just what Phase 3 already
had, unmodified. So those two files needed no line-level merging at
all: Phase 4's copies are used as-is and already contain everything
Phase 5 needs.

That left real merge work on exactly two files:

- **`intent_router.py`** — combine Phase 4's `_IMPLEMENTED_INTENTS`
  additions (`SCREEN_ANALYSIS`, `GIT`) with Phase 5's `Intent` enum
  additions (`HACKATHON`, `DEVELOPER_MODE`) and its
  `_IMPLEMENTED_INTENTS` additions (`STUDY`, `DSA`, `INTERVIEW`,
  `PLANNING`, `HACKATHON`, `DEVELOPER_MODE`).
- **`main.py`** — combine Phase 4's `SCREEN_ANALYSIS`/`GIT` chat
  branches, background-task broadcast wiring, and git imports with
  Phase 5's reference-resolution step, `study_assistant.init_study_db()`
  startup call, and its `STUDY`/`DSA`/`INTERVIEW`/`PLANNING`/
  `HACKATHON`/`DEVELOPER_MODE` chat branches.

`requirements.txt` and `test_phase3.py`: Phase 4's versions were
already supersets (Phase 4 added the OCR dependencies on top of
Phase 5's identical base; Phase 4's `test_phase3.py` only differed in
one comment/assertion updated for the hypothesis-ranking step count,
which the merge keeps since Phase 4's `debug_mode.py` is the one in
the merged tree).

## 2. CONFLICTS FOUND

### FILE: `intent_router.py`

```
Phase 4 behavior: _IMPLEMENTED_INTENTS = {GENERAL_CHAT, DEBUG,
    CODE_ANALYSIS, CODE_EXPLANATION, TERMINAL, SCREEN_ANALYSIS, GIT}
Phase 5 behavior: Intent enum adds HACKATHON, DEVELOPER_MODE.
    _IMPLEMENTED_INTENTS = {GENERAL_CHAT, DEBUG, CODE_ANALYSIS,
    CODE_EXPLANATION, TERMINAL, STUDY, DSA, INTERVIEW, PLANNING,
    HACKATHON, DEVELOPER_MODE}  — i.e. it silently DROPPED
    SCREEN_ANALYSIS and GIT back to falling through to GENERAL_CHAT.
Resolution: Union of both. Merged Intent enum has all members from
    both phases. Merged _IMPLEMENTED_INTENTS contains all 13
    implemented intents from both phases.
Reason: Phase 5 was developed from the pre-Phase-4 base and never
    saw Phase 4's SCREEN_ANALYSIS/GIT work, so its copy of this set
    is simply incomplete, not an intentional removal. A naive
    "take Phase 5's file" merge would have silently regressed two
    working Phase 4 features. Verified regression risk directly with
    test_merge_phase4_phase5.py::TestMergedIntentRouting.
```

### FILE: `main.py`

```
Phase 4 behavior: Imports git_tools; lifespan() wires
    background_tasks.task_manager's broadcast fn and shuts down
    background tasks on exit; _handle_phase3_intent() has branches
    for SCREEN_ANALYSIS (via tool_registry's CONFIRM-gated
    analyze_screen) and GIT (routes to git_tools' status/diff/log/
    branch/summary/commit-message/merge-conflict functions); /chat
    only classifies request.message directly (no reference
    resolution).
Phase 5 behavior: Imports core.session_context, core.llm_orchestrator,
    core.task_planner, core.reference_resolver, and the assistants
    package; lifespan() calls study_assistant.init_study_db();
    _handle_phase3_intent() has branches for DSA/STUDY/INTERVIEW
    (cse_assistant/study_assistant + llm_orchestrator),  PLANNING
    (task_planner), HACKATHON (hackathon_assistant), and
    DEVELOPER_MODE (developer_assistant, reusing debug_mode.py's
    Investigation exactly as DEBUG does); /chat resolves contextual
    references ("open it", "the second one") against history before
    classification. Phase 5's copy of main.py has NO git_tools
    import and NO SCREEN_ANALYSIS/GIT branches — built from the
    pre-Phase-4 base, same root cause as the intent_router conflict.
Resolution: Combined main.py contains everything from both: all
    imports from both phases; lifespan() does both the background-
    task broadcast wiring AND study_assistant.init_study_db(); the
    intent handler function has all nine specialized branches
    (DEBUG, CODE_ANALYSIS, CODE_EXPLANATION, TERMINAL,
    SCREEN_ANALYSIS, GIT, DSA/STUDY/INTERVIEW, PLANNING, HACKATHON,
    DEVELOPER_MODE); /chat does reference resolution before
    classification (Phase 5's addition) and then routes through the
    full merged _HANDLED_INTENTS tuple (both phases' entries).
Reason: Same root cause as the intent_router.py conflict — Phase 5
    was never rebased onto Phase 4's SCREEN_ANALYSIS/GIT work.
    Verified with test_merge_phase4_phase5.py::TestChatIntegrationPhase4Routes
    (confirms GIT and SCREEN_ANALYSIS still reach their Phase 4
    handlers post-merge) and ::TestCrossPhaseConversation (confirms
    a DEBUG turn — Phase 3/4 — and a STUDY turn — Phase 5 — in the
    same session don't corrupt each other's state).
```

### FILE: `tool_registry.py`

```
Phase 4 behavior: Registers 39 tools total — Phase 1–3's tools plus
    Phase 4's analyze_screen / capture_active_window /
    extract_screen_text (screen), start_background_task /
    get_background_task_status / monitor_background_task /
    stop_background_task (background monitoring), and git_status /
    git_diff / git_log / git_branch / generate_commit_summary /
    generate_commit_message / analyze_merge_conflict (git).
Phase 5 behavior: Identical to Phase 1–3's tool_registry.py — no new
    tools registered, and missing all of Phase 4's above.
Resolution: Use Phase 4's file verbatim. It is a strict superset;
    nothing in Phase 5 needed a tool that Phase 4's version lacks.
Reason: Diffed byte-for-byte — every line Phase 5 has, Phase 4 also
    has; Phase 4 just has more. No merge logic needed, only a
    "prefer the superset" decision, confirmed by diff before
    committing to it.
```

### FILE: `debug_mode.py`

```
Phase 4 behavior: MAX_STEPS=10 (was 8, raised for the two new
    conditional steps below); adds Hypothesis/HypothesisStatus
    types, repeated-action detection (_mark_action), an optional
    screen_context parameter to Investigation.run() (never captures
    itself — only incorporates evidence a caller already captured
    through the confirmation gate), conditional environment-check
    and port-check steps, hypothesis ranking
    (_build_hypotheses/_form_diagnosis rewritten around ranked
    Hypothesis objects), and to_incomplete_text() for the
    "INVESTIGATION INCOMPLETE" report on a cancelled/budget-exhausted
    run.
Phase 5 behavior: Identical to Phase 1–3's debug_mode.py — MAX_STEPS=8,
    no Hypothesis system, no environment/port checks, no
    to_incomplete_text(), 6-step investigation instead of 7.
Resolution: Use Phase 4's file verbatim, same reasoning as
    tool_registry.py — confirmed as a strict superset by diff.
    developer_assistant.py's format_diagnosis_as_developer_report()
    (Phase 5) duck-types against the Diagnosis object via getattr()
    rather than importing debug_mode at module load time, so it
    works unmodified against Phase 4's richer Diagnosis without any
    changes on either side.
Reason: Confirmed via diff, and confirmed working via
    test_merge_phase4_phase5.py::TestCrossPhaseConversation's DEBUG
    turn plus test_phase5.py's own
    test_developer_mode_debug_this_runs_real_investigation, which
    exercises DEVELOPER_MODE calling into this exact
    Investigation/Diagnosis pipeline.
```

### FILE: `requirements.txt`

```
Phase 4 behavior: Adds mss, pytesseract, pillow (screen OCR) on top
    of the shared base.
Phase 5 behavior: Shared base only, no OCR deps.
Resolution: Use Phase 4's file (superset).
Reason: Confirmed via diff — Phase 5's file is a strict subset (same
    packages minus the three OCR-only ones), no version conflicts to
    resolve.
```

### FILE: `test_phase3.py`

```
Phase 4 behavior: Debug-investigation step-count assertion expects 7
    steps (matches the Hypothesis-ranking rewrite's extra "rank
    hypotheses" step).
Phase 5 behavior: Expects 6 steps (matches the un-modified Phase 3
    debug_mode.py it was tested against).
Resolution: Use Phase 4's file/assertion (7 steps), since the merged
    tree uses Phase 4's debug_mode.py.
Reason: The assertion must match the actual debug_mode.py in the
    merged tree, which is Phase 4's. Confirmed passing after the
    resolution: test_phase3.py's full suite (114 tests) passes
    against the merged debug_mode.py with no further edits needed.
```

## 3. FILES CREATED

- `assistants/__init__.py`, `assistants/cse_assistant.py`,
  `assistants/developer_assistant.py`, `assistants/hackathon_assistant.py`,
  `assistants/study_assistant.py` — copied from Phase 5 unmodified.
- `core/__init__.py`, `core/confidence.py`, `core/llm_orchestrator.py`,
  `core/reference_resolver.py`, `core/session_context.py`,
  `core/task_planner.py` — copied from Phase 5 unmodified.
- `test_phase5.py` — copied from Phase 5 unmodified.
- `test_merge_phase4_phase5.py` — **new**, written for this merge.
  Covers exactly the seams a bad merge would break: the combined
  Intent enum/`_IMPLEMENTED_INTENTS` set, the combined tool registry,
  GIT/SCREEN_ANALYSIS still reachable through `/chat` after adding
  Phase 5's routing, and a single conversation crossing from a Phase 4
  capability (DEBUG) to a Phase 5 capability (STUDY) without state
  leaking between them.

## 4. FILES MODIFIED

- `intent_router.py` — merged `Intent` enum and `_IMPLEMENTED_INTENTS`
  (see Conflicts above); merged the classifier's prose guidelines to
  describe `PLANNING`/`HACKATHON`/`DEVELOPER_MODE` (Phase 5's wording)
  while keeping Phase 4's `SCREEN_ANALYSIS`/`GIT` implementation-notes
  comment.
- `main.py` — merged imports, `lifespan()`, the intent-handling
  function, and the `/chat` endpoint (see Conflicts above).

## 5. FILES REMOVED

None. Every file from both ZIPs is represented in the merged tree —
either verbatim (where one phase was already a superset, or where a
file was untouched by the other phase) or hand-merged (`main.py`,
`intent_router.py`). No duplicate core systems were created that
needed removing: `tool_registry.py`, `debug_mode.py`, the state
machine, context manager, permission manager, project
detector/memory, STT/TTS modules, and Ollama interface all have
exactly one implementation each in the merged tree, same as before
the merge.

Generated/runtime artifacts excluded from the merged tree and the
final ZIP: `jarvis_memory.db` (the copy shipped in the Phase 4 ZIP
was a stale runtime file, not source — a fresh one is created on
first run via `init_db()`/`init_project_memory_db()`/
`study_assistant.init_study_db()`), `__pycache__/`, `.pytest_cache/`.

## 6. FINAL ARCHITECTURE

Request flow through the merged backend, following the conceptual
diagram in the brief but matching what's actually implemented:

```
USER (voice via agent.py+LiveKit, or /chat REST/WS)
  → STT (whisper_stt.py) / direct text
  → main.py POST /chat
      → reference_resolver.resolve() (Phase 5) de-references
        "open it"/"the second one" against recent history
      → intent_router.classify() (Ollama structured JSON) + route_intent()
      → merged _HANDLED_INTENTS dispatch in _handle_phase3_intent():
          DEBUG / DEVELOPER_MODE  → debug_mode.Investigation
              (hypothesis ranking, step/tool/time limits, cancellation,
              loop/repeated-action detection — Phase 4)
              → optionally incorporates screen_tools.ScreenContext
                (only if the caller already went through the
                analyze_screen CONFIRM gate)
          CODE_ANALYSIS / CODE_EXPLANATION → code_analysis.py
          TERMINAL      → terminal_tools.py via tool_registry (SAFE/
                           CONFIRM/BLOCKED classification)
          SCREEN_ANALYSIS → screen_tools.py via tool_registry
                           (always CONFIRM)
          GIT           → git_tools.py (all SAFE, read-only)
          STUDY/DSA/INTERVIEW → cse_assistant.py / study_assistant.py
                           → core.llm_orchestrator + core.session_context
          PLANNING      → core.task_planner
          HACKATHON     → assistants.hackathon_assistant
                           → core.llm_orchestrator
      → all tool-touching paths go through tool_registry.py's single
        registry → permissions.py's SAFE/CONFIRM/BLOCKED gate; the
        LLM (Ollama) never calls a tool directly, it only produces
        text/structure that main.py maps onto a registered tool call
      → state.py's JarvisState machine broadcasts
        LISTENING/THINKING/EXECUTING/SPEAKING/ERROR over the
        WebSocket connection to the frontend and TTS (piper_tts.py)
      → background_tasks.py independently monitors any
        explicitly-started long-running command (build/dev server)
        and broadcasts failure/success notifications through the
        same WebSocket, deduplicated
```

No duplicate context managers, state machines, tool registries, or
debug systems exist in the merged tree — Phase 5's "Context Engine"
concept from the brief is realized as `core/session_context.py`
layered on top of the existing `context_manager.py`
(`context_manager.gather()` supplies project/file/terminal/debug
context; `session_context.py` adds conversation/mode/task context on
top, and `context_to_prompt_block()` is what Phase 5's
LLM-orchestrated intents actually consume) rather than as a
second, competing context system.

## 7. TEST RESULTS

Run (standard order — see Section 10, "How To Run"):

```
test_phase3.py             114 passed
test_phase4.py               73 passed
test_phase5.py               66 passed
test_merge_phase4_phase5.py   8 passed
──────────────────────────────────────
Total tests:   261
Passed:        261
Failed:          0
Skipped:         0
Warnings:       358  (all datetime.utcnow() deprecation warnings —
                       pre-existing in both original phases, not
                       introduced by this merge; see Known
                       Limitations)
```

Verified with `pyflakes` across every merged/hand-edited file
(`main.py`, `intent_router.py`, `debug_mode.py`, `tool_registry.py`,
`git_tools.py`, `screen_tools.py`, `background_tasks.py`, all of
`assistants/` and `core/`): the only warnings present are pre-existing
ones diffed and confirmed identical against the original Phase 4 ZIP
(unused `create_session` import, two unused `global` declarations,
two f-strings without placeholders, one unused local variable, two
unused `dataclasses.field` imports) — the merge introduced zero new
lint issues.

## 8. FEATURES VERIFIED

### ACTUALLY TESTED

- State transitions, intent routing, context gathering, project
  detection, project memory, tool registry, permission checks (114
  tests, `test_phase3.py`)
- Targeted screen capture, screen privacy/confirmation gating, OCR
  error extraction, hypothesis-based debug investigation, step/tool/
  time limits, cancellation, loop/repeated-action detection,
  background task lifecycle + failure detection + notification
  dedup, git status/diff/log/branch, merge-conflict analysis, git
  permission levels (73 tests, `test_phase4.py`)
- Conversation memory/reference resolution, confidence handling, LLM
  orchestration, task planning, CSE assistant, study assistant,
  hackathon assistant, developer-mode toggle (66 tests,
  `test_phase5.py`)
- The merge seams specifically: merged Intent enum/implemented-set
  completeness, merged tool registry completeness, GIT and
  SCREEN_ANALYSIS still reachable through `/chat` post-merge, a
  single session moving from a DEBUG (Phase 3/4) turn to a STUDY
  (Phase 5) turn without state corruption (8 tests,
  `test_merge_phase4_phase5.py`)
- End-to-end backend smoke test: real `uvicorn` process, hit `/`,
  `/status`, `/tools` (confirmed all 39 tools registered), `/state`,
  and `POST /chat`; confirmed clean, non-crashing startup and
  shutdown with Ollama unavailable and the optional voice-agent
  plugin dependency missing (both handled as graceful degradation,
  not a crash)

### IMPLEMENTED BUT NOT FULLY MANUALLY TESTED

- The actual LiveKit voice pipeline end-to-end (wake word → STT →
  intent → TTS) — this sandbox has no audio device, no LiveKit
  server, and no `livekit.plugins` package installed, so this could
  only be verified by code review + the fact that `agent.py`/
  `whisper_stt.py`/`piper_tts.py` are untouched by the merge (byte-
  identical to both source ZIPs).
- Actual Ollama-backed responses (all tests and the smoke test mock
  or gracefully degrade around Ollama, since no Ollama server is
  running in this environment).
- The `startup.bat` Windows launch path (untouched by the merge,
  identical between both ZIPs; not runnable in this Linux sandbox).

## 9. KNOWN LIMITATIONS

- **Pre-existing test-isolation gap, not introduced by this merge:**
  `config.py` resolves `DB_PATH = os.getenv("JARVIS_DB_PATH", ...)` at
  *module import time*, and `memory.py`/`project_memory.py` both do
  `from config import DB_PATH` — a plain name binding, not a live
  lookup. The `jarvis_db_path` pytest fixture (present in all three
  original test files, unmodified here) monkeypatches the env var,
  which works correctly as long as `config`/`memory` haven't already
  been imported earlier in the same pytest process. Run in the
  documented order (`test_phase3.py` → `test_phase4.py` →
  `test_phase5.py` → `test_merge_phase4_phase5.py`) all 261 tests
  pass; running the files in a different order can make one specific
  pre-existing test (`test_phase5.py::test_study_intent_teaches_topic`)
  flaky, because it does an unfiltered `SELECT ... FROM study_topics`
  that can pick up a row written by an earlier test file sharing the
  same underlying DB path. This was verified to be a latent issue in
  the original Phase 5 test file itself (not something the merge
  changed), and confirmed harmless in the documented run order — but
  is worth fixing properly in `config.py` (e.g. a function instead of
  a module constant) in a follow-up, not attempted here per the "do
  not rewrite working features unnecessarily" instruction.
- `datetime.datetime.utcnow()` is used throughout `memory.py`,
  `project_memory.py`, `state.py`, `file_ops.py`, and `main.py` — all
  pre-existing (identical in both source ZIPs), all raising Python
  3.12 deprecation warnings. Not touched by this merge; a real fix
  (`datetime.now(datetime.UTC)`) would need to run against both
  phases' pre-existing behavior, out of scope for a merge task.
- Voice pipeline, real Ollama inference, and Windows startup are
  unverified in this sandbox for the environmental reasons listed in
  Section 8.
- `background/backend/KMS/` — two essentially empty directories
  present in the original Phase 4 ZIP (`backend/`, `KMS/`) carried no
  files and are not referenced anywhere in the codebase; left as-is
  rather than guessing at their intended purpose.

## 10. HOW TO RUN

```bash
# 1. Activate a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install / update dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt   # only needed to run the test suite

# 3. Configure environment
#    Edit .env with real LIVEKIT_URL / LIVEKIT_API_KEY /
#    LIVEKIT_API_SECRET (required — config.py's validate() raises
#    without them) and any other values you use.

# 4. Run JARVIS
python main.py
#    (or: uvicorn main:app --host 0.0.0.0 --port 8000)

# 5. Run the automated test suite (standard order — see Known
#    Limitations for why order matters when running all files
#    together)
pytest test_phase3.py test_phase4.py test_phase5.py test_merge_phase4_phase5.py -v
```

## 11. FINAL PROJECT

Packaged as `JARVIS_PHASE4_PHASE5_MERGED.zip`, excluding
`.venv`/`venv`, `__pycache__`, `.pytest_cache`, and the runtime
`jarvis_memory.db` (created automatically on first run).
