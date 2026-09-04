# Phase 3 — Final Summary

```
PHASE 3 IMPLEMENTED

✓ Contextual Coding Assistant
✓ Code Analysis
✓ Code Explanation Modes
✓ Terminal Awareness
✓ Terminal Error Extraction
✓ Debug Mode
✓ Multi-step Debug Investigation
✓ Permission-Protected Fixes
✓ Tests
```

All 114 tests in `test_phase3.py` pass (`pytest test_phase3.py -v`). No Phase 1/2
system was rewritten — Context Manager, Intent Router, Tool Registry, Permission
Manager, Project Detection, Project Memory, and Active Window Awareness were all
reused and extended, never duplicated.

This document reflects the state of the repo after two passes: an initial Phase 3
implementation, followed by a fix pass that closed every gap found during audit
(listed under "What was fixed in the second pass" below).

---

## What's implemented

### 1. Contextual Coding Assistant
`context_manager.py` aggregates conversation history, active window, active
project, and project memory into one bounded context bundle (`gather_context`
tool). Bounded by design — it never scans an entire project; `analyze_code`,
`read_relevant_file`, and `find_code_reference` are separate, deliberately
narrow tools that only touch what's actually relevant to the request.

### 2. Code Analysis (`code_analysis.py`)
Real static analysis for Python via `pyflakes` (unused imports, unused
variables, undefined names, syntax errors) with confidence levels
(Confirmed/High/Possible). Structural checks only for other languages — this
is stated honestly in the output rather than pretending to a depth of analysis
that isn't there. Structured output: SUMMARY / ISSUES FOUND / etc., matching
Section 3's format.

### 3. Code Explanation Modes (`code_analysis.py`)
All six modes implemented: BEGINNER, LINE_BY_LINE, TECHNICAL, INTERVIEW, EXAM,
ELI5. `explain_code` extracts the relevant unit (file/function/class) and
builds a mode-specific prompt; the LLM call itself happens one layer up in
`main.py`, matching how the rest of Phase 3 is structured (this module
extracts and frames, callers decide how to turn it into conversational
prose).

### 4. Explanation Mode Detection
Extends the existing Ollama-backed Intent Router (`intent_router.py`) — no
second routing system. Classifies `CODE_EXPLANATION` with a `mode` field
matching Section 5's example JSON shape.

### 5. Terminal Tool (`terminal_tools.py`)
`run_terminal_command` executes via `create_subprocess_exec` — **never**
`shell=True`. Per-command classification into SAFE / CONFIRM / DANGEROUS /
REJECTED via `classify_command()`, enforced through the existing
`permissions.py` confirmation flow (`tool_registry.py`'s `dynamic_classifier`
mechanism). Command-chaining/injection attempts (`;`, `&&`, `|`, `` ` ``,
`$()`, `>`) are hard-rejected regardless of the leading command name.
Captures stdout/stderr/exit code/execution time, with output truncation and a
timeout.

### 6. Terminal Error Extraction (`terminal_tools.extract_errors`)
Pattern-matches Python tracebacks, `ModuleNotFoundError`/`ImportError`,
`SyntaxError`, npm/Node errors, port conflicts (`EADDRINUSE`), and permission
errors into PRIMARY ERROR / LIKELY ROOT CAUSE / RELEVANT FILES. Falls back
honestly to "no recognized error pattern" rather than fabricating a root
cause when nothing matches a known pattern.

### 7. Debug Mode (`debug_mode.py`)
Bounded, observable, cancellable investigation (`Investigation` class):

```
1. Gather context
2. Identify target file
3. Check terminal output      <- reads terminal_tools' last-result cache
4. Analyze code
5. Check project memory
6. Form diagnosis
```

Enforces `max_steps` (default 8) and a wall-clock `timeout_seconds` (default
60s); supports `cancel()` for user-initiated cancellation, checked before
every step. Diagnosis output matches Section 11's exact format (DIAGNOSIS /
EVIDENCE / ROOT CAUSE / CONFIDENCE / SECONDARY ISSUES / RECOMMENDED FIX /
NEXT STEP), broadcasting real state transitions per step (not fake frontend
animation — see `_emit()` calling `state_manager.set_state()`).

**Evidence ranking**: a live terminal error (step 3) is treated as the
strongest available evidence when the most recent command actually failed —
more direct proof of "what's wrong right now" than a static-analysis
heuristic — but known-issue and static-analysis findings are folded in as
corroborating detail rather than discarded. A clean/successful terminal run
does not suppress a real static-analysis finding (tested explicitly).

### 8. File Modification Workflow (`file_ops.py`, Section 14)
`apply_fix` is registered `CONFIRM`, always — there is no code path that lets
it run as `SAFE`. Its `dynamic_classifier` builds a real unified diff
(`build_diff_preview`) and shows it in the confirmation prompt, so the user
sees exactly what will change before approving (this is what satisfies
"SHOW FILES THAT WILL CHANGE" / "SHOW SUMMARY OF CHANGES" from Section 14 —
those happen at confirmation time, before any write occurs). On approval:
backs up the original file (`<file>.jarvis-backup-<timestamp>`), writes the
new content, then **actually re-runs static analysis on the result** and
returns it as `verification` — not a rubber-stamp "write succeeded" message.

### 9. UI Integration
`state.py`'s `EXECUTING` state carries a `detail` string per debug-investigation
step, broadcast over the existing WebSocket — real backend state, not a fake
frontend animation (verified by `TestDebugInvestigationBounds::test_state_broadcasts_distinct_progress_per_step`,
which asserts 6 distinct broadcasts, one per real step).

### 10. Tool Registry Integration (Section 17)
Every tool listed in Section 17 is now real:

| Tool | Permission | Notes |
|---|---|---|
| `analyze_code` | SAFE | pyflakes-backed |
| `explain_code` | SAFE | builds explanation prompt |
| `run_terminal_command` | dynamic (SAFE→BLOCKED) | classified per-command |
| `read_terminal_output` | SAFE | reads last-result cache |
| `inspect_environment` | SAFE | fixed, hardcoded read-only commands only |
| `run_tests` | CONFIRM | picks pytest/npm test from project stack |
| `read_relevant_file` | SAFE | bounded, optional line range |
| `find_code_reference` | SAFE | bounded literal search |
| `apply_fix` | CONFIRM | always; diff shown at confirmation |
| `debug_investigation` | SAFE | orchestrates the above, itself read-only |
| `check_port_usage` | SAFE | bonus — named in Section 6 |

`git status`/`git diff`/`git log` are deliberately **not** separate tools —
they're already reachable, SAFE, through `run_terminal_command`'s own
classifier, so a dedicated tool would just duplicate that path.

---

## What was fixed in the second pass

An initial implementation existed but had real gaps versus the spec, found
during audit and closed in this pass:

1. **Debug Mode never actually checked the terminal.** `debug_mode.py`
   imported `terminal_tools` but never called into it. Fixed: added the
   "Check terminal output" step (#3 above) and re-prioritized
   `_form_diagnosis()` to treat a live, failing command's error as the
   strongest evidence when present.
2. **No way to apply a fix at all.** Section 14's workflow was half-built —
   JARVIS could propose fixes in prose but had no tool to apply one, even
   behind confirmation. Fixed: `file_ops.py` + the `apply_fix` tool.
3. **TERMINAL intent had no handler** and fell through to `GENERAL_CHAT`.
   Fixed: `terminal_tools.extract_command_from_message()` (backtick or
   "run X" extraction, returns `None` rather than guessing) wired into
   `main.py`'s `_handle_phase3_intent`, routed through the same
   `run_terminal_command` permission gate as everything else.
4. **Five Section 17 tools were missing entirely**: `read_relevant_file`,
   `find_code_reference`, `read_terminal_output`, `inspect_environment`,
   `run_tests`. All implemented and registered (table above).
5. Stale "planned but not implemented" stub entries in `tool_registry.py`
   (`git_status`, `git_diff`, `git_log`, `read_file`, `search_project`,
   `read_terminal`) were removed now that the real tools cover them, so
   `/tools` reports accurately instead of claiming things are missing that
   already work.

Two real bugs were caught by the new tests during this pass (not
pre-existing — introduced by the fixes above, caught before shipping):
- `run_tests` referenced an undefined name (`context_manager` instead of the
  module's `_ctx` alias) — would have raised on every call.
- The terminal-check step in Debug Mode was treating a *successful* command's
  plain-text output (e.g. `Python 3.12.3` from `python --version`) as an
  "error" — fixed by gating on actual failure (nonzero exit code or
  non-empty stderr) before calling `extract_errors()`, matching the gate
  `run_terminal_command`'s own handler already used.

A third, genuinely pre-existing bug was found by running the real server as
an actual live process (not the mocked/pre-initialized `TestClient` the
pytest suite uses) and hitting it with real HTTP requests: `main.py`'s
`lifespan()` only called `memory.init_db()`, never
`project_memory.init_project_memory_db()`. Every test in the suite calls the
latter explicitly in its own fixture, which is exactly what let this slip
past 113 passing tests — a genuinely fresh production database would 500 on
the very first `inspect_project`/`save_project_memory`/`debug_investigation`
call with `no such table: projects`. Fixed in `lifespan()`, and covered by a
new regression test (`TestStartupInitializesProjectMemory`) that deliberately
does *not* pre-initialize the table itself, so it only passes if the real
startup path does the initialization.

---

## Features that could not be implemented / were deliberately scoped out

- **Live/continuous terminal reading.** JARVIS can only see terminal output
  from commands it ran itself (cached via `terminal_tools.get_last_result()`).
  It cannot read an arbitrary external terminal window's live contents — that
  would require screen-scraping/OCR, which Section 15 explicitly prohibits
  ("do not continuously capture the screen"). This is a scope decision, not a
  bug: `debug_mode.py`'s docstring states this honestly rather than pretending
  otherwise.
- **Active window awareness is Windows-only** (`awareness.py` uses
  `pygetwindow`, which only works there). Documented, not silently broken on
  other platforms — `get_active_window` reports unavailability honestly.
- **`find_code_reference` is literal text search, not symbol resolution.**
  It finds every line containing the string, with no understanding of scope,
  imports, or shadowing — that would need a real per-language parser this
  project doesn't have. Framed honestly in the tool's docstring as "where else
  does this string appear," not "go to definition."
- **`apply_fix` only modifies existing files**, it doesn't create new ones —
  a materially larger permission surface deliberately left out of this phase.
- **`list_directory` remains unimplemented** — registered as a stub
  (`implemented=False`) so `/tools` reports it honestly rather than pretending
  it doesn't exist. `inspect_project` already provides a bounded top-level
  structure; a true on-demand directory listing tool didn't make this pass.

## Assumptions made

- Single JARVIS backend process, single user — the terminal last-result cache
  and `permission_manager`'s pending-confirmation store are both process-global
  state, matching every other piece of Phase 1/2 state in this codebase (no
  multi-tenancy exists anywhere else in the project either).
- "Recent" terminal output is defined as within the last 10 minutes
  (`terminal_tools._LAST_RESULT_MAX_AGE_SECONDS`) — old enough to survive a
  normal conversational pause, recent enough that a since-fixed error won't
  misdiagnose a current question.
- `run_tests` infers pytest vs. `npm test` from `project_memory`'s detected
  technologies; a project that isn't clearly Python or Node reports an honest
  error rather than guessing a command.

## Remaining known issues

- None discovered that affect correctness of what's implemented. Pre-existing,
  unrelated pyflakes warnings exist elsewhere in the codebase (a few
  `f-string is missing placeholders` and unused-`global` notices in
  `main.py`/`awareness.py`/`tool_registry.py` predate this phase and are
  cosmetic, not functional).
- `datetime.utcnow()` deprecation warnings appear throughout the existing
  codebase (Phase 1/2 code, not Phase 3) — cosmetic under Python 3.12, not
  fixed here since it's out of Phase 3's scope and touches files this phase
  was told not to rewrite.

---

## How to test each major feature manually

Prerequisites: `pip install -r requirements.txt -r requirements-dev.txt`
(Windows) or the equivalent on Linux/Mac; Ollama running locally with
`llama3.1:8b` pulled; then `python main.py` to start the backend.

**Code analysis** — `POST /tools/run {"tool": "analyze_code", "args": {"file_path": "<path to a .py file>"}}`.
Expect a structured SUMMARY/ISSUES FOUND response; try a file with an obvious
undefined name to see it ranked as the top issue.

**Code explanation modes** — `POST /tools/run {"tool": "explain_code", "args": {"file_path": "<path>", "mode": "ELI5"}}`.
Try each of the 6 modes and confirm the returned `prompt` field's framing
changes accordingly.

**Terminal tool + permissions** —
`POST /tools/run {"tool": "run_terminal_command", "args": {"command": "git status"}}`
should return `status: ok` immediately (SAFE). Then try
`{"command": "pip install requests"}` — expect `status: pending_confirmation`;
resolve via `POST /confirmations/{id} {"approved": true}` and confirm it then
runs. Try `{"command": "git status && rm -rf /"}` — expect `status: blocked`.

**Terminal error extraction** — run a command that actually fails, e.g.
`{"command": "python -c \"import totally_missing_module\""}`, then
`POST /tools/run {"tool": "read_terminal_output", "args": {}}` and confirm
`extracted_error.primary_error` mentions the missing module.

**Debug Mode end-to-end** — set an active project
(`POST /tools/run {"tool": "set_active_project", "args": {"path": "<project dir>"}}`),
then `POST /chat {"message": "why is <file> failing"}` on a file with a real
bug. Confirm the response contains DIAGNOSIS/EVIDENCE/ROOT CAUSE/CONFIDENCE
sections, and that `GET /state` returns to `IDLE` afterward.

**Terminal-integrated debugging** — run a failing command via
`run_terminal_command` first (e.g. the missing-module example above), then
immediately ask `"why isn't this working"` via `/chat` with DEBUG intent.
Confirm the diagnosis references the terminal error, not just static
analysis.

**TERMINAL intent via chat** — `POST /chat {"message": "run `git status`"}`
should return a COMMAND/EXIT CODE/OUTPUT formatted reply. Try
`{"message": "can you use the terminal"}` (no command given) — expect a
request for the exact command, not a guess.

**apply_fix** — `POST /tools/run {"tool": "apply_fix", "args": {"file_path": "<path>", "new_content": "<new file text>"}}`
should return `pending_confirmation` with a real diff in the message; approve
via `/confirmations/{id}`, then confirm the file changed and a
`<file>.jarvis-backup-<timestamp>` sibling file exists with the original
content.

**run_tests** — with an active Python project set,
`POST /tools/run {"tool": "run_tests", "args": {}}` should require
confirmation, then run `pytest` in that directory on approval.

**Automated regression** — `pytest test_phase3.py -v` from the project root
(114 tests) covers all of the above plus the failure scenarios from Section
19: no active project, no terminal available, no error detected, file
unavailable, permission denied, command timeout, malformed terminal output,
tool failure, and LLM/API failure never producing a 500.
