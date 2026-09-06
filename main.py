import os
import sys
import re
import time
import base64
import asyncio
import subprocess
import tempfile
import threading
import uuid
import pytz
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import ollama

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

from config import (
    BACKEND_PORT,
    LIVEKIT_URL,
    LIVEKIT_API_KEY,
    LIVEKIT_API_SECRET,
    TEXT_MODEL,
    VISION_MODEL,
)
from memory import (
    init_db,
    save_message,
    get_history,
    get_all_history,
    get_stats,
    log_event,
    get_recent_events,
    clear_events,
    AuditLogError,
    list_sessions,
    clear_session_history,
)
from state import state_manager, JarvisState
from permissions import permission_manager
from tool_registry import tool_registry
from intent_router import classify as classify_intent, route_intent, Intent
import model_selector
from context_manager import context_manager
import debug_mode as debug_mode_module
import code_analysis
import terminal_tools
import git_tools
import system_health
import proactive_engine
import context_engine

# ── Phase 5: intelligence layer, CSE/study/hackathon assistants ───
from core import session_context
from core import llm_orchestrator
from core import task_planner
from core.reference_resolver import resolve as resolve_reference
from assistants import cse_assistant, study_assistant, hackathon_assistant, developer_assistant

# ── Phase 6: workflow engine + concrete workflow kinds ─────────────
# Importing `workflows` registers every WorkflowKindSpec (project_review,
# ...) onto the shared workflow_engine singleton — same pattern as
# tool_registry.py building its default tool registry at import time.
from workflow_engine import workflow_engine, WorkflowStatus, is_active as _workflow_is_active
import workflows  # noqa: F401 — registers workflow kinds as a side effect
from project_health import project_health_monitor
from suggestion_engine import suggestion_engine

# ── Ollama Setup ───────────────────────────────────────────
# Section 8: no more hardcoded model name — TEXT_MODEL/VISION_MODEL
# come from config.py (JARVIS_TEXT_MODEL / JARVIS_VISION_MODEL env
# vars). OLLAMA_MODEL is kept as an alias so the handful of other
# modules/tests that still reference main.OLLAMA_MODEL keep working.
OLLAMA_MODEL = TEXT_MODEL


# ── Attachment Cache (Section 3 + 10 + 14) ─────────────────
# In-memory, per-session cache of the most recently uploaded
# image/file for THIS process's lifetime. This is what lets a
# follow-up like "what's wrong with it?" or "find the problem" (no new
# upload attached) resolve against the screenshot/code the user sent a
# turn or two ago, and what lets a code file + an error screenshot
# uploaded in separate messages be combined for Section 14's combined
# debugging. Deliberately NOT persisted to jarvis_memory.db — raw image
# bytes don't belong in a text-message metadata column, and this is
# exactly the kind of short-lived working context Section 2 describes
# ("do not blindly send everything forever"). Entries expire after
# ATTACHMENT_TTL_SECONDS so a reference days later doesn't silently
# reuse a stale image.
ATTACHMENT_TTL_SECONDS = 30 * 60  # 30 minutes
_session_attachments: dict[str, dict] = {}
_attachments_lock = threading.Lock()


def _remember_attachment(session_id: str, kind: str, filename: str, **fields) -> None:
    """kind is 'image' or 'document'. fields holds kind-specific data
    (image: base64_data, media_type; document: text, analysis_text)."""
    with _attachments_lock:
        bucket = _session_attachments.setdefault(session_id, {})
        bucket[kind] = {
            "filename":  filename,
            "stored_at": time.time(),
            **fields,
        }


def _recall_attachment(session_id: str, kind: str) -> Optional[dict]:
    with _attachments_lock:
        bucket = _session_attachments.get(session_id) or {}
        entry = bucket.get(kind)
    if not entry:
        return None
    if time.time() - entry["stored_at"] > ATTACHMENT_TTL_SECONDS:
        return None
    return entry


# Heuristic used only to decide whether a plain /chat message (no new
# upload) should be answered using a previously-uploaded image/document
# still cached for this session — e.g. "what's wrong with it?",
# "explain this screenshot", "summarize the document". This is on top
# of (not a replacement for) core/reference_resolver.py's existing
# this/that/it handling, which resolves references against *text*
# history; images/files need their own path since the referent isn't a
# chat message at all.
_VISUAL_REFERENCE_WORDS = (
    "image", "screenshot", "picture", "photo", "diagram", "screen",
    "ui", "this", "that", "it", "the pic", "above",
)
_DOCUMENT_REFERENCE_WORDS = (
    "document", "doc", "file", "pdf", "this", "that", "it", "the text",
    "uploaded", "attached", "above",
)


def _mentions_any(message: str, words: tuple) -> bool:
    lower = f" {message.lower()} "
    return any(f" {w} " in lower or lower.strip().startswith(w) for w in words)

# ── Agent Process Manager ──────────────────────────────────
agent_process: Optional[subprocess.Popen] = None
agent_status: dict = {
    "running":    False,
    "pid":        None,
    "started_at": None,
    "restarts":   0,
}


def _pump_agent_output(proc: subprocess.Popen):
    """
    Continuously drain agent.py's stdout/stderr into this process's own
    console.

    ROOT CAUSE OF "JARVIS DOES NOT RESPOND":
    start_agent() launches agent.py with stdout=subprocess.PIPE (and
    stderr redirected into that same pipe). A pipe has a small, fixed-size
    OS buffer (~64KB on Windows). If nothing on the parent side ever reads
    from that pipe, the buffer fills up — and once it's full, the CHILD
    PROCESS'S calls to print()/logging BLOCK inside the OS write() call
    until someone reads. Nobody was reading it here.

    agent.py prints a diagnostic line on essentially every step of the
    voice pipeline ("[VOICE] Listening...", "[STT] Recognized: ...",
    "[COMMAND] Received: ...", "[TTS] Speaking...", etc.), on top of
    LiveKit's own fairly chatty internal logging. That's enough output to
    fill a 64KB pipe within the first exchange or two. The moment it
    fills, agent.py silently freezes mid-print — no traceback, no crash,
    no log_event() gets written (because the frozen code is upstream of
    any error handling), and playback of course does not happen because
    the process running STT → LLM → TTS is deadlocked. This exactly
    matches "I speak, I hear myself, then nothing happens": the browser
    side (mic capture, WebRTC connection) keeps working fine because it's
    unrelated to this deadlock, while the local agent.py worker silently
    hangs.

    Fix: run a daemon thread per agent process that continuously reads
    and forwards its output, so the pipe never backs up.
    """
    try:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            print(f"[AGENT] {line.rstrip()}")
    except Exception as e:
        print(f"[JARVIS] Agent output reader stopped: {e}")


def start_agent():
    global agent_process

    root_dir   = os.path.dirname(os.path.abspath(__file__))
    agent_path = os.path.join(root_dir, "agent.py")

    if not os.path.exists(agent_path):
        log_event("error", f"agent.py not found at {agent_path}")
        return

    # Guard against a second agent process being spawned while one is
    # already alive. Two "Jarvis" agents registering with LiveKit at once
    # both try to answer the same room — this is the "multiple competing
    # microphone listeners" failure mode that causes unreliable/garbled
    # recognition. This can happen if agent.py was already launched
    # separately (e.g. via startup.bat) before the backend starts.
    if agent_process is not None and agent_process.poll() is None:
        print(
            f"[JARVIS] Agent already running (PID {agent_process.pid}) — "
            f"skipping duplicate start."
        )
        log_event("agent_start_skipped", "start_agent() called while an agent was already running")
        return

    try:
        # FIX: force the child interpreter into unbuffered mode.
        #
        # The pipe-drain thread (above) stops the child from *deadlocking*,
        # but on Windows, Python defaults to BLOCK buffering (not line
        # buffering) whenever stdout is not an interactive console — which
        # a piped subprocess never is. That means agent.py's print()
        # statements were sitting in the CHILD's own internal buffer
        # (several KB) before ever being handed to the OS pipe at all.
        # Practically: the diagnostic trail you need ("[VOICE]
        # Listening...", "[STT] Recognized: ...", "[COMMAND]
        # Processing...", "[TTS] Speaking...") could appear late, out of
        # order relative to real time, or in one big delayed dump —
        # making it look like nothing is happening even when it is, and
        # making the actual failure point impossible to pinpoint from the
        # console. `-u` (equivalently PYTHONUNBUFFERED=1) forces every
        # print() to flush immediately, so the pump thread shows you the
        # real, live sequence of what agent.py is doing.
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        agent_process = subprocess.Popen(
            [sys.executable, "-u", agent_path, "dev"],
            cwd=root_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        agent_status["running"]    = True
        agent_status["pid"]        = agent_process.pid
        agent_status["started_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        agent_status["restarts"]  += 1

        # FIX: drain the pipe continuously so agent.py's stdout writes
        # never block once the OS pipe buffer fills. See docstring above.
        threading.Thread(
            target=_pump_agent_output,
            args=(agent_process,),
            daemon=True,
        ).start()

        log_event("agent_start", f"Agent started with PID {agent_process.pid}")
        print(f"[JARVIS] Agent started — PID {agent_process.pid}")

    except Exception as e:
        log_event("error", f"Failed to start agent: {str(e)}")
        print(f"[JARVIS] Failed to start agent: {e}")


def stop_agent():
    if agent_process and agent_process.poll() is None:
        agent_process.terminate()
        try:
            agent_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent_process.kill()

        log_event("agent_stop", f"Agent stopped (PID {agent_process.pid})")
        print("[JARVIS] Agent stopped.")

    agent_status["running"] = False
    agent_status["pid"]     = None


# ── Ollama Chat Helper ─────────────────────────────────────
# Section 4 ("dynamic response length") + Section 5 ("proper markdown
# rendering"). The previous prompt hard-capped every reply at one
# sentence (and num_predict=150 enforced that at the token level too)
# — which is what made JARVIS incapable of ever explaining, teaching,
# or writing code no matter what was asked. The system prompt now asks
# for a length that matches the request instead of a fixed cap, and
# num_predict is raised enough that a genuinely long technical answer
# (e.g. a full explanation + code block) isn't truncated mid-thought.
# The butler personality/"Sir" address is preserved unchanged — only
# the length ceiling and formatting guidance change.
def _build_system_prompt(extra: str = "") -> str:
    ist          = pytz.timezone("Asia/Kolkata")
    now          = datetime.now(ist)
    current_time = now.strftime("%I:%M %p")
    current_date = now.strftime("%A, %d %B %Y")

    return f"""You are JARVIS, a personal AI assistant exactly like the one from Iron Man.
Speak like a classy butler. Be slightly sarcastic. Always address the user as Sir.

Match your response length to the request, not a fixed limit:
- Greetings, small talk, and simple factual questions: one or two sentences.
- Requests to do/confirm something: a brief acknowledgement that you did it.
- Technical, coding, debugging, planning, or "explain"/"design" requests: as much
  detail as is actually useful — explanation, reasoning, and structure — without
  padding it with filler.
- When you write code, put it in a fenced Markdown code block with the correct
  language tag (e.g. ```python), and use Markdown (headings, bold, bullet or
  numbered lists, tables) wherever it makes a technical answer clearer. Do not
  use Markdown for a one-line greeting.

Honesty (bug fix — this was previously missing here even though the voice
persona already had it): you have no ability to call tools, browse the web, or
look anything up from inside this response. Never write raw JSON, a fake
function/tool call (e.g. {{"name": "...", "parameters": {{...}}}}), or a
sentence like "let me search that" / "I couldn't find that on the web" — none
of that is real here, it is fabricated. For a plain factual question like
"What is Python?", just answer it directly from what you already know, in
your own words, like a knowledgeable person would — do not pretend to browse,
search, or invoke anything to do so.

Stay in character and in scope: acknowledge requests to take an action briefly,
then confirm. Use the ongoing conversation for context — refer back to what the
user already told you (their project, their previous choices, code you already
discussed) instead of asking them to repeat it, unless you genuinely don't have
enough information to proceed.

Current real time in India (IST): {current_time}
Current date: {current_date}
{extra}"""


def ask_ollama(user_message: str, history_msgs: list = [], model: Optional[str] = None) -> str:
    messages = [{"role": "system", "content": _build_system_prompt()}]

    for msg in history_msgs:
        if msg["role"] in ("user", "assistant"):
            messages.append({
                "role":    msg["role"],
                "content": msg["content"],
            })

    messages.append({"role": "user", "content": user_message})

    response = ollama.chat(
        # model_selector.py's select_model() decides this at the call
        # site based on the already-classified intent; None here just
        # means "no specific selection was made" (e.g. a caller that
        # predates model routing), so it keeps the original behavior.
        model=model or TEXT_MODEL,
        messages=messages,
        options={
            "temperature": 0.8,
            # Raised from 150 (one-sentence cap) so technical/planning
            # answers aren't cut off mid-explanation. Still bounded —
            # unbounded generation on a local model is a real latency/
            # memory cost — but big enough for a full explanation +
            # a reasonably sized code block.
            "num_predict": 900,
        },
    )
    return response["message"]["content"].strip()


# ── Ollama Vision Chat Helper (Sections 7, 8, 9, 14) ───────
def ask_ollama_vision(
    user_message: str,
    images_b64: list[str],
    history_msgs: list = [],
    extra_text_context: Optional[str] = None,
) -> str:
    """
    Sends the user's text AND the actual image bytes to VISION_MODEL.
    This is the real analysis path — the model receives the image
    itself (as base64 in the `images` field Ollama's vision-capable
    models expect), not a filename, not OCR-only text, and no claim of
    having "looked" at anything is returned unless this actually ran.

    extra_text_context carries along source code (Section 14: combined
    image + code debugging) or other text the user attached in the
    same or a very recent turn, appended to the prompt so the model can
    connect what's visible in the image to the actual code.

    Raises RuntimeError if VISION_MODEL isn't configured — callers must
    surface that honestly (Section 8/25) rather than silently falling
    back to the text model and pretending to have seen the image.
    """
    if not VISION_MODEL:
        raise RuntimeError("no vision-capable model is configured")

    system_prompt = _build_system_prompt(
        "\nAn image has been attached to this message. Actually inspect it — "
        "describe only what is really visible (errors, UI elements, diagrams, "
        "text) rather than guessing. If code was also provided as text, connect "
        "what you see in the image to that code explicitly."
    )

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history_msgs:
        if msg["role"] in ("user", "assistant"):
            messages.append({"role": msg["role"], "content": msg["content"]})

    content = user_message.strip() or "What do you see in this image? Analyze it."
    if extra_text_context:
        content = f"{content}\n\n--- Attached source code / text ---\n{extra_text_context}"

    messages.append({
        "role":    "user",
        "content": content,
        "images":  images_b64,
    })

    response = ollama.chat(
        model=VISION_MODEL,
        messages=messages,
        options={
            "temperature": 0.6,
            "num_predict": 900,
        },
    )
    return response["message"]["content"].strip()


# ── WebSocket Connection Manager ───────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

    async def send_personal(self, ws: WebSocket, data: dict):
        try:
            await ws.send_json(data)
        except Exception:
            self.disconnect(ws)


manager = ConnectionManager()


# ── Lifespan ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[JARVIS] Initialising database...")
    init_db()
    # Bug fix (found via live end-to-end testing, not caught by the
    # test suite): project_memory.py's tables were never created on
    # server startup — only memory.py's init_db() was called here.
    # Every test that touched project memory called
    # init_project_memory_db() explicitly in its own fixture, which
    # masked this in CI/pytest; a genuinely fresh production database
    # would 500 on the very first inspect_project/save_project_memory/
    # debug_investigation call with "no such table: projects".
    import project_memory
    project_memory.init_project_memory_db()
    # Phase 5: study_topics table, same CREATE TABLE IF NOT EXISTS
    # pattern as memory.init_db()/project_memory.init_project_memory_db()
    # above — a fresh DB needs this exactly like it needed the project
    # memory fix documented above (see Phase 3 summary), so it goes
    # through the same real startup path rather than being lazily
    # created on first use.
    study_assistant.init_study_db()
    log_event("system_start", "JARVIS backend starting up")

    # Phase 4 Feature 3: background task notifications (failure/success)
    # need to reach the same WS clients everything else broadcasts to.
    # Wired here rather than passed per-call because tool_registry's
    # handlers only ever receive **args — see background_tasks.py's
    # TaskManager.set_broadcast_fn docstring.
    import background_tasks
    background_tasks.task_manager.set_broadcast_fn(manager.broadcast)

    # Phase 6 Feature 6 (remaining work) — automatic/periodic health
    # monitoring. Wiring the broadcast fn here does not itself start
    # the checker loop (it stays opt-in via the "start automatic health
    # monitoring" chat command) — this only makes sure that whenever it
    # IS started, its alerts can reach WS clients, same as
    # background_tasks above.
    project_health_monitor.set_broadcast_fn(manager.broadcast)

    # PROACTIVE INTELLIGENCE — CPU/RAM/GPU/temp/battery/network,
    # security, storage, and voice-availability monitoring with real
    # debouncing/cooldown/severity/recovery (see proactive_engine.py).
    # Safe to auto-start unconditionally: it's not project-scoped
    # opt-in like project_health_monitor, every detector is a cheap
    # local psutil/PowerShell read (never a new security scan, never
    # the LLM), and it can be turned off at runtime via
    # POST /proactive/toggle (Section 19) without restarting.
    proactive_engine.proactive_engine.set_broadcast_fn(manager.broadcast)
    proactive_engine.proactive_engine.start()

    try:
        ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            options={"num_predict": 5},
        )
        print(f"[JARVIS] Ollama verified — model {OLLAMA_MODEL} ready.")
        log_event("ollama_ready", f"Ollama model {OLLAMA_MODEL} loaded and ready")
    except Exception as e:
        print(f"[JARVIS] WARNING: Ollama not responding: {e}")
        log_event("error", f"Ollama not available: {str(e)}")

    # Model-routing availability check (lightweight — ollama.list(),
    # not a generation call): confirms the coding/vision models are
    # actually pulled, without downloading anything automatically.
    availability = model_selector.check_model_availability()
    for label, info in availability["models"].items():
        if info["name"] and not info["available"]:
            print(f"[JARVIS] WARNING: configured {label} model '{info['name']}' does not appear to be pulled in Ollama.")
        elif info["name"]:
            print(f"[JARVIS] Model check: {label} = {info['name']} (available)")

    # Only spawn the voice agent when LiveKit is actually configured.
    # agent.py's worker.run() raises ValueError("ws_url is required...")
    # immediately if LIVEKIT_URL isn't set, so without this guard
    # start_agent() launches a subprocess that crashes on startup every
    # single time LiveKit is left unconfigured (the supported, optional
    # config path per config.py) — logging a false "Agent started" PID
    # right before it dies. Skip it and say why instead.
    if LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET:
        print("[JARVIS] Starting voice agent...")
        start_agent()
    else:
        print("[JARVIS] LiveKit not configured — skipping voice agent startup (text chat still works).")

    yield

    print("[JARVIS] Shutting down...")
    stop_agent()
    # Kill any still-running background tasks (Phase 4 Feature 3) so a
    # monitored `npm run dev` doesn't outlive this backend process as
    # an orphan subprocess.
    background_tasks.task_manager.shutdown_all()
    # Graceful shutdown of the health-monitor loop (Feature 6
    # requirement) — no-op if it was never started.
    project_health_monitor.stop_auto_monitor()
    proactive_engine.proactive_engine.stop()
    log_event("system_stop", "JARVIS backend shutting down")


# ── FastAPI App ────────────────────────────────────────────
app = FastAPI(
    title="JARVIS Backend",
    description="J.A.R.V.I.S Personal AI Assistant API — 100% Local",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # allow_credentials=True together with a wildcard origin is an
    # invalid combination per the CORS spec (browsers won't honor
    # Access-Control-Allow-Credentials alongside "*"). Nothing in this
    # app actually sends cookies/credentialed requests (frontend/app.js
    # never sets credentials: "include"), so this was dead, spec-invalid
    # config rather than something protecting a real feature — dropped.
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic Models ────────────────────────────────────────
class ChatRequest(BaseModel):
    message:    str
    session_id: Optional[str] = None
    # Set by backend_bridge_llm.py (the voice pipeline) when it has
    # already saved+broadcast the user's utterance itself via POST
    # /voice/transcript, before calling /chat -- avoids double-saving/
    # double-broadcasting the same user turn. Typed chat never sets this.
    skip_user_save: Optional[bool] = False


class ChatResponse(BaseModel):
    response:   str
    session_id: str
    timestamp:  str


class ClearRequest(BaseModel):
    session_id: str


class TranscriptRequest(BaseModel):
    role:       str
    content:    str
    session_id: Optional[str] = "voice-session"


# ── REST Endpoints ─────────────────────────────────────────
@app.get("/api")
async def root():
    return {
        "status":  "online",
        "name":    "J.A.R.V.I.S",
        "version": "2.1.0",
        "engine":  f"ollama/{TEXT_MODEL}" + (f" + {VISION_MODEL} (vision)" if VISION_MODEL else ""),
        "message": "At your service, Sir. Fully local.",
        "ui":      "/",
    }


@app.get("/status")
async def get_status():
    agent_alive = (
        agent_process is not None and
        agent_process.poll() is None
    )
    agent_status["running"] = agent_alive

    try:
        ollama.list()
        ollama_status = "online"
    except Exception:
        ollama_status = "offline"

    return {
        "backend":          "online",
        "agent":            "online" if agent_alive else "offline",
        "database":         "online",
        "livekit":          "configured" if LIVEKIT_URL else "not configured",
        "gemini":           ollama_status,
        "text_model":       TEXT_MODEL,
        "vision_model":     VISION_MODEL or None,
        "vision_available": bool(VISION_MODEL),
        "agent_pid":        agent_status["pid"],
        "agent_started_at": agent_status["started_at"],
        "agent_restarts":   agent_status["restarts"],
        "timestamp":        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }


_TOPIC_STRIP_RE = re.compile(
    r"^(teach me|explain|quiz me on|quiz me about|help me prepare for|help me with|"
    r"give me flashcards on|flashcards for|revision plan for|revision timetable for|"
    r"generate a coding exercise on|about|on)\s+", re.IGNORECASE,
)


def _extract_topic(user_message: str) -> Optional[str]:
    """
    Best-effort topic extraction for Section 9's study commands
    ("teach me operating systems" -> "operating systems"). Deliberately
    simple prefix-stripping rather than an LLM call — this only feeds
    a prompt-builder argument, and an imperfect topic still produces a
    reasonable prompt (the LLM sees the full original user_message too
    via the orchestrator's user turn, so this isn't the only signal).
    Returns None rather than a mangled guess if nothing is left after
    stripping, so callers fall back to a generic prompt instead of
    teaching "the current topic" about nothing.
    """
    original = user_message.strip()
    stripped = _TOPIC_STRIP_RE.sub("", original).strip(" ?.!")
    if stripped and stripped.lower() != original.lower():
        return stripped
    return None


def _format_activity_event(event: dict) -> str:
    """
    Feature 16 — one readable line per audit-log row for the "recent
    activity" command. Strips the "workflow:" prefix already used for
    every workflow-engine audit call and, for the (very common)
    "workflow_step" event whose message is a small JSON blob, unpacks
    it into plain fields instead of dumping raw JSON at the user (Rule:
    "Do not expose internal implementation noise unnecessarily.").
    """
    import json as _json

    action = event.get("event_type", "")
    if action.startswith("workflow:"):
        action = action[len("workflow:"):]
    message = event.get("message") or ""
    if action == "workflow_step":
        try:
            data = _json.loads(message)
            detail = f"{data.get('step', '?')} -> {data.get('status', '?')}"
            if data.get("outcome"):
                detail += f" ({data['outcome']})"
            message = detail
        except (ValueError, TypeError):
            pass
    return f"[{event.get('timestamp', '?')}] {action}: {message}"[:220]


async def _handle_phase3_intent(
    routed: Intent,
    intent_result,
    user_message: str,
    session_id: str,
) -> str:
    """
    Produces the chat reply for DEBUG / CODE_ANALYSIS / CODE_EXPLANATION /
    TERMINAL / SCREEN_ANALYSIS (Phase 4 Feature 1) / GIT (Phase 4
    Feature 4) / the Phase 5 study/CSE/hackathon/planning/developer-mode
    intents — deliberately NOT routed through ask_ollama()'s butler-voice
    system prompt (Section 20's "concise, structured" response design
    is a different shape than a one-sentence spoken reply, and
    debug_mode.py's Diagnosis.to_text()/code_analysis.py's
    AnalysisResult.to_text() already produce that structure directly).

    Runs through EXECUTING (debug_mode.py's Investigation broadcasts
    its own step-by-step detail; the single-shot analyze/explain paths
    broadcast one EXECUTING update since they're not multi-step).
    Falls back to a plain apology sentence on any failure, matching
    the existing ask_ollama() error path's tone, rather than leaking a
    raw traceback into the chat.
    """
    # Section 15: "ACTIVE MODE should become SYSTEM" while a
    # security/storage request is being handled, reverting afterward —
    # a plain broadcast the frontend already has a slot for (see
    # index.html's #active-mode-value), not a new UI surface.
    is_system_mode = routed in (Intent.SYSTEM_SECURITY, Intent.SYSTEM_STORAGE)
    if is_system_mode:
        try:
            await manager.broadcast({"type": "active_mode", "mode": "SYSTEM"})
        except Exception:
            pass

    try:
        if routed == Intent.DEBUG:
            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Starting investigation...")
            investigation = debug_mode_module.Investigation()
            result = await investigation.run(user_message, session_id=session_id, broadcast_state=manager.broadcast)
            if result.diagnosis is None:
                return "I looked into it, Sir, but couldn't form a diagnosis — could you point me at a specific file or error?"
            context_engine.context_engine.record_action(session_id, "Ran a debug investigation")
            # A cancelled/step-or-timeout-exhausted investigation gets
            # the Phase 4 "INVESTIGATION INCOMPLETE" report (evidence
            # collected so far + ranked hypotheses + a concrete next
            # step) rather than presenting result.diagnosis's
            # placeholder text as if it were a real finding.
            if result.stopped_reason and (result.cancelled or "budget exhausted" in result.stopped_reason):
                return result.to_incomplete_text()
            diagnosis_text = result.diagnosis.to_text()
            context_engine.context_engine.record_error(session_id, diagnosis_text[:400], source="debug_mode")
            return diagnosis_text

        elif routed == Intent.CODE_ANALYSIS:
            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Analyzing code...")
            ctx = context_manager.gather(session_id=session_id)
            target_file = debug_mode_module.guess_target_file(user_message, ctx)
            if not target_file:
                return "I'd be happy to analyze some code, Sir — which file did you mean?"
            try:
                analysis = code_analysis.analyze_file(target_file)
                context_engine.context_engine.record_action(session_id, f"Analyzed {os.path.basename(target_file)}")
                return analysis.to_text()
            except (FileNotFoundError, ValueError) as e:
                return f"I couldn't analyze that, Sir: {e}"

        elif routed == Intent.CODE_EXPLANATION:
            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Preparing explanation...")
            ctx = context_manager.gather(session_id=session_id)
            target_file = debug_mode_module.guess_target_file(user_message, ctx)
            if not target_file:
                return "I'd be glad to explain some code, Sir — which file (and function, if you like)?"
            mode = intent_result.mode or "TECHNICAL"
            try:
                unit = code_analysis.extract_unit(target_file)
                prompt = code_analysis.build_explanation_prompt(unit, mode)
                # Model routing: CODE_EXPLANATION is a coding-classified
                # intent (model_selector._CODING_INTENTS), so this now
                # goes to CODING_MODEL instead of the previously
                # hardcoded OLLAMA_MODEL (=TEXT_MODEL) — the prompt
                # itself is unchanged, only which model answers it.
                selected_model = model_selector.select_model(Intent.CODE_EXPLANATION)
                response = await asyncio.to_thread(
                    ollama.chat,
                    model=selected_model,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response["message"]["content"]
            except (FileNotFoundError, ValueError) as e:
                return f"I couldn't explain that, Sir: {e}"

        elif routed == Intent.SCREEN_ANALYSIS:
            # Screen capture always requires confirmation (see
            # tool_registry.py's analyze_screen registration) — this
            # goes through the exact same tool_registry/permission
            # gate run_terminal_command does, it just always lands on
            # CONFIRM rather than being classified per-call.
            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Preparing to look at your screen...")
            outcome = await tool_registry.run_tool("analyze_screen", {}, broadcast=manager.broadcast)
            status = outcome.get("status")
            if status == "pending_confirmation":
                return f"I'd like to take a look at your screen, Sir — {outcome.get('message')}"
            elif status != "ok":
                return f"I couldn't look at your screen, Sir: {outcome.get('message')}"

            res = outcome["result"]
            if not res.get("available"):
                return f"I couldn't get anything useful from your screen, Sir: {res.get('reason')}"

            context_engine.context_engine.record_action(session_id, "Analyzed the screen")
            if res.get("detected_errors"):
                context_engine.context_engine.record_error(
                    session_id, res["detected_errors"][0], source="screen",
                    file=res["file_references"][0] if res.get("file_references") else None,
                    line=res["line_references"][0] if res.get("line_references") else None,
                )

            # Section 16 — combine PROJECT CONTEXT + SCREEN VISUAL
            # CONTEXT into one coherent interpretation rather than a
            # bare dump of what OCR found, when a project is actually
            # known for this session.
            lines = [f"ACTIVE WINDOW\n{res.get('window_title') or 'unknown'} ({res.get('application_type') or 'UNKNOWN'})"]
            if current_context.project_name:
                lines.append(f"\nPROJECT\n{current_context.project_name}")
            if res.get("detected_errors"):
                lines.append("\nDETECTED ERRORS\n" + "\n".join(res["detected_errors"][:5]))
            if res.get("file_references"):
                lines.append("\nFILES REFERENCED\n" + ", ".join(res["file_references"]))
            if not res.get("detected_errors") and not res.get("file_references"):
                snippet = (res.get("extracted_text") or "").strip()[:500]
                lines.append(f"\nTEXT ON SCREEN (excerpt)\n{snippet or '(nothing recognizable was extracted)'}")
            return "\n".join(lines)

        elif routed == Intent.GIT:
            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Checking Git...")
            action = git_tools.route_git_request(user_message)
            ctx = context_manager.gather(session_id=session_id)
            cwd = ctx.active_project_path

            if action == "merge_conflict":
                target_file = debug_mode_module.guess_target_file(user_message, ctx)
                if not target_file:
                    return "Which file has the conflict, Sir? I couldn't find one named in your message."
                result = await git_tools.analyze_merge_conflict(target_file)
                return result.to_text()

            if action == "commit_message":
                result = await git_tools.generate_commit_message(cwd)
                if not result.get("available"):
                    return f"I couldn't generate a commit message, Sir: {result.get('reason')}"
                if not result.get("message"):
                    return result.get("reason", "Nothing to describe — no changes found.")
                return f"Here's a proposed commit message, Sir ({result.get('source')} changes) — I haven't run `git commit`:\n\n{result['message']}"

            if action == "log":
                result = await git_tools.git_log(cwd)
                if not result.get("available"):
                    return f"I couldn't read the commit log, Sir: {result.get('reason')}"
                commits = result.get("commits", [])
                if not commits:
                    return "No commits yet, Sir."
                return "\n".join(f"{c['hash']} — {c['message']} ({c['author']}, {c['when']})" for c in commits)

            if action == "branch":
                result = await git_tools.git_branch(cwd)
                if not result.get("available"):
                    return f"I couldn't check branches, Sir: {result.get('reason')}"
                current = result.get("current") or "unknown"
                others = [b["name"] for b in result.get("branches", []) if not b["current"]]
                return f"You're on '{current}', Sir." + (f" Other branches: {', '.join(others)}." if others else "")

            if action == "diff":
                result = await git_tools.git_diff(cwd)
                if not result.available:
                    return f"I couldn't get the diff, Sir: {result.reason}"
                if not result.diff_text or not result.diff_text.strip():
                    return f"No unstaged changes, Sir. ({result.stat_summary})"
                return f"{result.stat_summary}\n\n{result.diff_text}"

            # action == "summary" (default) — status + change summary combined
            result = await git_tools.generate_change_summary(cwd)
            if not result.get("available"):
                return f"I couldn't summarize changes, Sir: {result.get('reason')}"
            context_engine.context_engine.record_action(session_id, f"Checked Git status ({action})")
            return result["summary_text"]

        elif routed == Intent.TERMINAL:
            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Preparing command...")
            command = terminal_tools.extract_command_from_message(user_message)
            if not command:
                return (
                    "I'd be glad to run that, Sir — could you give me the exact command, "
                    "e.g. \"run `pytest -k foo`\"?"
                )
            # Goes through the same tool_registry/permission_manager gate
            # as every other CONFIRM/DANGEROUS tool — SAFE commands run
            # immediately, anything else returns pending_confirmation and
            # is picked up by the existing /confirmations/{id} flow.
            outcome = await tool_registry.run_tool(
                "run_terminal_command", {"command": command}, broadcast=manager.broadcast,
            )
            status = outcome.get("status")
            if status == "ok":
                res = outcome["result"]
                context_engine.context_engine.record_action(session_id, f"Ran command: {res['command']}")
                lines = [f"COMMAND\n{res['command']}", f"\nEXIT CODE\n{res['exit_code']}"]
                if res.get("stdout", "").strip():
                    lines.append(f"\nOUTPUT\n{res['stdout'].strip()}")
                if res.get("stderr", "").strip():
                    lines.append(f"\nERROR\n{res['stderr'].strip()}")
                extracted = res.get("extracted_error")
                if extracted and extracted.get("primary_error"):
                    context_engine.context_engine.record_error(
                        session_id, extracted.get("likely_root_cause") or extracted["primary_error"], source="terminal",
                    )
                    lines.append(f"\nLIKELY CAUSE\n{extracted.get('likely_root_cause') or extracted['primary_error']}")
                return "\n".join(lines)
            elif status == "pending_confirmation":
                return f"That command needs your confirmation first, Sir: {outcome.get('message')}"
            elif status == "blocked":
                return f"I can't run that, Sir: {outcome.get('message')}"
            else:
                return f"I couldn't run that command, Sir: {outcome.get('message')}"

        elif routed == Intent.SYSTEM_SECURITY:
            msg_lower = user_message.lower()
            # Sub-action detection is deliberately simple keyword
            # matching (Section 18 — the LLM is used to interpret/
            # communicate results, not to decide which deterministic
            # tool call to make). "full"/"quick" pick the scan type;
            # anything else that reads as a scan request defaults to a
            # quick scan (Section 2's lightweight default); anything
            # else again is a pure status read with no new scan
            # started, e.g. "is the scan done yet".
            if "full" in msg_lower:
                scan_type = "full"
            elif any(w in msg_lower for w in ("scan", "virus", "threat", "safe", "malware")):
                scan_type = "quick"
            else:
                scan_type = None

            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="SYSTEM SECURITY SCAN — SCANNING...")
            scan_started = None
            if scan_type is not None:
                outcome = await tool_registry.run_tool("system_security_scan", {"scan_type": scan_type}, broadcast=manager.broadcast)
                status = outcome.get("status")
                if status == "pending_confirmation":
                    return f"I'd like to run a full security scan, Sir — {outcome.get('message')}"
                elif status not in ("ok",):
                    return f"I couldn't start the security scan, Sir: {outcome.get('message')}"
                scan_started = outcome["result"]

            sec_status = await asyncio.to_thread(system_health.get_security_status)
            threats = await asyncio.to_thread(system_health.get_threat_detections)
            return system_health.format_security_report(sec_status, threats, scan_started)

        elif routed == Intent.SYSTEM_STORAGE:
            msg_lower = user_message.lower()
            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Reading storage information...")

            if any(w in msg_lower for w in ("clean", "cleanup", "remove junk", "delete junk")):
                analyze_outcome = await tool_registry.run_tool("system_storage_analyze", {}, broadcast=manager.broadcast)
                if analyze_outcome.get("status") != "ok":
                    return f"I couldn't analyze storage, Sir: {analyze_outcome.get('message')}"
                analysis = analyze_outcome["result"]
                if not analysis.get("available"):
                    return system_health.format_storage_analysis(analysis)

                safe_keys = [c["key"] for c in analysis.get("categories", []) if c["classification"] == "SAFE_TO_CLEAN" and c["size_bytes"] > 0]
                if not safe_keys:
                    return "I didn't find anything in the SAFE_TO_CLEAN categories worth removing right now, Sir."

                # dry_run=False here does NOT delete anything by itself —
                # tool_registry's dynamic classifier (see
                # _classify_clean_junk in tool_registry.py) escalates
                # this exact call to CONFIRM, so it always comes back
                # pending_confirmation. This is what actually shows the
                # itemized preview + puts a real Allow/Deny prompt in
                # front of the user (Section 6/7/13) — there is no path
                # in this handler that reaches an executed deletion
                # without that round trip.
                clean_outcome = await tool_registry.run_tool(
                    "system_clean_junk", {"category_keys": safe_keys, "dry_run": False}, broadcast=manager.broadcast,
                )
                if clean_outcome.get("status") == "pending_confirmation":
                    preview = system_health.clean_junk(safe_keys, dry_run=True)
                    return (
                        f"{system_health.format_clean_preview(preview)}\n\n"
                        f"Say the word and I'll remove these, Sir — or use the confirmation prompt above."
                    )
                elif clean_outcome.get("status") == "ok":
                    return system_health.format_clean_result(clean_outcome["result"])
                else:
                    return f"I couldn't prepare the cleanup, Sir: {clean_outcome.get('message')}"

            if any(w in msg_lower for w in ("large file", "biggest file", "taking up", "big files", "large files")):
                outcome = await tool_registry.run_tool("system_find_large_files", {}, broadcast=manager.broadcast)
                if outcome.get("status") != "ok":
                    return f"I couldn't search for large files, Sir: {outcome.get('message')}"
                return system_health.format_large_files(outcome["result"])

            if any(w in msg_lower for w in ("free space", "how much storage", "disk space", "storage details")):
                outcome = await tool_registry.run_tool("system_storage_summary", {}, broadcast=manager.broadcast)
                if outcome.get("status") != "ok":
                    return f"I couldn't read storage information, Sir: {outcome.get('message')}"
                return system_health.format_storage_summary(outcome["result"])

            # Default: full junk/category analysis (Section 4/11) —
            # covers "analyze my storage", "what's taking up my
            # storage", "find junk files", "how much junk can I clean".
            outcome = await tool_registry.run_tool("system_storage_analyze", {}, broadcast=manager.broadcast)
            if outcome.get("status") != "ok":
                return f"I couldn't analyze storage, Sir: {outcome.get('message')}"
            return system_health.format_storage_analysis(outcome["result"])

        # ── Phase 5 intents ─────────────────────────────────────
        elif routed in (Intent.DSA, Intent.STUDY, Intent.INTERVIEW):
            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Thinking it through...")
            history = get_history(session_id, limit=10)
            ctx_block = session_context.context_to_prompt_block(session_id)

            if routed == Intent.DSA:
                static_answer = cse_assistant.try_static_answer(user_message)
                if static_answer:
                    return static_answer
                return await asyncio.to_thread(
                    llm_orchestrator.run, cse_assistant.build_system_prompt(), user_message, history, ctx_block,
                )

            # STUDY / INTERVIEW share the study assistant's prompt
            # builders — INTERVIEW is treated as a quiz/viva-style
            # study session rather than a separate subsystem, since
            # Section 9's "ask viva questions" is exactly that shape.
            lower = user_message.lower()
            topic = _extract_topic(user_message)
            if "study session" in lower:
                # Phase 6 Feature 13 — the interactive teach -> quiz ->
                # evaluate -> adjust-difficulty loop, now genuinely
                # engine-backed via the WAITING_FOR_USER suspend/resume
                # primitive (workflow_engine.provide_input(), checked
                # early in chat() for as long as this workflow is
                # active). Distinct trigger phrase ("study session") so
                # ordinary "teach me X" / "quiz me on X" — answered by
                # the untouched per-message path below — can never be
                # accidentally upgraded into a multi-round workflow.
                rounds_match = re.search(r"(\d+)\s*round", lower)
                rounds = int(rounds_match.group(1)) if rounds_match else 3
                await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Starting your study session...")
                wf = workflow_engine.create_workflow(
                    "study_session",
                    user_request=user_message,
                    goal=f"Guided study session on {topic or 'the topic'}",
                    session_id=session_id,
                    topic=topic,
                    rounds=rounds,
                )
                result = await workflow_engine.run(wf.id, broadcast=manager.broadcast)
                if result.status == WorkflowStatus.WAITING_FOR_USER:
                    return (result.pending_input or {}).get("prompt", "")
                if result.status == WorkflowStatus.COMPLETED:
                    return result.steps[-1].result or result.to_report()
                return (
                    f"I couldn't get the study session going, Sir "
                    f"({result.stopped_reason or result.status.value}):\n\n{result.to_report()}"
                )
            if "quiz" in lower and ("harder" in lower or "another" in lower):
                if topic:
                    study_assistant.start_topic(session_id, topic)
                system_prompt = study_assistant.quiz_prompt(topic or "the current topic", harder=True)
            elif "quiz" in lower:
                if topic:
                    study_assistant.start_topic(session_id, topic)
                system_prompt = study_assistant.quiz_prompt(topic or "the current topic")
            elif "flashcard" in lower:
                system_prompt = study_assistant.flashcards_prompt(topic or "the current topic")
            elif "viva" in lower or routed == Intent.INTERVIEW:
                system_prompt = study_assistant.viva_questions_prompt(topic or "the current topic")
            elif "revision plan" in lower or "revision timetable" in lower:
                system_prompt = study_assistant.revision_plan_prompt(topic or "the exam")
            elif topic:
                level = study_assistant.get_current_level(session_id, topic)
                study_assistant.start_topic(session_id, topic, level=level)
                system_prompt = study_assistant.teach_prompt(topic, level=level)
            else:
                system_prompt = cse_assistant.build_system_prompt()

            return await asyncio.to_thread(
                llm_orchestrator.run, system_prompt, user_message, history, ctx_block,
            )

        elif routed == Intent.PLANNING:
            lower = user_message.lower()
            # Phase 6 Feature 5 — "prepare my environment" without a
            # hackathon/exam qualifier means an actual dev-environment
            # workflow (workflow_engine.py's "dev_env_prep"), not a
            # static core/task_planner.py plan — it needs to actually
            # look at the project, not just describe generic steps.
            if "hackathon" not in lower and "exam" not in lower and (
                "environment" in lower or "prepare" in lower or "set up" in lower or "setup" in lower
            ):
                ctx = context_manager.gather(session_id=session_id)
                project_path = ctx.active_project_path
                if not project_path:
                    return (
                        "I don't have an active project selected, Sir — which project directory "
                        "should I prepare?"
                    )
                await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Preparing your development environment...")
                wf = workflow_engine.create_workflow(
                    "dev_env_prep",
                    user_request=user_message,
                    goal=f"Development environment preparation for {project_path}",
                    project_path=project_path,
                    session_id=session_id,
                )
                result = await workflow_engine.run(wf.id, broadcast=manager.broadcast)
                if result.status == WorkflowStatus.WAITING_FOR_PERMISSION:
                    plan_text = result.steps[-2].result or ""  # generate_plan's own PROPOSED CHANGES text
                    preview = workflow_engine.pending_steps_preview(wf.id)
                    actions_text = "\n".join(f"{p['index']}. {p['description']}" for p in preview)
                    return (
                        f"{plan_text}\n\nRecommended actions:\n\n{actions_text}\n\n"
                        "Choose: \"approve 1\" / \"approve all\" / \"reject\""
                    )
                if result.status == WorkflowStatus.COMPLETED:
                    plan_text = result.steps[-2].result or result.to_report()
                    return plan_text
                return (
                    f"I couldn't finish preparing the environment, Sir "
                    f"({result.stopped_reason or result.status.value}):\n\n{result.to_report()}"
                )

            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Building a plan...")
            try:
                if "hackathon" in lower:
                    plan = task_planner.create_plan("hackathon_environment")
                elif "exam" in lower:
                    # Phase 6 Feature 13 — routed through the workflow_engine's
                    # real "exam_prep" workflow (actual LLM-backed revision
                    # plan + practice-question generation, OBSERVE/PLAN/ACT/
                    # VERIFY, progress/audit like every other Phase 6
                    # workflow) instead of core/task_planner.py's old static
                    # plan_exam_prep(), which only ever described steps
                    # without doing them. This branch is reached only when
                    # the LLM classified the message as PLANNING *and* it
                    # mentions "exam" — an ordinary "teach me operating
                    # systems" or "quiz me on X" still classifies as STUDY
                    # and never reaches here (see intent_router.py), so
                    # normal study requests are not at risk of being
                    # reinterpreted as exam-prep workflows.
                    subject = _extract_topic(user_message)
                    wf = workflow_engine.create_workflow(
                        "exam_prep",
                        user_request=user_message,
                        goal=f"Exam prep for {subject or 'the exam'}",
                        session_id=session_id,
                    )
                    result = await workflow_engine.run(wf.id, broadcast=manager.broadcast)
                    if result.status == WorkflowStatus.COMPLETED:
                        return result.steps[-1].result or result.to_report()
                    return (
                        f"I couldn't finish putting your exam prep together, Sir "
                        f"({result.stopped_reason or result.status.value}):\n\n{result.to_report()}"
                    )
                else:
                    return (
                        "I can put together a plan, Sir — for a hackathon environment or exam prep so far. "
                        "Which did you have in mind?"
                    )
            except ValueError as e:
                return f"I couldn't build that plan, Sir: {e}"
            confirmation_note = (
                " I'll need your go-ahead before any step that touches files or the terminal."
                if plan.requires_confirmation else ""
            )
            return f"{plan.to_text()}\n\n(Shall I proceed?{confirmation_note})"

        elif routed == Intent.HACKATHON:
            lower = user_message.lower()
            # Phase 6 Feature 14 — a request for a genuine end-to-end
            # project plan (ideas -> architecture -> tech stack -> MVP
            # -> task breakdown -> pitch) is routed through the real
            # "hackathon_project" workflow instead of a single generic
            # LLM reply. A single-capability ask ("give me hackathon
            # ideas", "what tech stack should I use", "pitch prep") is
            # left completely untouched: hackathon_assistant.
            # classify_request() already recognizes those, and only
            # messages it does NOT recognize AND that explicitly ask
            # for a full/whole project plan reach the workflow branch —
            # so ordinary single-capability requests can never be
            # accidentally converted into a 6-step workflow.
            wants_full_plan = re.search(
                r"\b(plan|build|put together|design|walk me through)\b.*\bhackathon project\b"
                r"|\bhackathon project\b.*\b(plan|end.to.end|from scratch|from start to finish)\b",
                lower,
            )
            if wants_full_plan and hackathon_assistant.classify_request(user_message) is None:
                await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Building your hackathon project plan...")
                theme = _extract_topic(user_message)
                team_match = re.search(r"(\d+)\s*(?:person|people|member)", lower)
                team_size = int(team_match.group(1)) if team_match else 3
                wf = workflow_engine.create_workflow(
                    "hackathon_project",
                    user_request=user_message,
                    goal=f"Hackathon project plan{f' — {theme}' if theme else ''}",
                    session_id=session_id,
                    theme=theme,
                    team_size=team_size,
                )
                result = await workflow_engine.run(wf.id, broadcast=manager.broadcast)
                if result.status == WorkflowStatus.COMPLETED:
                    return result.steps[-1].result or result.to_report()
                return (
                    f"I couldn't finish putting your hackathon plan together, Sir "
                    f"({result.stopped_reason or result.status.value}):\n\n{result.to_report()}"
                )

            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Working on that...")
            history = get_history(session_id, limit=10)
            system_prompt = hackathon_assistant.dispatch(user_message) or hackathon_assistant.idea_generation_prompt()
            ctx_block = session_context.context_to_prompt_block(session_id)
            return await asyncio.to_thread(
                llm_orchestrator.run, system_prompt, user_message, history, ctx_block,
            )

        elif routed == Intent.PROJECT_ANALYSIS:
            # Phase 6 Feature 4 — routes through the central workflow
            # engine, not a one-shot tool call: this is a bounded,
            # multi-step, evidence-based review (structure -> entry
            # points -> dependencies -> static analysis -> git status
            # -> report), read-only by construction (see
            # workflows/project_review.py's module docstring for why
            # it needs no permission gate).
            ctx = context_manager.gather(session_id=session_id)
            project_path = ctx.active_project_path
            if not project_path:
                return (
                    "I don't have an active project selected, Sir — could you tell me which "
                    "project directory to review?"
                )
            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Reviewing your project...")
            wf = workflow_engine.create_workflow(
                "project_review",
                user_request=user_message,
                goal=f"Project review for {project_path}",
                project_path=project_path,
                session_id=session_id,
            )
            result = await workflow_engine.run(wf.id, broadcast=manager.broadcast)
            if result.status == WorkflowStatus.COMPLETED:
                # The last step's own result IS the formatted report —
                # generate_report() in project_review.py already builds
                # the exact PROJECT HEALTH REPORT shape.
                return result.steps[-1].result or result.to_report()
            if result.status == WorkflowStatus.WAITING_FOR_PERMISSION:
                return "I need your go-ahead before I can continue that review, Sir."
            return (
                f"I couldn't finish reviewing the project, Sir "
                f"({result.stopped_reason or result.status.value}). Here's what I found before stopping:\n\n"
                f"{result.to_report()}"
            )

        elif routed == Intent.DEVELOPER_MODE:
            if developer_assistant.wants_to_exit_developer_mode(user_message):
                session_context.update_context(session_id, mode="general")
                return "Exiting developer mode, Sir."

            session_context.update_context(session_id, mode="developer")
            if not developer_assistant.wants_investigation(user_message):
                # A plain "enter developer mode" toggle with no debug
                # request attached just flips the mode and waits — it
                # does not itself kick off a (likely contextless)
                # investigation. See developer_assistant.wants_investigation.
                return "Developer mode active, Sir. What would you like me to debug?"

            await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast, detail="Entering developer mode...")
            investigation = debug_mode_module.Investigation()
            result = await investigation.run(user_message, session_id=session_id, broadcast_state=manager.broadcast)
            if result.diagnosis is None:
                return "Developer mode active, Sir — I couldn't form a diagnosis yet. Point me at a file or error?"
            report = developer_assistant.format_diagnosis_as_developer_report(result.diagnosis)
            return report.to_text()

    except Exception as e:
        log_event("error", f"Phase 3 handler ({routed.value}) failed: {e}")
        await state_manager.set_error(manager.broadcast, detail=str(e))
        return "I ran into trouble working on that, Sir — my apologies."
    finally:
        if is_system_mode:
            try:
                await manager.broadcast({"type": "active_mode", "mode": "ASSISTANT"})
            except Exception:
                pass

    # Unreachable given the routed-intent check in /chat, but keeps
    # this function honest about always returning a string.
    return "I'm not quite sure how to handle that yet, Sir."


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())

    # Section 20 — quiet/focus: a fresh message means the user is
    # actively engaged, so hold back any INFO-level proactive event for
    # a short window rather than have it land mid-conversation.
    # WARNING/CRITICAL events are unaffected — see ProactiveEngine._emit.
    proactive_engine.proactive_engine.note_user_activity()

    if not request.skip_user_save:
        save_message(session_id, "user", request.message)

        await manager.broadcast({
            "type":       "message",
            "role":       "user",
            "content":    request.message,
            "session_id": session_id,
            "timestamp":  datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        })

    history = get_history(session_id, limit=10)

    # ── Phase 5: resolve contextual references ("open it", "the
    # second one", "explain the previous one") against the history
    # gathered above (excluding the message just saved, which is the
    # request itself) before classification, so intent_router sees a
    # de-referenced message instead of a bare pronoun it has no way to
    # ground. Only substitutes when resolution actually found
    # something (confidence > 0) — an unresolved reference is passed
    # through unchanged and the low reference_resolution_confidence is
    # still available to core/confidence.py if a handler wants it.
    resolved_reference = resolve_reference(request.message, history[:-1] if history else [])
    effective_message = (
        resolved_reference.resolved_message
        if resolved_reference.was_referential and resolved_reference.confidence > 0
        else request.message
    )

    # ── Sections 3 & 10: follow-up questions about a previously
    # uploaded image/document, with no new file attached this turn
    # ("what's wrong with it?", "explain that screenshot", "what are
    # the requirements?"). core/reference_resolver.py resolves this/
    # that/it against *text* history; it has no notion of an image or
    # uploaded document, so that reference needs a separate check
    # against the attachment cache. Only triggers when the message
    # actually reads like it's pointing at the attachment (see
    # _mentions_any) — an unrelated new question still falls through to
    # ordinary classification untouched.
    cached_image = _recall_attachment(session_id, "image")
    cached_doc = _recall_attachment(session_id, "document")
    if cached_image and _mentions_any(effective_message, _VISUAL_REFERENCE_WORDS):
        await state_manager.set_state(JarvisState.THINKING, manager.broadcast, detail="Re-inspecting the image...")
        extra_context = None
        if cached_doc and _mentions_any(effective_message, ("code", "debug", "bug", "error", "fix")):
            extra_context = f"File: {cached_doc['filename']}\n{cached_doc['text']}"
        try:
            reply = await asyncio.to_thread(
                ask_ollama_vision, request.message, [cached_image["base64_data"]], history[:-1] if history else [], extra_context,
            )
        except Exception as e:
            log_event("error", f"Vision follow-up failed: {e}")
            reply = f"I couldn't re-examine that image, Sir: {e}"
        finally:
            await state_manager.set_state(JarvisState.IDLE, manager.broadcast, force=True)
        save_message(session_id, "assistant", reply)
        await manager.broadcast({
            "type": "message", "role": "assistant", "content": reply,
            "session_id": session_id, "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        })
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    if cached_doc and not cached_image and _mentions_any(effective_message, _DOCUMENT_REFERENCE_WORDS):
        await state_manager.set_state(JarvisState.THINKING, manager.broadcast, detail="Re-reading the document...")
        prompt = (
            f"The user previously uploaded a file named '{cached_doc['filename']}'. Its extracted "
            f"contents are below. Answer using ONLY this content as the source of truth.\n\n"
            f"--- FILE CONTENTS ---\n{cached_doc['text']}\n--- END FILE CONTENTS ---"
        )
        try:
            reply = await asyncio.to_thread(ask_ollama, f"{request.message}\n\n{prompt}", history[:-1] if history else [])
        except Exception as e:
            log_event("error", f"Document follow-up failed: {e}")
            reply = f"I couldn't reason over that document again, Sir: {e}"
        finally:
            await state_manager.set_state(JarvisState.IDLE, manager.broadcast, force=True)
        save_message(session_id, "assistant", reply)
        await manager.broadcast({
            "type": "message", "role": "assistant", "content": reply,
            "session_id": session_id, "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        })
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    # ── Context Awareness ("What am I doing?") ──────────────────────
    # A second, distinct reference-resolution pass from resolve_reference
    # above: that one works purely off conversation TEXT ("the second
    # one", "the file we were working on"); this one grounds phrases
    # that need the actual computer context instead ("this file",
    # "check this", "fix it" referring to a real recorded error) — see
    # context_engine.py's module docstring for why these are kept as
    # two separate passes rather than merged into one resolver.
    #
    # gather() itself is cheap (no screenshot, no LLM call — see its
    # own docstring); a screenshot only happens if wants_screen ends up
    # True below, and even then it's exactly the existing
    # screen_tools.analyze_screen() call SCREEN_ANALYSIS already uses,
    # not a second capture path.
    current_context = context_engine.context_engine.gather(
        session_id,
        active_attachment=(cached_image or cached_doc or {}).get("filename"),
    )
    context_reference = context_engine.context_engine.resolve(effective_message, current_context)

    if context_reference.was_referential:
        # Section 12 — genuine ambiguity (multiple known projects
        # matched the same name in the window title) gets a concise
        # clarifying question instead of a guess. This is the one case
        # this module short-circuits the normal chat flow for; every
        # other resolution just enriches the message below and lets
        # the existing pipeline (intent classification -> LLM) handle it.
        if context_reference.resolved_kind == "project" and current_context.ambiguous_projects:
            reply = (
                f"I can see {len(current_context.ambiguous_projects)} projects matching "
                f"'{current_context.project_name}', Sir. Which one did you mean: "
                f"{', '.join(current_context.ambiguous_projects)}?"
            )
            save_message(session_id, "assistant", reply)
            await manager.broadcast({
                "type": "message", "role": "assistant", "content": reply,
                "session_id": session_id, "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            })
            return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

        # Otherwise, fold whatever was actually resolved into the
        # message as a short, explicit annotation — never silently
        # replacing the user's words, just grounding the pronoun for
        # the classifier/LLM the same way resolve_reference's
        # substitution does above.
        if context_reference.resolved_entity:
            effective_message = f"{effective_message} [context: {context_reference.resolved_kind} = {context_reference.resolved_entity}]"

        # Section 7 — an explicit screen-directed phrase ("check this",
        # "look at this", "what's wrong here") routes straight to the
        # existing SCREEN_ANALYSIS handling below rather than leaving
        # it to the LLM classifier to infer from a bare "check this",
        # which has very little to classify against on its own.
        if context_reference.wants_screen and context_reference.resolved_kind in (None, "screen"):
            effective_message = f"{effective_message} [screen analysis requested]"
    # These are meta-commands about an existing background task, not
    # something the intent classifier should route — check them first,
    # against whichever workflow was most recently started in this
    # session (workflow_engine.latest_for_session), same scoping
    # debug_mode.py's Investigation implicitly uses for "the current
    # investigation." A message that doesn't match one of these exact
    # short forms falls through to normal classification untouched, so
    # "let's pause and think about this differently" still reaches the
    # LLM as ordinary chat.
    _lower = effective_message.strip().lower().rstrip(".!")
    _workflow_control_phrases = {
        "pause": ("pause", "pause it", "pause that", "pause the workflow",
                  "pause the investigation", "pause the review"),
        "resume": ("continue", "resume", "keep going", "resume the workflow",
                   "continue the investigation", "resume it"),
        "cancel": ("cancel", "cancel it", "cancel that", "stop", "cancel the workflow",
                   "cancel the investigation", "cancel the review"),
    }
    for _action, _phrases in _workflow_control_phrases.items():
        if _lower in _phrases:
            active = workflow_engine.latest_for_session(session_id)
            if active is None or not _workflow_is_active(active.status):
                reply = "There's nothing running for me to control right now, Sir."
            elif _action == "pause":
                workflow_engine.pause(active.id)
                reply = "Pausing, Sir."
            elif _action == "resume":
                if active.status != WorkflowStatus.PAUSED:
                    reply = "That workflow isn't paused, Sir."
                else:
                    workflow_engine.resume(active.id, broadcast=manager.broadcast)
                    reply = "Resuming, Sir."
            else:  # cancel
                workflow_engine.cancel(active.id)
                reply = "Cancelled, Sir. I've kept whatever evidence was already gathered."
            save_message(session_id, "assistant", reply)
            return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    # ── Structured Approval UX (Feature 10) ───────────────────────
    # Same "meta-command about an existing background task" treatment
    # as pause/resume/cancel above. A workflow WAITING_FOR_PERMISSION
    # is genuinely stuck without one of these — the generic
    # /confirmations/{id} REST endpoint runs the underlying tool but
    # never advances the *workflow* itself (see its docstring), so
    # these are the only way a chat user can actually get a paused-for-
    # permission workflow moving again.
    _approve_n_match = re.match(r"^approve(?:\s+(\d+(?:\s*,\s*\d+)*))?$", _lower)
    if _approve_n_match or _lower in ("approve all", "approve everything", "reject", "reject it", "reject that", "deny"):
        active = workflow_engine.latest_for_session(session_id)
        if active is None or active.status != WorkflowStatus.WAITING_FOR_PERMISSION:
            reply = "There's nothing waiting on my approval right now, Sir."
        elif _lower in ("reject", "reject it", "reject that", "deny"):
            workflow_engine.reject_next_step(active.id, broadcast=manager.broadcast)
            reply = "Rejected, Sir — I won't run that step. Continuing with the rest."
        elif _lower in ("approve all", "approve everything"):
            workflow_engine.approve_all_remaining(active.id, broadcast=manager.broadcast)
            reply = "Approved, Sir — I'll proceed with everything remaining without asking again."
        else:
            indices_raw = _approve_n_match.group(1)
            count = len(indices_raw.split(",")) if indices_raw else 1
            workflow_engine.approve_steps(active.id, count=count, broadcast=manager.broadcast)
            reply = f"Approved {count} step(s), Sir. Continuing." if count > 1 else "Approved, Sir. Continuing."
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    # ── Phase 6 Feature 6: Project Health Monitor meta-commands ───
    # Explicit opt-in/opt-out and an on-demand report — same "checked
    # before classification" treatment as pause/continue/cancel above,
    # since these are commands about a monitor, not something an LLM
    # needs to interpret.
    if _lower in ("monitor this project", "start monitoring this project", "start monitoring"):
        ctx = context_manager.gather(session_id=session_id)
        if not ctx.active_project_path:
            reply = "I don't have an active project selected, Sir."
        else:
            project_health_monitor.enable(ctx.active_project_path)
            reply = f"Monitoring {ctx.active_project_path}, Sir. Ask me for a health check any time."
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    if _lower in ("stop monitoring", "stop monitoring this project"):
        ctx = context_manager.gather(session_id=session_id)
        if ctx.active_project_path and project_health_monitor.is_enabled(ctx.active_project_path):
            project_health_monitor.disable(ctx.active_project_path)
            reply = "Stopped monitoring, Sir."
        else:
            reply = "I wasn't monitoring that project, Sir."
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    if _lower in ("project health", "check project health", "how's my project", "hows my project", "health check"):
        ctx = context_manager.gather(session_id=session_id)
        if not ctx.active_project_path:
            reply = "I don't have an active project selected, Sir."
        elif not project_health_monitor.is_enabled(ctx.active_project_path):
            reply = (
                "I'm not monitoring that project yet, Sir — say \"monitor this project\" "
                "and I'll start tracking it."
            )
        else:
            report = await project_health_monitor.get_report(ctx.active_project_path)
            reply = report.to_text()
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    # ── Phase 6 Feature 6 (remaining work) — automatic/periodic health
    # monitoring controls. Distinct from "monitor this project" above,
    # which only opts a project into on-demand reports; these control
    # the single background checker that periodically re-runs those
    # same reports and raises an alert only on new/changed issues.
    if _lower in ("start automatic health monitoring", "enable automatic health monitoring",
                  "turn on automatic monitoring", "start automatic monitoring"):
        project_health_monitor.set_broadcast_fn(manager.broadcast)
        started = project_health_monitor.start_auto_monitor()
        reply = (
            f"Automatic health monitoring is running, Sir — checking every "
            f"{int(project_health_monitor.auto_interval_seconds // 60)} minute(s)."
            if started else
            "Automatic health monitoring is already running, Sir."
        )
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    if _lower in ("stop automatic health monitoring", "disable automatic health monitoring",
                  "turn off automatic monitoring", "stop automatic monitoring"):
        was_running = project_health_monitor.is_auto_monitor_running()
        project_health_monitor.stop_auto_monitor()
        reply = "Stopped automatic health monitoring, Sir." if was_running else "Automatic health monitoring wasn't running, Sir."
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    _interval_match = re.match(r"^set health check interval to (\d+)\s*(minute|minutes|min|second|seconds|sec)?$", _lower)
    if _interval_match:
        value = int(_interval_match.group(1))
        unit = _interval_match.group(2) or "minutes"
        seconds = value if unit.startswith("sec") else value * 60
        applied = project_health_monitor.set_auto_interval(seconds)
        reply = f"Health check interval set to {int(applied // 60)} minute(s), Sir."
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    # ── Phase 6 Feature 16 — audit log: recent activity viewer & clear ──
    # Explicit meta-commands, same "checked before classification"
    # treatment as the workflow/health controls above — this is about
    # inspecting/managing JARVIS's own audit trail, not something an
    # LLM needs to interpret.
    _recent_activity_match = (
        re.match(r"^(?:show |get )?recent(?: workflow)?(?: activity)?(?: (\d+))?$", _lower)
        or re.match(r"^(?:show )?(?:the )?audit log(?: (\d+))?$", _lower)
    )
    if _recent_activity_match and _lower not in ("recent", "recently"):
        limit_raw = _recent_activity_match.group(1)
        limit = max(1, min(int(limit_raw), 50)) if limit_raw else 10
        events = get_recent_events(limit=limit, event_type_prefix="workflow:")
        if not events:
            reply = "No recent workflow activity recorded, Sir."
        else:
            lines = [f"RECENT ACTIVITY (last {len(events)}):", ""]
            for e in events:
                lines.append(_format_activity_event(e))
            reply = "\n".join(lines)
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    if _lower in ("clear logs", "clear the logs", "clear audit log", "clear the audit log",
                  "clear activity log", "clear the activity log"):
        reply = 'This will permanently delete the workflow audit log. Say "confirm clear logs" to proceed, Sir.'
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    if _lower in ("confirm clear logs", "confirm clear audit log", "yes clear logs", "yes, clear logs"):
        try:
            count = clear_events()
            reply = (
                f"Cleared {count} audit log entr{'y' if count == 1 else 'ies'}, Sir."
                if count else "The audit log was already empty, Sir."
            )
        except AuditLogError as e:
            reply = f"I couldn't clear the audit log, Sir: {e}"
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    # ── Phase 6 Feature 7 — Proactive Suggestion Engine ───────────
    # Generation is proactive (project_health.py's automatic monitor
    # raises these without being asked, see suggestion_engine.py's
    # docstring for why); retrieval here is on-demand, the same shape
    # Feature 6 already uses for its own non-automatic report.
    if _lower in ("suggestions", "any suggestions", "any suggestions?", "what do you suggest",
                  "what do you suggest?", "show suggestions"):
        ctx = context_manager.gather(session_id=session_id)
        if not ctx.active_project_path:
            reply = "I don't have an active project to base suggestions on, Sir."
        else:
            pending = suggestion_engine.get_pending(ctx.active_project_path)
            if not pending:
                reply = "No suggestions right now, Sir."
            else:
                lines = ["SUGGESTIONS", ""] + [f"- {s.text}" for s in pending]
                reply = "\n".join(lines)
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    if _lower in ("dismiss suggestions", "dismiss all suggestions", "clear suggestions"):
        ctx = context_manager.gather(session_id=session_id)
        if not ctx.active_project_path:
            reply = "I don't have an active project, Sir."
        else:
            count = suggestion_engine.dismiss_all(ctx.active_project_path)
            reply = f"Dismissed {count} suggestion(s), Sir." if count else "There were no pending suggestions, Sir."
        save_message(session_id, "assistant", reply)
        return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    # ── Phase 6 Feature 13 — guided study session answers ─────────
    # If the session's workflow is genuinely WAITING_FOR_USER (the
    # suspend-and-wait-for-user-answer primitive), the raw message IS
    # the student's answer to the quiz question just asked — it must
    # be routed to provide_input() before intent classification, not
    # interpreted as a fresh request. Only reached when the message
    # didn't match any of the exact meta-commands checked above.
    _active_wf = workflow_engine.latest_for_session(session_id)
    if _active_wf is not None and _active_wf.status == WorkflowStatus.WAITING_FOR_USER:
        try:
            result = await workflow_engine.provide_input(_active_wf.id, effective_message, broadcast=manager.broadcast)
        except ValueError:
            result = None
        if result is not None:
            if result.status == WorkflowStatus.WAITING_FOR_USER:
                reply = (result.pending_input or {}).get("prompt", "")
            elif result.status == WorkflowStatus.COMPLETED:
                reply = result.steps[-1].result or result.to_report()
            else:
                reply = (
                    f"The study session didn't finish cleanly, Sir "
                    f"({result.stopped_reason or result.status.value}):\n\n{result.to_report()}"
                )
            save_message(session_id, "assistant", reply)
            return ChatResponse(response=reply, session_id=session_id, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    # ── Backend state: THINKING while classifying + generating ────
    # Wrapped in try/finally so a mid-turn exception can't leave the
    # backend permanently stuck reporting THINKING to the UI forever.
    await state_manager.set_state(JarvisState.THINKING, manager.broadcast)

    try:
        # Classify intent first. GENERAL_CHAT, DEBUG, CODE_ANALYSIS,
        # CODE_EXPLANATION, TERMINAL, SCREEN_ANALYSIS, GIT, and the
        # Phase 5 study/CSE/hackathon/planning/developer-mode intents
        # all have real subsystems behind them — route_intent() falls
        # everything else back to GENERAL_CHAT (see intent_router.py
        # for the current set).
        intent_result = await asyncio.to_thread(classify_intent, effective_message)
        routed = route_intent(intent_result)

        # Section 7 — deterministic override: an explicit screen-
        # directed phrase ("check this", "look at this", "what's wrong
        # here") always reaches the existing SCREEN_ANALYSIS handling,
        # rather than depending on the LLM classifier correctly
        # inferring visual intent from a message as bare as "check
        # this". context_reference.wants_screen was only ever set True
        # by context_engine.wants_screen_context()'s own explicit
        # phrase match above — never a guess.
        if context_reference.wants_screen and context_reference.resolved_kind in (None, "screen") and routed != Intent.SCREEN_ANALYSIS:
            routed = Intent.SCREEN_ANALYSIS

        log_event(
            "intent_classified",
            f"'{request.message[:40]}...' -> {intent_result.intent.value} "
            f"(confidence={intent_result.confidence:.2f}, routed={routed.value})"
        )

        _HANDLED_INTENTS = (
            Intent.DEBUG, Intent.CODE_ANALYSIS, Intent.CODE_EXPLANATION, Intent.TERMINAL,
            Intent.SCREEN_ANALYSIS, Intent.GIT,
            Intent.DSA, Intent.STUDY, Intent.INTERVIEW, Intent.PLANNING,
            Intent.HACKATHON, Intent.DEVELOPER_MODE, Intent.PROJECT_ANALYSIS,
            Intent.SYSTEM_SECURITY, Intent.SYSTEM_STORAGE,
        )
        if routed in _HANDLED_INTENTS:
            reply = await _handle_phase3_intent(routed, intent_result, effective_message, session_id)
        else:
            try:
                # Model routing: this branch is ONLY ever reached when
                # routed == GENERAL_CHAT (traced precisely: route_intent()
                # returns either the original intent, if implemented, or
                # GENERAL_CHAT — and every coding-classified intent IS
                # implemented, so it never falls back here). intent_result
                # .intent at this point is therefore always GENERAL_CHAT
                # itself, or one of the intents with no subsystem yet
                # (PROJECT_MEMORY/FILE_OPERATION/SYSTEM_MONITOR/RESEARCH)
                # that route_intent() downgraded to GENERAL_CHAT — never
                # a coding intent. So this always selects TEXT_MODEL in
                # practice today; it's wired through model_selector
                # anyway (rather than hardcoding TEXT_MODEL) so that if
                # a future intent gets added to this fallback path with
                # coding-flavored semantics, it's correctly routed
                # without touching this call site again.
                selected_model = model_selector.select_model(intent_result.intent)
                reply = await asyncio.to_thread(
                    ask_ollama,
                    request.message,
                    history[:-1],
                    selected_model,
                )
            except Exception as e:
                log_event("error", f"Ollama error: {str(e)}")
                reply = "I seem to be experiencing a momentary lapse in cognition, Sir. Is Ollama running?"
                await state_manager.set_error(manager.broadcast, detail=str(e))

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

        save_message(session_id, "assistant", reply)

        # ERROR is a dead-end state that only recovers via IDLE/LISTENING
        # (see state.py's _ALLOWED_TRANSITIONS) — skip the normal
        # THINKING -> SPEAKING -> IDLE walk if we already errored above.
        if state_manager.state != JarvisState.ERROR:
            await state_manager.set_state(JarvisState.SPEAKING, manager.broadcast)

        await manager.broadcast({
            "type":       "message",
            "role":       "assistant",
            "content":    reply,
            "session_id": session_id,
            "timestamp":  now,
        })

        return ChatResponse(
            response=reply,
            session_id=session_id,
            timestamp=now,
        )
    finally:
        # Always return to IDLE, whether we finished normally, hit the
        # Ollama-error branch above, or something raised outright.
        await state_manager.set_state(JarvisState.IDLE, manager.broadcast, force=True)


_UPLOAD_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}


@app.post("/chat/upload", response_model=ChatResponse)
async def chat_upload(
    message: str = Form(""),
    session_id: str = Form(""),
    file: UploadFile = File(...),
):
    """
    File-attachment counterpart to /chat, for the frontend's upload
    button (see app.js's sendMessageWithFile()). Two real, distinct
    pipelines depending on what was uploaded — Sections 7/8/9/12/13/14:

    - IMAGE (png/jpg/jpeg/webp): the actual image bytes are base64-
      encoded and sent to VISION_MODEL via ask_ollama_vision(), together
      with the user's text and recent conversation history. If a code/
      text file was uploaded earlier in this session and the user's
      message reads like a debugging request, that cached file's text
      is included too (Section 14 — combined image+code debugging).
      If no VISION_MODEL is configured, this returns an explicit,
      honest message rather than pretending to have looked at the
      image (Section 8/25).

    - EVERYTHING ELSE (code/text/pdf/docx): runs the existing
      code_analysis.analyze_file() structural/static check AND
      extracts the real text via code_analysis.extract_text_for_llm()
      so the LLM can actually answer free-form questions about it
      ("summarize this", "what are the requirements") on this and
      later turns — Section 12. The extracted text is cached
      per-session (see _remember_attachment) so those follow-up
      questions work even without re-uploading.

    Contract matches /chat's ChatResponse (response/session_id/
    timestamp) so the frontend needed no shape changes.
    """
    session_id = session_id or str(uuid.uuid4())
    proactive_engine.proactive_engine.note_user_activity()
    filename = file.filename or "uploaded_file"
    ext = os.path.splitext(filename)[1].lower()
    is_image = ext in code_analysis.IMAGE_EXTENSIONS
    now = lambda: datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    user_note = message.strip() or (f"(uploaded {filename})" if not is_image else "What do you see in this image?")
    logged_user_message = f"{user_note} [attached: {filename}]"

    contents = await file.read()

    # Persist an attachment marker in the message's own metadata (small
    # JSON, not the raw bytes) so /history and the frontend can render
    # an attachment tag / image thumbnail against this exact message —
    # Sections 1 & 10. The full image bytes live only in the in-memory
    # _session_attachments cache (see its docstring for why).
    attachment_meta = {"filename": filename, "kind": "image" if is_image else "file"}
    if is_image:
        attachment_meta["data_url_b64"] = base64.b64encode(contents).decode() if len(contents) <= 2_000_000 else None

    save_message(session_id, "user", logged_user_message, metadata={"attachment": attachment_meta})
    await manager.broadcast({
        "type":       "message",
        "role":       "user",
        "content":    logged_user_message,
        "session_id": session_id,
        "timestamp":  now(),
        "attachment": attachment_meta,
    })

    await state_manager.set_state(JarvisState.THINKING, manager.broadcast, detail=(
        "Inspecting the image..." if is_image else "Reading the file..."
    ))

    try:
        if is_image:
            reply = await _handle_image_upload(session_id, filename, contents, message)
        else:
            reply = await _handle_file_upload(session_id, filename, ext, contents, message)
    finally:
        await state_manager.set_state(JarvisState.IDLE, manager.broadcast, force=True)

    save_message(session_id, "assistant", reply)
    await manager.broadcast({
        "type":       "message",
        "role":       "assistant",
        "content":    reply,
        "session_id": session_id,
        "timestamp":  now(),
    })

    return ChatResponse(response=reply, session_id=session_id, timestamp=now())


async def _handle_image_upload(session_id: str, filename: str, contents: bytes, message: str) -> str:
    """Section 7/8/9: the real vision-model path for an uploaded image."""
    if len(contents) > code_analysis._MAX_BINARY_FILE_BYTES:
        return (
            f"That image is too large for me to inspect, Sir — {len(contents):,} bytes "
            f"(limit {code_analysis._MAX_BINARY_FILE_BYTES:,})."
        )

    if not VISION_MODEL:
        # Section 8/25: never fake it. Tell the user exactly what's
        # missing and how to fix it, and don't touch the "engine" field
        # or otherwise imply the image was looked at.
        return (
            "I received the image, Sir, but I can't actually analyze it yet — no "
            "vision-capable model is configured. Set the JARVIS_VISION_MODEL "
            "environment variable to a vision-capable Ollama model (e.g. \"llava\", "
            "\"llama3.2-vision\", or \"bakllava\") and make sure it's pulled "
            "(`ollama pull llava`), then restart the backend."
        )

    ext = os.path.splitext(filename)[1].lower()
    media_type = _UPLOAD_MEDIA_TYPES.get(ext, "image/png")
    b64 = base64.b64encode(contents).decode()

    # Section 14: if there's a recently-uploaded code/text file for this
    # session and the request reads like a debugging ask, hand the model
    # both — the image AND the actual source, not just one or the other.
    extra_context = None
    cached_doc = _recall_attachment(session_id, "document")
    if cached_doc and _mentions_any(message or "find the problem debug", ("code", "debug", "bug", "error", "problem", "fix", "wrong")):
        extra_context = f"File: {cached_doc['filename']}\n{cached_doc['text']}"

    history = get_history(session_id, limit=8)
    try:
        reply = await asyncio.to_thread(
            ask_ollama_vision, message, [b64], history[:-1] if history else [], extra_context,
        )
    except Exception as e:
        log_event("error", f"Vision model call failed for {filename}: {e}")
        return (
            f"I couldn't reach the vision model ({VISION_MODEL}), Sir: {e}. "
            f"Is Ollama running with that model pulled?"
        )

    _remember_attachment(session_id, "image", filename, base64_data=b64, media_type=media_type)
    return reply


async def _handle_file_upload(session_id: str, filename: str, ext: str, contents: bytes, message: str) -> str:
    """Sections 11/12/13: code/text/PDF/docx — structural analysis (as
    before) PLUS real text extraction cached for follow-up Q&A."""
    if len(contents) > code_analysis._MAX_BINARY_FILE_BYTES:
        return (
            f"That file's too large for me to inspect, Sir — {len(contents):,} bytes "
            f"(limit {code_analysis._MAX_BINARY_FILE_BYTES:,})."
        )

    suffix = ext
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name

        try:
            extracted_text = code_analysis.extract_text_for_llm(tmp_path)
        except (FileNotFoundError, ValueError) as e:
            extracted_text = None
            extract_error = str(e)
        else:
            extract_error = None
            _remember_attachment(session_id, "document", filename, text=extracted_text)

        # If the user actually asked something (summarize/explain/
        # requirements/design/etc.) and we have real extracted text,
        # answer it with the LLM against that text — Section 12 — rather
        # than only returning the static structural analysis. A bare
        # upload with no question still gets the structural analysis
        # (Section 11's "show upload/processing state" + something
        # concrete back), same as before.
        user_asked_something = bool(message.strip())

        if user_asked_something and extracted_text:
            history = get_history(session_id, limit=8)
            prompt = (
                f"The user uploaded a file named '{filename}'. Its extracted contents "
                f"are below. Answer the user's request using ONLY this content as the "
                f"source of truth; say plainly if something isn't in it rather than "
                f"inventing detail.\n\n--- FILE CONTENTS ---\n{extracted_text}\n"
                f"--- END FILE CONTENTS ---"
            )
            try:
                reply = await asyncio.to_thread(
                    ask_ollama, f"{message}\n\n{prompt}", history[:-1] if history else [],
                )
            except Exception as e:
                log_event("error", f"LLM call over uploaded file failed for {filename}: {e}")
                reply = f"I read the file, Sir, but couldn't reason over it: {e}"
            return reply

        # No question asked (or no extractable text) — fall back to the
        # structural check, same behavior as before this upgrade.
        try:
            analysis = code_analysis.analyze_file(tmp_path)
            reply = analysis.to_text()
        except (FileNotFoundError, ValueError) as e:
            reply = f"I couldn't analyze that, Sir: {e}"
        except Exception as e:
            log_event("error", f"File analysis failed for {filename}: {e}")
            reply = f"Something went wrong while inspecting that file, Sir: {e}"

        if extract_error and not extracted_text:
            reply += f"\n\n(Note: I also couldn't extract readable text for follow-up questions: {extract_error})"
        return reply
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.get("/history")
async def history(session_id: Optional[str] = None, limit: int = 50):
    if session_id:
        messages = get_history(session_id, limit=limit)
    else:
        messages = get_all_history(limit=limit)
    return {"messages": messages, "count": len(messages)}


@app.get("/sessions")
async def sessions():
    return {"sessions": list_sessions()}


@app.post("/sessions/clear")
async def clear_history(request: ClearRequest):
    clear_session_history(request.session_id)
    log_event("clear", f"History cleared for session {request.session_id}")
    return {"status": "cleared", "session_id": request.session_id}


@app.get("/tools")
async def list_tools():
    """
    List every registered tool with its permission level and whether
    it's actually implemented yet — see tool_registry.py.
    """
    return {
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "permission": spec.permission.value,
                "implemented": spec.implemented,
            }
            for spec in tool_registry.list_tools()
        ]
    }


class RunToolRequest(BaseModel):
    tool: str
    args: dict = {}


@app.post("/tools/run")
async def run_tool_endpoint(request: RunToolRequest):
    """
    Run a tool through the permission-aware registry. SAFE tools run
    immediately; CONFIRM tools return pending_confirmation and must be
    resolved via /confirmations/{id} before they actually execute;
    BLOCKED tools never run.
    """
    await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast)
    try:
        result = await tool_registry.run_tool(request.tool, request.args, broadcast=manager.broadcast)
        log_event("tool_run", f"{request.tool} -> {result.get('status')}")
        return result
    finally:
        await state_manager.set_state(JarvisState.IDLE, manager.broadcast, force=True)


class ResolveConfirmationRequest(BaseModel):
    approved: bool


@app.post("/confirmations/{confirmation_id}")
async def resolve_confirmation_endpoint(confirmation_id: str, request: ResolveConfirmationRequest):
    """
    User's Allow/Deny response to a pending tool confirmation (see
    Section 8's "JARVIS wants to run X — Allow / Deny" flow).
    Approving does NOT automatically re-run the tool — the caller
    (frontend) re-issues /tools/run with auto_approved semantics by
    calling this, then run_tool with the now-approved confirmation on
    record. Kept as two explicit steps so an approval can never
    silently trigger execution without a fresh, deliberate re-request.
    """
    confirmation = permission_manager.resolve_confirmation(confirmation_id, request.approved)
    if confirmation is None:
        raise HTTPException(status_code=404, detail="Unknown confirmation id")

    log_event(
        "confirmation_resolved",
        f"{confirmation.tool_name} ({confirmation_id}) -> "
        f"{'approved' if request.approved else 'denied'}"
    )

    result = {"id": confirmation_id, "tool": confirmation.tool_name, "approved": request.approved}

    if request.approved:
        await state_manager.set_state(JarvisState.EXECUTING, manager.broadcast)
        try:
            run_result = await tool_registry.run_tool(
                confirmation.tool_name,
                confirmation.args,
                broadcast=manager.broadcast,
                auto_approved=True,
            )
            result["execution"] = run_result
        finally:
            await state_manager.set_state(JarvisState.IDLE, manager.broadcast, force=True)

    return result


@app.get("/state")
async def get_backend_state():
    """Current backend state machine snapshot — see state.py."""
    return state_manager.as_dict()


@app.get("/events")
async def events(limit: int = 20):
    return {"events": get_recent_events(limit=limit)}


# ── Proactive Intelligence (Section 19: minimal ON/OFF control) ────
class ProactiveToggleRequest(BaseModel):
    enabled: bool


@app.get("/proactive/status")
async def proactive_status():
    eng = proactive_engine.proactive_engine
    return {"enabled": eng.enabled, "running": eng.is_running()}


@app.post("/proactive/toggle")
async def proactive_toggle(request: ProactiveToggleRequest):
    eng = proactive_engine.proactive_engine
    enabled = eng.set_enabled(request.enabled)
    log_event("proactive_toggle", f"Proactive intelligence {'enabled' if enabled else 'disabled'}")
    return {"enabled": enabled}


@app.get("/proactive/events")
async def proactive_events(limit: int = 20):
    """Recent proactive events — mainly a debugging/inspection aid
    (Section 26); the durable record is memory.log_event(), same as
    every other JARVIS event."""
    return {"events": proactive_engine.proactive_engine.recent_events(limit=limit)}


# ── Context Awareness (Section 21: developer/debug inspection) ─────
@app.get("/context/debug")
async def context_debug(session_id: str = "default", include_screen: bool = False):
    """
    Section 21 — "create a way to inspect the current context
    internally... developer/debug functionality." Deliberately a plain
    REST endpoint, not a UI panel (Section 24 explicitly rules out a
    new dashboard). include_screen=True triggers exactly one on-demand
    screen_tools.analyze_screen() call, same as any other explicit
    screen-analysis request — never automatic.
    """
    ctx = context_engine.context_engine.gather(session_id, include_screen=include_screen)
    return {"context": ctx.to_dict(), "debug_text": ctx.to_debug_text()}


@app.get("/stats")
async def stats():
    return get_stats()


# ── Voice Transcript Endpoint ──────────────────────────────
@app.post("/voice/transcript")
async def voice_transcript(request: TranscriptRequest):
    """Receives voice transcripts from agent and broadcasts to UI."""
    session_id = "voice-session"

    # Section 20 — an active voice turn is the highest-priority form of
    # "actively interacting"; hold back low-priority (INFO) proactive
    # notifications for a bit longer here than for a typed message.
    proactive_engine.proactive_engine.note_user_activity(quiet_seconds=25.0)

    save_message(session_id, request.role, request.content)

    await manager.broadcast({
        "type":       "message",
        "role":       request.role,
        "content":    request.content,
        "session_id": session_id,
        "timestamp":  datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    })

    log_event(
        "voice_transcript",
        f"{request.role}: {request.content[:50]}..."
    )


class AgentStateRequest(BaseModel):
    state: str


@app.post("/voice/state")
async def voice_state(request: AgentStateRequest):
    """
    Receives real agent processing state ("listening"/"thinking"/
    "speaking") from agent.py and broadcasts it so the frontend can show
    live feedback while STT/LLM are running. Without this, a slow-but-
    legitimate multi-second STT/LLM pass looks identical to JARVIS not
    having heard you at all — nothing distinguishes "still working" from
    "broken" in the UI.
    """
    await manager.broadcast({
        "type":  "voice_state",
        "state": request.state,
    })
    return {"status": "ok"}


@app.post("/agent/restart")
async def restart_agent():
    stop_agent()
    await asyncio.sleep(1)
    start_agent()
    log_event("agent_restart", "Agent manually restarted via API")
    return {
        "status":  "restarted",
        "pid":     agent_status["pid"],
        "message": "Agent restarted, Sir."
    }


@app.post("/agent/stop")
async def stop_agent_endpoint():
    stop_agent()
    return {"status": "stopped", "message": "Agent stopped, Sir."}


@app.post("/agent/start")
async def start_agent_endpoint():
    if agent_status["running"]:
        return {"status": "already_running", "pid": agent_status["pid"]}
    start_agent()
    return {
        "status":  "started",
        "pid":     agent_status["pid"],
        "message": "Agent started, Sir."
    }


# ── LiveKit Token Endpoint ─────────────────────────────────
def _livekit_http_url(ws_url: str) -> str:
    """LiveKitAPI talks HTTP; .env usually stores the browser WebSocket URL."""
    if not ws_url:
        return ws_url
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[len("wss://"):]
    if ws_url.startswith("ws://"):
        return "http://" + ws_url[len("ws://"):]
    return ws_url


@app.get("/livekit/token")
async def get_livekit_token(
    room:     str = "jarvis-room",
    identity: str = "user"
):
    try:
        from livekit.api import AccessToken, VideoGrants, LiveKitAPI
        from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest

        # Generate user token — must explicitly allow publish or the
        # browser can join the room while the mic track never reaches
        # the agent (UI stuck on LISTENING, "audio not got by it").
        token = (
            AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(identity)
            .with_name("JARVIS User")
            .with_grants(VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            ))
        )
        jwt = token.to_jwt()

        # Match agent.py: local LiveKit uses an unnamed auto-accept
        # worker; Cloud / non-local uses named "Jarvis" + explicit dispatch.
        lk_url_l = (LIVEKIT_URL or "").lower()
        is_local = "localhost" in lk_url_l or "127.0.0.1" in lk_url_l
        agent_name = os.getenv("JARVIS_AGENT_NAME")
        if agent_name is None:
            agent_name = "" if is_local else "Jarvis"
        agent_name = agent_name.strip()

        agent_dispatched = False
        dispatch_error = None
        try:
            lkapi = LiveKitAPI(
                url=_livekit_http_url(LIVEKIT_URL),
                api_key=LIVEKIT_API_KEY,
                api_secret=LIVEKIT_API_SECRET,
            )

            dispatch_req = CreateAgentDispatchRequest()
            dispatch_req.room = room
            # Named workers only accept matching dispatches; unnamed
            # local workers accept a dispatch with no agent_name set.
            if agent_name:
                dispatch_req.agent_name = agent_name

            await lkapi.agent_dispatch.create_dispatch(dispatch_req)
            agent_dispatched = True

            print(
                f"[JARVIS] Agent dispatched to room: {room}"
                + (f" (name={agent_name})" if agent_name else " (auto/unnamed worker)")
            )
            log_event("agent_dispatch", f"Agent dispatched to room {room}")
            await lkapi.aclose()

        except Exception as dispatch_err:
            dispatch_error = str(dispatch_err)
            # On local auto mode, a failed dispatch is still fatal for
            # self-hosted LiveKit — surface it clearly.
            print(f"[JARVIS] Dispatch warning: {dispatch_err}")

        return {
            "token":            jwt,
            "url":              LIVEKIT_URL,
            "room":             room,
            "identity":         identity,
            "agent_dispatched": agent_dispatched,
            "dispatch_error":   dispatch_error,
        }

    except Exception as e:
        log_event("error", f"LiveKit token error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ── WebSocket: Live Conversation Panel ─────────────────────
@app.websocket("/ws/conversation")
async def websocket_conversation(websocket: WebSocket):
    await manager.connect(websocket)
    session_id = str(uuid.uuid4())

    try:
        await manager.send_personal(websocket, {
            "type":       "connected",
            "message":    "JARVIS interface connected, Sir.",
            "session_id": session_id,
            "timestamp":  datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        })

        while True:
            data     = await websocket.receive_json()
            msg_type = data.get("type", "message")

            if msg_type == "ping":
                await manager.send_personal(websocket, {
                    "type":      "pong",
                    "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                })

            elif msg_type == "message":
                content    = data.get("content", "")
                session_id = data.get("session_id", session_id)

                if content.strip():
                    save_message(session_id, "user", content)
                    await manager.broadcast({
                        "type":       "message",
                        "role":       "user",
                        "content":    content,
                        "session_id": session_id,
                        "timestamp":  datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                    })

                    history_msgs = get_history(session_id, limit=10)

                    try:
                        reply = await asyncio.to_thread(
                            ask_ollama,
                            content,
                            history_msgs[:-1],
                        )
                    except Exception as e:
                        reply = "My circuits appear to be momentarily indisposed, Sir."
                        log_event("error", f"Ollama WS error: {str(e)}")

                    save_message(session_id, "assistant", reply)
                    await manager.broadcast({
                        "type":       "message",
                        "role":       "assistant",
                        "content":    reply,
                        "session_id": session_id,
                        "timestamp":  datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                    })

            elif msg_type == "status":
                agent_alive = (
                    agent_process is not None and
                    agent_process.poll() is None
                )
                await manager.send_personal(websocket, {
                    "type":      "status",
                    "backend":   "online",
                    "agent":     "online" if agent_alive else "offline",
                    "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                })

    except WebSocketDisconnect:
        manager.disconnect(websocket)
        print("[JARVIS] WebSocket client disconnected.")
    except Exception as e:
        manager.disconnect(websocket)
        log_event("error", f"WebSocket error: {str(e)}")


# ── Frontend (served by the same process as the API) ───────
# Mounted last so every explicit API / WS route above wins.
if os.path.isdir(FRONTEND_DIR):
    app.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIR, html=True),
        name="frontend",
    )
else:
    print(f"[JARVIS] WARNING: frontend directory not found at {FRONTEND_DIR}")


# ── Entry Point ────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=BACKEND_PORT,
        reload=False,
        log_level="info",
    )