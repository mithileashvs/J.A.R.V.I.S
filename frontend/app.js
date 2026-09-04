// ── CONFIG ─────────────────────────────────────────────
const BACKEND_URL = "http://localhost:8000";
const WS_URL      = "ws://localhost:8000/ws/conversation";

// ── STATE ──────────────────────────────────────────────
let sessionId        = null;
let ws               = null;
let wsReconnectTimer = null;
let voiceActive      = false;
let livekitRoom      = null;
let audioContext     = null;
let analyser         = null;
let micStream        = null;
let waveformAnimId   = null;
let statusInterval   = null;
let statsInterval    = null;
let eventsInterval   = null;
let typewriterInterval = null;
let jarvisSpeaking   = false;  // ── FIX 4: track when JARVIS is speaking

// ── Section 5: MARKDOWN RENDERING ──────────────────────
// marked.js parses Markdown -> HTML, DOMPurify sanitizes that HTML
// before it's ever inserted into the DOM (never trust rendered HTML,
// even from our own backend), highlight.js syntax-highlights code
// blocks. All three are optional (loaded from cdnjs in index.html) —
// if any failed to load (offline dev environment, blocked CDN, etc.)
// this degrades to plain escaped text rather than throwing.
let _markedReady = false;
function _ensureMarkedConfigured() {
    if (_markedReady || typeof marked === "undefined") return;
    marked.setOptions({
        breaks: true,
        gfm: true,
        highlight: function (code, lang) {
            if (typeof hljs === "undefined") return code;
            try {
                if (lang && hljs.getLanguage(lang)) {
                    return hljs.highlight(code, { language: lang }).value;
                }
                return hljs.highlightAuto(code).value;
            } catch (e) {
                return code;
            }
        },
    });
    _markedReady = true;
}

function _escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

// Renders `text` as sanitized Markdown HTML. Falls back to plain
// (escaped, newline-preserving) text if marked/DOMPurify aren't
// available. Used for JARVIS's own replies (bubble text + response
// panel) — user messages are always rendered as plain text (see
// addChatBubble), since nothing the user types needs Markdown parsing.
function renderMarkdown(text) {
    if (typeof marked === "undefined") {
        return `<span class="md-plain">${_escapeHtml(text)}</span>`;
    }
    _ensureMarkedConfigured();
    let html;
    try {
        html = marked.parse(text || "");
    } catch (e) {
        return `<span class="md-plain">${_escapeHtml(text)}</span>`;
    }
    if (typeof DOMPurify !== "undefined") {
        html = DOMPurify.sanitize(html, { ADD_ATTR: ["target"] });
    }
    return html;
}

// After Markdown HTML has been inserted into `container`, attach a
// copy button to every fenced code block (Section 5: "code blocks
// should support ... a copy button"). Safe to call repeatedly — it
// skips blocks that already have one.
function enhanceCodeBlocks(container) {
    if (!container) return;
    container.querySelectorAll("pre > code").forEach((codeEl) => {
        const pre = codeEl.parentElement;
        if (!pre || pre.dataset.enhanced) return;
        pre.dataset.enhanced = "1";
        pre.classList.add("code-block-wrap");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "code-copy-btn";
        btn.textContent = "Copy";
        btn.setAttribute("aria-label", "Copy code");
        btn.addEventListener("click", () => {
            navigator.clipboard.writeText(codeEl.textContent || "").then(() => {
                btn.textContent = "Copied";
                setTimeout(() => { btn.textContent = "Copy"; }, 1500);
            }).catch(() => {
                btn.textContent = "Failed";
                setTimeout(() => { btn.textContent = "Copy"; }, 1500);
            });
        });
        pre.appendChild(btn);
    });
}

// ── Section 10: IMAGE LIGHTBOX (click-to-expand) ───────
function openImageLightbox(src, label) {
    let overlay = document.getElementById("image-lightbox");
    if (!overlay) {
        overlay = document.createElement("div");
        overlay.id = "image-lightbox";
        overlay.className = "image-lightbox";
        overlay.innerHTML = `
            <div class="image-lightbox-backdrop"></div>
            <div class="image-lightbox-content">
                <img id="image-lightbox-img" alt="" />
                <div class="image-lightbox-caption" id="image-lightbox-caption"></div>
                <button type="button" class="image-lightbox-close" aria-label="Close">×</button>
            </div>`;
        document.body.appendChild(overlay);
        overlay.querySelector(".image-lightbox-backdrop").addEventListener("click", closeImageLightbox);
        overlay.querySelector(".image-lightbox-close").addEventListener("click", closeImageLightbox);
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") closeImageLightbox();
        });
    }
    document.getElementById("image-lightbox-img").src = src;
    document.getElementById("image-lightbox-caption").textContent = label || "";
    overlay.classList.add("open");
}
function closeImageLightbox() {
    const overlay = document.getElementById("image-lightbox");
    if (overlay) overlay.classList.remove("open");
}

// ── JARVIS-STYLE SOUND CUES ────────────────────────────
// Short synthesized chimes via Web Audio API — no audio files needed.
// A distinct sound on connect/disconnect and before/after JARVIS speaks
// sells the "system" feeling much more than the voice alone.
let _chimeCtx = null;
function _getChimeCtx() {
    if (!_chimeCtx) {
        _chimeCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    return _chimeCtx;
}
function playChime(kind) {
    try {
        const ctx = _getChimeCtx();
        const now = ctx.currentTime;
        // [freq Hz, start offset, duration] pairs per cue — short,
        // clean two-tone blips rather than anything melodic/jingly.
        const patterns = {
            connect:    [[660, 0, 0.09], [880, 0.10, 0.12]],   // rising — "online"
            disconnect: [[660, 0, 0.10], [440, 0.10, 0.14]],   // falling — "offline"
            listenStart:[[520, 0, 0.06]],                       // single soft tick
            speakStart: [[740, 0, 0.05]],                       // single soft tick, higher
        };
        const seq = patterns[kind];
        if (!seq) return;
        for (const [freq, offset, dur] of seq) {
            const osc  = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = "sine";
            osc.frequency.value = freq;
            gain.gain.setValueAtTime(0, now + offset);
            gain.gain.linearRampToValueAtTime(0.06, now + offset + 0.01);
            gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + dur);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(now + offset);
            osc.stop(now + offset + dur + 0.02);
        }
    } catch (e) {
        console.warn("[CHIME] Could not play sound cue:", e);
    }
}

// ── INIT ───────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    generateSessionId();
    startClock();
    initParticles();
    initWaveform();
    connectWebSocket();
    startStatusPolling();
    buildRingTicks();
    loadConversationHistory();
    initProactiveToggle();

    // Section 21: hide the "new messages" pill once the user scrolls
    // back down near the bottom themselves.
    const convList = document.getElementById("conversation-list");
    if (convList) {
        convList.addEventListener("scroll", () => {
            const nearBottom = (convList.scrollHeight - convList.scrollTop - convList.clientHeight) < 60;
            if (nearBottom) hideNewMessagesIndicator();
        });
    }
});

// ── SESSION ────────────────────────────────────────────
function generateSessionId() {
    sessionId = "sess-" + Math.random().toString(36).substr(2, 9);
    const el = document.getElementById("session-id-display");
    if (el) el.textContent = sessionId.toUpperCase();
}

// ── CLOCK ──────────────────────────────────────────────
function startClock() {
    function tick() {
        const now  = new Date();
        const time = now.toTimeString().split(" ")[0];
        const date = now.toLocaleDateString("en-GB", {
            day:   "2-digit",
            month: "short",
            year:  "numeric",
        }).toUpperCase();
        const clockEl = document.getElementById("clock-display");
        const dateEl  = document.getElementById("date-display");
        if (clockEl) clockEl.textContent = time;
        if (dateEl)  dateEl.textContent  = date;
    }
    tick();
    setInterval(tick, 1000);
}

// ── WEBSOCKET ──────────────────────────────────────────
function connectWebSocket() {
    clearTimeout(wsReconnectTimer);
    try {
        ws = new WebSocket(WS_URL);
    } catch (e) {
        setWsStatus("OFFLINE");
        scheduleReconnect();
        return;
    }

    ws.onopen = () => {
        setWsStatus("ONLINE");
        addLogEntry("WebSocket connected.", "success");
        ws.send(JSON.stringify({ type: "ping" }));
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleWsMessage(data);
        } catch (e) {
            console.error("[JARVIS] WS parse error:", e);
        }
    };

    ws.onerror = () => {
        setWsStatus("ERROR");
        addLogEntry("WebSocket error.", "error");
    };

    ws.onclose = () => {
        setWsStatus("RECONNECTING");
        addLogEntry("WebSocket disconnected. Reconnecting...", "error");
        scheduleReconnect();
    };
}

function scheduleReconnect() {
    wsReconnectTimer = setTimeout(() => {
        addLogEntry("Attempting reconnect...", "info");
        connectWebSocket();
    }, 4000);
}

function handleWsMessage(data) {
    switch (data.type) {

        case "connected":
            addLogEntry("JARVIS interface ready.", "success");
            break;

        case "pong":
            break;

        case "message":
            // ── FIX 3: All messages go to conversation panel ──
            if (data.role === "user") {
                addChatBubble("user", data.content, data.timestamp, null, data.attachment || null);
            } else if (data.role === "assistant") {
                addChatBubble("assistant", data.content, data.timestamp);
                // Update response panel for voice messages too
                updateResponsePanel(data.content);
                setResponseStatus("STANDBY");
                // NOTE: this event fires when the TEXT transcript of
                // JARVIS's reply arrives over the WebSocket — that only
                // proves a response was GENERATED, not that TTS audio
                // has started playing. Deliberately NOT touching
                // jarvisSpeaking/setVoiceState here anymore. The actual
                // "JARVIS SPEAKING" state is now driven solely by the
                // real <audio> element's `playing`/`ended`/`error`
                // events (see activateVoice()'s TrackSubscribed
                // handler) — that was bug #4: the button used to flip to
                // "JARVIS SPEAKING" here, based on text arrival, via a
                // blind 3-second timeout that had no relationship to
                // whether audio was actually playing or how long it
                // actually took.
            }
            break;

        case "status":
            updateStatusFromData(data);
            break;

        case "backend_state":
            // Real backend state machine broadcast from state.py —
            // distinct from "voice_state" above (which is the LiveKit
            // voice agent's own turn-scoped state). This fires for
            // every JarvisState transition AND every same-state detail
            // update, which is what makes multi-step progress (a debug
            // investigation's "step 3/8: analyzing code") visible here
            // instead of the UI just freezing on step 1 — see
            // state.py's set_state() docstring for why same-state
            // calls are allowed through.
            updateBackendProgress(data);
            break;

        case "voice_state":
            // Real agent-side state pushed from agent.py's
            // agent_state_changed event via the backend. This is the
            // FRAMEWORK'S OWN turn-scoped notion of speaking — accurate
            // per-reply, unlike trying to infer it from the browser's
            // local <audio> element (that track is one continuous
            // connection-lifetime stream, so its `playing` event only
            // fires once at connect time and never again — which is
            // exactly why "JARVIS SPEAKING" appeared disconnected from
            // any actual reply being spoken). This is now the primary
            // driver for both "thinking" and "speaking".
            if (!voiceActive) break;
            if (data.state === "thinking") {
                setVoiceState("THINKING");
            } else if (data.state === "speaking") {
                jarvisSpeaking = true;
                setVoiceState("SPEAKING");
                playChime("speakStart");
            } else if (data.state === "listening") {
                jarvisSpeaking = false;
                setVoiceState("LISTENING");
                playChime("listenStart");
            }
            break;

        case "system_alert":
            // Legacy alias — the old SystemHealthMonitor emitted this
            // type; superseded by "proactive_event" below, but handled
            // the same way in case anything still sends it.
        case "proactive_event":
            // New — proactive_engine.py's SYSTEM/SECURITY/STORAGE/VOICE
            // detectors, with real debouncing/cooldown/severity/
            // recovery already applied server-side (see
            // proactive_engine.py's docstring). Never fired on a fixed
            // timer regardless of conditions.
            handleProactiveNotification(normalizeProactiveEvent(data));
            break;

        case "background_task_notification":
            // Pre-existing (background_tasks.py's TaskManager) —
            // DEVELOPMENT category build/test/dev-server failures.
            // This broadcast already existed; it simply had nothing on
            // the frontend rendering it until now.
            handleProactiveNotification(normalizeBackgroundTaskEvent(data));
            break;

        case "health_alert":
            // Pre-existing (project_health.py's ProjectHealthMonitor) —
            // PROJECT category (git/test/dependency signals). Same
            // situation: already implemented server-side, never
            // surfaced client-side until now.
            handleProactiveNotification(normalizeHealthAlertEvent(data));
            break;

        case "active_mode":
            // Section 15 — "ACTIVE MODE should become SYSTEM" while a
            // security/storage request runs, reverting afterward. Real
            // backend-driven text swap, not a client-side guess.
            {
                const el = document.getElementById("active-mode-value");
                if (el) el.textContent = data.mode || "ASSISTANT";
            }
            break;

        case "confirmation_request":
            // A CONFIRM-tier tool (Section 13: terminal commands, git
            // commits, screen capture, and now system_security_scan /
            // system_clean_junk) is waiting on the user. This already
            // fired from the backend for every such tool before this
            // change — it just had nothing listening for it, so the
            // only way to approve/deny was never actually reachable
            // from the UI. Rendered as a plain assistant bubble with
            // two buttons, using the existing conversation panel per
            // Section 15 ("use the existing JARVIS response/
            // conversation system"), not a new dashboard.
            renderConfirmationRequest(data);
            break;

        default:
            break;
    }
}

function setWsStatus(status) {
    const el = document.getElementById("status-ws");
    if (!el) return;
    el.textContent = status;
    el.className   = "info-value " + (
        status === "ONLINE"       ? "online"  :
        status === "RECONNECTING" ? "warning" : "offline"
    );
}

// ── FIX 4: Visual voice state ─────────────────────────
function setVoiceState(state) {
    const label = document.getElementById("voice-label");
    const btn   = document.getElementById("voice-btn");
    // Redesign: mirrors the same state onto the small "VOICE STATUS"
    // readout below the reactor (see index.html's .reactor-meta) —
    // purely a second display of the same real state voice-label
    // already carries, not a separate/fake status.
    const statusReadout = document.getElementById("voice-status-value");
    if (statusReadout) statusReadout.textContent = state;
    if (!label || !btn) return;

    label.textContent = state;

    btn.classList.remove("active", "speaking", "thinking");
    if (state === "LISTENING")       btn.classList.add("active");
    if (state === "SPEAKING") btn.classList.add("speaking");
    if (state === "THINKING") btn.classList.add("thinking");
}

// ── STATUS POLLING ─────────────────────────────────────
function startStatusPolling() {
    fetchStatus();
    fetchStats();
    fetchEvents();
    statusInterval = setInterval(fetchStatus, 5000);
    statsInterval  = setInterval(fetchStats,  10000);
    eventsInterval = setInterval(fetchEvents, 8000);
}

async function fetchStatus() {
    try {
        const res  = await fetch(`${BACKEND_URL}/status`);
        const data = await res.json();
        updateStatusFromData(data);
    } catch (e) {
        setOfflineStatus();
    }
}

function updateStatusFromData(data) {
    setStatusValue("status-backend", data.backend || "offline");
    setStatusValue("status-agent",   data.agent   || "offline");
    setStatusValue("status-livekit", data.livekit || "offline");
    setStatusValue("status-gemini",  data.gemini  || "offline");

    const pid = document.getElementById("status-pid");
    if (pid) pid.textContent = data.agent_pid || "—";

    const dot = document.getElementById("dot-backend");
    if (dot) {
        dot.className = "status-dot " + (
            data.backend === "online" ? "online" : "offline"
        );
    }

    // Section 8 / redesign: reflect the real backend/vision state
    // already returned by /status onto the Core Modules list, instead
    // of a static "ONLINE" label for everything. No new data is
    // invented — Conversational/Code/System modules track the same
    // `backend` signal every other status row already uses, and Vision
    // Analyzer tracks the real `vision_available` flag (true only when
    // JARVIS_VISION_MODEL is actually configured).
    const backendUp = data.backend === "online";
    setModuleStatus("mod-conversational", backendUp);
    setModuleStatus("mod-code",           backendUp);
    setModuleStatus("mod-vision",         !!data.vision_available);
    setModuleStatus("mod-architect",      backendUp);
    setModuleStatus("mod-files",          backendUp);
}

function setModuleStatus(id, isOnline) {
    const row = document.getElementById(id);
    if (!row) return;
    const dot   = row.querySelector(".mod-dot");
    const label = row.querySelector(".mod-status-label");
    if (dot)   dot.className = "mod-dot " + (isOnline ? "online" : "offline");
    if (label) label.textContent = isOnline ? "ONLINE" : "OFFLINE";
}

function setStatusValue(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value.toUpperCase();
    el.className   = "info-value " + (
        value === "online"      || value === "configured" ? "online"  :
        value === "offline"     || value === "error"      ? "offline" :
        value === "not configured"                        ? "warning" : ""
    );
}

function setOfflineStatus() {
    ["status-backend","status-agent","status-livekit","status-gemini"].forEach(id => {
        setStatusValue(id, "offline");
    });
    ["mod-conversational","mod-code","mod-vision","mod-architect","mod-files"].forEach(id => {
        setModuleStatus(id, false);
    });
}

async function fetchStats() {
    try {
        const res  = await fetch(`${BACKEND_URL}/stats`);
        const data = await res.json();
        const total    = document.getElementById("stat-total");
        const sessions = document.getElementById("stat-sessions");
        const today    = document.getElementById("stat-today");
        if (total)    total.textContent    = data.total_messages || 0;
        if (sessions) sessions.textContent = data.total_sessions || 0;
        if (today)    today.textContent    = data.messages_today || 0;
    } catch (e) {}
}

async function fetchEvents() {
    try {
        const res  = await fetch(`${BACKEND_URL}/events?limit=15`);
        const data = await res.json();
        renderEvents(data.events || []);
    } catch (e) {}
}

function renderEvents(events) {
    const list = document.getElementById("events-list");
    if (!list) return;
    list.innerHTML = "";
    events.slice().reverse().forEach(ev => {
        const entry = document.createElement("div");
        entry.className = "log-entry " + (
            ev.event_type === "error"        ? "error"   :
            ev.event_type === "agent_start"  ||
            ev.event_type === "system_start" ? "success" : "info"
        );
        const time = new Date(ev.timestamp).toLocaleTimeString();
        entry.textContent = `[${time}] ${ev.message}`;
        list.appendChild(entry);
    });
}

// ── CHAT ───────────────────────────────────────────────
async function sendMessage() {
    const input = document.getElementById("chat-input");
    if (!input) return;
    const message = input.value.trim();
    const file    = selectedFile;

    // Unchanged behavior: nothing to send, do nothing. Now also covers
    // the file-only case (empty text is fine as long as a file is attached).
    if (!message && !file) return;

    input.value = "";

    // FIX: render the user's own bubble immediately instead of relying
    // solely on the websocket echo. POST /chat already broadcasts this
    // same message back over the socket with role:"user" (see main.py's
    // chat() handler) — addChatBubble()'s recent-message guard absorbs
    // that duplicate. Doing it here means the Live Conversation panel
    // still shows what was typed even if the socket is momentarily
    // disconnected/reconnecting, which it previously would not.
    // Section 6/10: for an image, pass along the local preview data URL
    // (captured at selection time — see handleFileSelect) so the
    // outgoing bubble shows the actual image immediately, not just a
    // filename, without waiting on the round trip.
    const isImage = file && IMAGE_FILE_EXTENSIONS.includes("." + (file.name.split(".").pop() || "").toLowerCase());
    const localAttachment = file
        ? (isImage
            ? { kind: "image", filename: file.name, data_url_b64: selectedFilePreviewB64, media_type: file.type || "image/png" }
            : { kind: "file", filename: file.name })
        : null;

    addChatBubble(
        "user",
        message || (isImage ? `What do you see in this image, Sir?` : `Analyze this file, Sir: ${file.name}`),
        new Date().toISOString(),
        null,
        localAttachment
    );

    setResponseStatus("PROCESSING", true);
    setResponseThinking(
        file ? (isImage ? "Inspecting the image, Sir..." : "Receiving and reading your file, Sir...")
             : "Processing your request, Sir..."
    );

    if (file) {
        await sendMessageWithFile(message, file);
        return;
    }

    try {
        const res = await fetch(`${BACKEND_URL}/chat`, {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({ message, session_id: sessionId }),
        });
        const data = await res.json();
        updateResponsePanel(data.response);
        // FIX: the assistant's reply was never added to the Live
        // Conversation panel for typed commands — main.py's /chat only
        // ever broadcasts the USER half of the exchange over the
        // websocket (voice replies get there via a separate
        // /voice/transcript call that broadcasts both roles). Adding it
        // here client-side keeps this a frontend-only fix.
        addChatBubble("assistant", data.response, data.timestamp);
        setResponseStatus("STANDBY");
        if (data.session_id) sessionId = data.session_id;
    } catch (e) {
        updateResponsePanel("I seem to be unable to reach my cognitive centres, Sir.");
        setResponseStatus("ERROR");
        addLogEntry("Chat request failed.", "error");
    }
}

function handleKey(event) {
    if (event.key === "Enter") sendMessage();
}

function quickCmd(text) {
    const input = document.getElementById("chat-input");
    if (input) {
        input.value = text;
        sendMessage();
    }
}

// ── FILE UPLOAD ─────────────────────────────────────────
// Frontend-only state for the file currently attached to the command
// input (see index.html's #file-input / .upload-btn / #file-chip).
let selectedFile = null;
// Section 6/10: base64 (no data: prefix) of the selected image, read
// client-side via FileReader at selection time so the command bar and
// the outgoing chat bubble can show a real thumbnail before the
// backend round-trip completes.
let selectedFilePreviewB64 = null;

// main.py's POST /chat/upload now runs uploads through one of two real
// pipelines: images go to the vision model (main.py's
// _handle_image_upload -> ask_ollama_vision), everything else goes
// through code_analysis.py's structural analysis and, if the user
// asked a question, real text extraction fed to the LLM
// (main.py's _handle_file_upload). Each category has its own size cap
// on the backend (code_analysis._MAX_FILE_BYTES /
// _MAX_BINARY_FILE_BYTES) — mirrored here so the UI rejects an
// oversized file before it's even sent.
const CODE_FILE_EXTENSIONS = [
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".txt", ".md",
    ".c", ".cpp", ".h", ".hpp", ".java", ".html", ".css",
    ".yml", ".yaml", ".log",
];
const IMAGE_FILE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp"];
// Legacy .doc is deliberately excluded — python-docx (the backend
// reader) only handles modern .docx, and there's no reliable
// pure-Python reader for the old binary format.
const BINARY_FILE_EXTENSIONS = [...IMAGE_FILE_EXTENSIONS, ".pdf", ".docx"];
const ALLOWED_FILE_EXTENSIONS = [...CODE_FILE_EXTENSIONS, ...BINARY_FILE_EXTENSIONS];

const MAX_CODE_FILE_SIZE_BYTES   = 500 * 1024;        // matches code_analysis._MAX_FILE_BYTES
const MAX_BINARY_FILE_SIZE_BYTES = 5 * 1024 * 1024;   // matches code_analysis._MAX_BINARY_FILE_BYTES

function triggerFileSelect() {
    const fileInput = document.getElementById("file-input");
    if (fileInput) fileInput.click();
}

function handleFileSelect(event) {
    const file = event.target.files && event.target.files[0];
    event.target.value = ""; // allow re-selecting the same file later
    if (!file) return;

    const ext = "." + (file.name.split(".").pop() || "").toLowerCase();
    if (!ALLOWED_FILE_EXTENSIONS.includes(ext)) {
        selectedFile = null;
        selectedFilePreviewB64 = null;
        showFileChip(file.name);
        setFileChipState("error", `Unsupported type (${ext})`);
        return;
    }

    const maxSize = BINARY_FILE_EXTENSIONS.includes(ext) ? MAX_BINARY_FILE_SIZE_BYTES : MAX_CODE_FILE_SIZE_BYTES;
    if (file.size > maxSize) {
        selectedFile = null;
        selectedFilePreviewB64 = null;
        showFileChip(file.name);
        setFileChipState("error", `Too large (max ${(maxSize / (1024 * 1024)).toFixed(maxSize < 1024 * 1024 ? 1 : 0)}MB)`);
        return;
    }

    selectedFile = file;
    selectedFilePreviewB64 = null;

    if (IMAGE_FILE_EXTENSIONS.includes(ext)) {
        // Section 6: real image preview (not just the filename) in the
        // command bar chip, generated client-side so it's instant.
        const reader = new FileReader();
        reader.onload = () => {
            const dataUrl = reader.result || "";
            const comma = dataUrl.indexOf(",");
            selectedFilePreviewB64 = comma >= 0 ? dataUrl.slice(comma + 1) : null;
            showFileChip(file.name, dataUrl);
            setFileChipState("idle", "");
        };
        reader.onerror = () => {
            showFileChip(file.name);
            setFileChipState("idle", "");
        };
        reader.readAsDataURL(file);
    } else {
        showFileChip(file.name);
        setFileChipState("idle", "");
    }
}

function clearSelectedFile() {
    selectedFile = null;
    selectedFilePreviewB64 = null;
    const chip = document.getElementById("file-chip");
    if (chip) chip.style.display = "none";
    const thumb = document.getElementById("file-chip-thumb");
    if (thumb) { thumb.style.display = "none"; thumb.src = ""; }
    const fileInput = document.getElementById("file-input");
    if (fileInput) fileInput.value = "";
}

function showFileChip(name, previewDataUrl) {
    const chip   = document.getElementById("file-chip");
    const nameEl = document.getElementById("file-chip-name");
    const thumb  = document.getElementById("file-chip-thumb");
    const icon   = document.getElementById("file-chip-icon");
    if (!chip || !nameEl) return;
    chip.style.display = "flex";
    nameEl.textContent = name;
    if (thumb) {
        if (previewDataUrl) {
            thumb.src = previewDataUrl;
            thumb.style.display = "block";
            if (icon) icon.style.display = "none";
        } else {
            thumb.style.display = "none";
            thumb.src = "";
            if (icon) icon.style.display = "inline";
        }
    }
}

function setFileChipState(state, message) {
    const chip     = document.getElementById("file-chip");
    const statusEl = document.getElementById("file-chip-status");
    if (!chip) return;
    chip.className = "file-chip" + (state && state !== "idle" ? ` ${state}` : "");
    if (statusEl) statusEl.textContent = message || "";
}

// Sends multipart/form-data to POST /chat/upload. Images are routed
// server-side to the real vision-model pipeline; every other supported
// type goes through structural analysis + (if the user asked something)
// real text extraction fed to the LLM. See main.py's chat_upload().
async function sendMessageWithFile(message, file) {
    setFileChipState("uploading", "Uploading…");

    const formData = new FormData();
    formData.append("message", message || "");
    formData.append("session_id", sessionId || "");
    formData.append("file", file, file.name);

    try {
        const res = await fetch(`${BACKEND_URL}/chat/upload`, {
            method: "POST",
            body:   formData,
        });
        if (!res.ok) throw new Error(`Backend returned ${res.status}`);

        const data = await res.json();
        setFileChipState("success", "Received");
        updateResponsePanel(data.response);
        addChatBubble("assistant", data.response, data.timestamp);
        setResponseStatus("STANDBY");
        if (data.session_id) sessionId = data.session_id;
        setTimeout(clearSelectedFile, 2500);
    } catch (e) {
        setFileChipState("error", "Upload failed");
        updateResponsePanel(`I wasn't able to process that file, Sir: ${e.message}`);
        setResponseStatus("ERROR");
        addLogEntry(`File upload failed: ${e.message}`, "error");
    }
}


// ── RESPONSE PANEL ─────────────────────────────────────
function clearTypewriter() {
    if (typewriterInterval) {
        clearInterval(typewriterInterval);
        typewriterInterval = null;
    }
    const el = document.getElementById("response-text");
    if (el) el.classList.remove("typing-cursor");
}

function updateBackendProgress(data) {
    const progressEl = document.getElementById("response-progress");
    if (!progressEl) return;

    if (data.state === "EXECUTING" && data.detail) {
        progressEl.textContent = data.detail;
        progressEl.style.display = "block";
    } else {
        // Any other state (IDLE, SPEAKING, THINKING with no detail,
        // ERROR) means whatever multi-step work was running is done —
        // hide the line rather than leave a stale "step 3/8" message
        // sitting there after the investigation finished.
        progressEl.style.display = "none";
        progressEl.textContent = "";
    }

    if (data.state === "ERROR" && data.detail) {
        addLogEntry(`Backend error: ${data.detail}`, "error");
    }
}

function updateResponsePanel(text) {
    const el = document.getElementById("response-text");
    if (!el) return;
    clearTypewriter();
    el.classList.remove("thinking");
    el.classList.remove("markdown-body");
    el.textContent = "";
    el.classList.add("typing-cursor");
    let i = 0;
    typewriterInterval = setInterval(() => {
        if (i < text.length) {
            el.textContent += text[i];
            i++;
            // FIX: response-text scrolls internally instead of growing
            // unbounded, so keep it pinned to the latest line while typing.
            el.scrollTop = el.scrollHeight;
        } else {
            clearTypewriter();
            // Section 5: once the full reply has "typed out", render it
            // as sanitized Markdown (headings, code blocks with
            // highlighting + copy button, tables, etc.) instead of
            // leaving raw Markdown syntax visible.
            el.classList.add("markdown-body");
            el.innerHTML = renderMarkdown(text);
            enhanceCodeBlocks(el);
        }
    }, 18);
}

function setResponseThinking(text) {
    const el = document.getElementById("response-text");
    if (!el) return;
    el.classList.add("thinking");
    el.classList.remove("typing-cursor");
    el.textContent = text;
    el.scrollTop = el.scrollHeight;
}

function setResponseStatus(status, active = false) {
    const el = document.getElementById("response-status");
    if (!el) return;
    el.textContent = status;
    el.className   = "response-status" + (active ? " active" : "");
}

// ── CONVERSATION ───────────────────────────────────────
// attachment (optional): { filename, kind: "image"|"file", data_url_b64?, media_type? }
// Section 10: images render inline (aspect-ratio preserved, capped
// size, click-to-expand, load/error states) instead of just a filename
// tag; Section 5: assistant text renders as sanitized Markdown.
function addChatBubble(role, content, timestamp, attachmentName, attachment) {
    const list = document.getElementById("conversation-list");
    if (!list) return;

    // FIX: the old dedupe compared `content` against EVERY bubble ever
    // added — now scoped to just the immediate previous bubble (same
    // role, same text, added in the last few seconds) so it only
    // catches a genuine websocket echo of something we just rendered.
    const last = list.lastElementChild;
    if (last && last.dataset.role === role) {
        const lastRaw = last.dataset.rawText || "";
        const lastTs = Number(last.dataset.ts || 0);
        if (lastRaw === content && Date.now() - lastTs < 3000) return;
    }

    // Section 21: only auto-scroll if the user was already at (or near)
    // the bottom before this bubble was added — otherwise a manual
    // scroll-up to review earlier messages gets yanked back down.
    const wasNearBottom = (list.scrollHeight - list.scrollTop - list.clientHeight) < 60;

    const bubble = document.createElement("div");
    bubble.className    = `chat-bubble ${role}`;
    bubble.dataset.role  = role;
    bubble.dataset.ts    = Date.now().toString();
    bubble.dataset.rawText = content;

    const roleLabel = document.createElement("div");
    roleLabel.className   = "bubble-role";
    roleLabel.textContent = role === "user" ? "YOU" : "J.A.R.V.I.S";

    const text = document.createElement("div");
    text.className = "bubble-text";
    if (role === "assistant") {
        text.classList.add("markdown-body");
        text.innerHTML = renderMarkdown(content);
        enhanceCodeBlocks(text);
    } else {
        // User text stays plain (no Markdown parsing needed/wanted for
        // what the user typed themselves).
        text.textContent = content;
    }

    bubble.appendChild(roleLabel);
    bubble.appendChild(text);

    // Section 10: real inline image rendering for an image attachment.
    if (attachment && attachment.kind === "image") {
        const wrap = document.createElement("div");
        wrap.className = "bubble-image-wrap";

        const img = document.createElement("img");
        img.className = "bubble-image loading";
        img.alt = attachment.filename || "attached image";

        const fallback = document.createElement("div");
        fallback.className = "bubble-image-error";
        fallback.style.display = "none";
        fallback.textContent = `⚠ Couldn't load ${attachment.filename || "image"}`;

        img.addEventListener("load", () => img.classList.remove("loading"));
        img.addEventListener("error", () => {
            wrap.replaceChild(fallback, img);
            fallback.style.display = "block";
        });
        img.addEventListener("click", () => openImageLightbox(img.src, attachment.filename));

        if (attachment.data_url_b64) {
            const mime = attachment.media_type || "image/png";
            img.src = `data:${mime};base64,${attachment.data_url_b64}`;
        } else {
            // No inline bytes available (very large image, or a
            // websocket echo that didn't carry the payload) — show the
            // filename tag instead of a broken <img>.
            fallback.style.display = "block";
            fallback.textContent = `📎 ${attachment.filename || "image"}`;
            wrap.appendChild(fallback);
            bubble.appendChild(wrap);
            list.appendChild(bubble);
            if (wasNearBottom) list.scrollTop = list.scrollHeight;
            return;
        }

        wrap.appendChild(img);
        bubble.appendChild(wrap);
    } else if (attachment && attachment.kind === "file") {
        const tag = document.createElement("div");
        tag.className   = "bubble-attachment";
        tag.textContent = `📎 ${attachment.filename}`;
        bubble.appendChild(tag);
    } else if (attachmentName) {
        const tag = document.createElement("div");
        tag.className   = "bubble-attachment";
        tag.textContent = `📎 ${attachmentName}`;
        bubble.appendChild(tag);
    }

    const time = document.createElement("div");
    time.className   = "bubble-time";
    time.textContent = timestamp
        ? new Date(timestamp).toLocaleTimeString()
        : new Date().toLocaleTimeString();
    bubble.appendChild(time);

    list.appendChild(bubble);

    if (wasNearBottom) {
        list.scrollTop = list.scrollHeight;
        hideNewMessagesIndicator();
    } else if (role === "assistant") {
        showNewMessagesIndicator();
    }
}

// ── Section 21: "new messages" indicator when scrolled up ─────
function showNewMessagesIndicator() {
    const panel = document.getElementById("conversation-panel");
    if (!panel) return;
    let indicator = document.getElementById("new-messages-indicator");
    if (!indicator) {
        indicator = document.createElement("button");
        indicator.id = "new-messages-indicator";
        indicator.type = "button";
        indicator.className = "new-messages-indicator";
        indicator.textContent = "↓ New message";
        indicator.addEventListener("click", () => {
            const list = document.getElementById("conversation-list");
            if (list) list.scrollTop = list.scrollHeight;
            hideNewMessagesIndicator();
        });
        panel.appendChild(indicator);
    }
    indicator.classList.add("visible");
}
function hideNewMessagesIndicator() {
    const indicator = document.getElementById("new-messages-indicator");
    if (indicator) indicator.classList.remove("visible");
}

// ── PROACTIVE INTELLIGENCE ──────────────────────────────
// Normalizes the three distinct backend broadcast shapes (this
// feature's own "proactive_event", plus the two pre-existing types
// that had nothing rendering them before this change) into one common
// shape so there's a single render path — Section 16's "keep detection
// separate from presentation" applies just as much on this side.
function normalizeProactiveEvent(data) {
    return {
        id: data.event_id, category: data.category, severity: data.severity,
        title: data.title, message: data.message, actions: data.actions || [],
        timestamp: data.timestamp,
    };
}
function normalizeBackgroundTaskEvent(data) {
    const task = data.task || {};
    const sev = data.severity === "ERROR" ? "WARNING" : (data.severity || "INFO");
    return {
        id: `bg:${task.id || task.name}`, category: "DEVELOPMENT", severity: sev,
        title: sev === "INFO" ? "TASK COMPLETE" : "BUILD/TASK FAILURE",
        message: data.message || `Task '${task.name}' finished.`,
        actions: sev === "INFO" ? [] : ["VIEW_DETAILS"],
        timestamp: Date.now() / 1000,
    };
}
function normalizeHealthAlertEvent(data) {
    const attention = data.attention || [];
    return {
        id: `health:${data.project_path}`, category: "PROJECT",
        severity: attention.length ? "WARNING" : "INFO",
        title: "PROJECT HEALTH",
        message: attention.length ? attention.join(" — ") : "Project health looks fine.",
        actions: attention.length ? ["VIEW_DETAILS"] : [],
        timestamp: Date.now() / 1000,
    };
}

// Section 11 — every action maps to a real, already-working chat
// command (system_security_scan / system_storage_analyze / etc. via
// the normal intent pipeline), never a new tool-call surface.
const PROACTIVE_ACTION_COMMANDS = {
    ANALYZE_STORAGE: "Analyze my storage.",
    CLEAN:           "Clean junk files.",
    RUN_SCAN:        "Run a quick security scan.",
};

let _proactiveToastCount = { WARNING: 0, CRITICAL: 0 };

function handleProactiveNotification(event) {
    if (!event || !event.title) return;

    // Section 26 (lightweight local echo — memory.log_event() on the
    // backend is the durable record; this is just the visible log).
    const logType = event.severity === "CRITICAL" ? "error" : (event.severity === "WARNING" ? "warning" : "info");
    addLogEntry(`${event.title}: ${event.message}`, logType);

    // Section 10/23 — becomes part of the existing conversation, so a
    // follow-up like "how much can I safely clean?" has real context
    // to resolve against.
    const icon = event.severity === "INFO" ? "ℹ" : "⚠";
    addChatBubble("assistant", `${icon} ${event.title}\n\n${event.message}`, event.timestamp ? event.timestamp * 1000 : Date.now());

    showProactiveToast(event);
    updateReactorAlertState(event.severity, +1);
}

function showProactiveToast(event) {
    const stack = document.getElementById("proactive-toast-stack");
    if (!stack) return;

    const toast = document.createElement("div");
    toast.className = `proactive-toast severity-${event.severity}`;

    const title = document.createElement("div");
    title.className = "toast-title";
    title.textContent = (event.severity === "CRITICAL" ? "⚠ " : (event.severity === "WARNING" ? "⚠ " : "")) + event.title;

    const message = document.createElement("div");
    message.className = "toast-message";
    message.textContent = event.message;

    const actions = document.createElement("div");
    actions.className = "toast-actions";

    let dismissed = false;
    function dismiss() {
        if (dismissed) return;
        dismissed = true;
        toast.remove();
        if (event.severity === "WARNING" || event.severity === "CRITICAL") {
            updateReactorAlertState(event.severity, -1);
        }
    }

    (event.actions || []).forEach((action) => {
        const command = PROACTIVE_ACTION_COMMANDS[action];
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "toast-btn";
        btn.textContent = action.replace(/_/g, " ");
        btn.addEventListener("click", () => {
            dismiss();
            if (command) {
                quickCmd(command);
            } else {
                // VIEW_DETAILS / INSPECT — no dedicated tool command;
                // ask JARVIS about the event using the conversation
                // context it was just added to.
                quickCmd(`Tell me more about: ${event.title}`);
            }
        });
        actions.appendChild(btn);
    });

    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "toast-btn dismiss";
    dismissBtn.textContent = "DISMISS";
    dismissBtn.addEventListener("click", dismiss);
    actions.appendChild(dismissBtn);

    toast.appendChild(title);
    toast.appendChild(message);
    toast.appendChild(actions);
    stack.appendChild(toast);

    // Section 2 — don't linger forever. INFO clears itself quickly;
    // WARNING gives more time to notice; CRITICAL stays until the user
    // dismisses it or takes the action.
    if (event.severity === "INFO") {
        setTimeout(dismiss, 8000);
    } else if (event.severity === "WARNING") {
        setTimeout(dismiss, 20000);
    }
}

// Section 22 — subtle reactor alert state while at least one WARNING/
// CRITICAL toast is currently visible; clears back to normal once none
// remain. Counting (not just a boolean) so two overlapping warnings
// don't have the second one's dismissal wrongly clear the first's glow.
function updateReactorAlertState(severity, delta) {
    if (severity !== "WARNING" && severity !== "CRITICAL") return;
    _proactiveToastCount[severity] = Math.max(0, (_proactiveToastCount[severity] || 0) + delta);
    const wrapper = document.querySelector(".hud-wrapper");
    if (!wrapper) return;
    wrapper.classList.toggle("proactive-critical", _proactiveToastCount.CRITICAL > 0);
    wrapper.classList.toggle("proactive-warning", _proactiveToastCount.CRITICAL === 0 && _proactiveToastCount.WARNING > 0);
}

// ── Section 19: minimal ON/OFF control ──────────────────
async function initProactiveToggle() {
    const btn = document.getElementById("proactive-toggle");
    const dot = document.getElementById("proactive-toggle-dot");
    if (!btn) return;

    async function refresh() {
        try {
            const res = await fetch(`${BACKEND_URL}/proactive/status`);
            const data = await res.json();
            btn.classList.toggle("on", !!data.enabled);
        } catch (e) {}
    }

    btn.addEventListener("click", async () => {
        const nextEnabled = !btn.classList.contains("on");
        btn.classList.toggle("on", nextEnabled);
        try {
            await fetch(`${BACKEND_URL}/proactive/toggle`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: nextEnabled }),
            });
        } catch (e) {
            refresh(); // revert to actual server state on failure
        }
    });

    refresh();
}

// ── CONFIRM-TIER TOOL REQUESTS ──────────────────────────
// Renders an inline Allow/Deny prompt for any pending tool
// confirmation (see permissions.py's request_confirmation() — this
// fires for every CONFIRM-tier tool call, not just the storage
// cleanup one). POSTs straight to the existing /confirmations/{id}
// endpoint; that endpoint runs the tool itself on approval, so this
// never re-implements any tool logic client-side.
function renderConfirmationRequest(data) {
    const list = document.getElementById("conversation-list");
    if (!list) return;

    const wasNearBottom = (list.scrollHeight - list.scrollTop - list.clientHeight) < 60;

    const bubble = document.createElement("div");
    bubble.className = "chat-bubble assistant confirmation-bubble";
    bubble.dataset.role = "assistant";
    bubble.dataset.ts = Date.now().toString();

    const roleLabel = document.createElement("div");
    roleLabel.className = "bubble-role";
    roleLabel.textContent = "J.A.R.V.I.S";

    const text = document.createElement("div");
    text.className = "bubble-text";
    text.textContent = data.reason || `JARVIS wants to run "${data.tool}".`;

    const actions = document.createElement("div");
    actions.className = "confirmation-actions";

    const allowBtn = document.createElement("button");
    allowBtn.type = "button";
    allowBtn.className = "confirm-btn confirm-allow";
    allowBtn.textContent = "ALLOW";

    const denyBtn = document.createElement("button");
    denyBtn.type = "button";
    denyBtn.className = "confirm-btn confirm-deny";
    denyBtn.textContent = "DENY";

    const statusEl = document.createElement("div");
    statusEl.className = "confirmation-status";

    async function resolve(approved) {
        allowBtn.disabled = true;
        denyBtn.disabled = true;
        statusEl.textContent = approved ? "Running..." : "Cancelling...";
        try {
            const res = await fetch(`${BACKEND_URL}/confirmations/${data.id}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ approved }),
            });
            const result = await res.json();
            if (!approved) {
                statusEl.textContent = "Denied.";
                return;
            }
            const execution = result.execution || {};
            if (execution.status === "ok") {
                statusEl.textContent = "Done.";
            } else if (execution.status) {
                statusEl.textContent = `${execution.status}: ${execution.message || ""}`;
            } else {
                statusEl.textContent = "Done.";
            }
        } catch (e) {
            statusEl.textContent = `Request failed: ${e.message}`;
        }
    }

    allowBtn.addEventListener("click", () => resolve(true));
    denyBtn.addEventListener("click", () => resolve(false));

    actions.appendChild(allowBtn);
    actions.appendChild(denyBtn);

    bubble.appendChild(roleLabel);
    bubble.appendChild(text);
    bubble.appendChild(actions);
    bubble.appendChild(statusEl);

    const time = document.createElement("div");
    time.className = "bubble-time";
    time.textContent = data.timestamp ? new Date(data.timestamp).toLocaleTimeString() : new Date().toLocaleTimeString();
    bubble.appendChild(time);

    list.appendChild(bubble);
    if (wasNearBottom) list.scrollTop = list.scrollHeight;
}

async function loadConversationHistory() {
    try {
        const res  = await fetch(`${BACKEND_URL}/history?limit=20`);
        const data = await res.json();
        const messages = data.messages || [];
        messages.slice(-10).forEach(msg => {
            const attachment = msg.metadata && msg.metadata.attachment ? msg.metadata.attachment : null;
            addChatBubble(msg.role, msg.content, msg.timestamp, null, attachment);
        });
    } catch (e) {}
}

function clearConversation() {
    const list = document.getElementById("conversation-list");
    if (list) list.innerHTML = "";
    fetch(`${BACKEND_URL}/sessions/clear`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ session_id: sessionId }),
    }).catch(() => {});
    updateResponsePanel("Memory cleared. Starting fresh, Sir.");
    addLogEntry("Conversation cleared.", "info");
    generateSessionId();
}

function addLogEntry(message, type = "info") {
    const list = document.getElementById("events-list");
    if (!list) return;
    const entry = document.createElement("div");
    entry.className   = `log-entry ${type}`;
    entry.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
    list.insertBefore(entry, list.firstChild);
    while (list.children.length > 30) list.removeChild(list.lastChild);
}

// ── VOICE ──────────────────────────────────────────────
async function toggleVoice() {
    if (voiceActive) {
        await deactivateVoice();
    } else {
        await activateVoice();
    }
}

async function activateVoice() {
    const btn   = document.getElementById("voice-btn");
    const label = document.getElementById("voice-label");
    const mic   = document.getElementById("mic-indicator");

    try {
        const res  = await fetch(`${BACKEND_URL}/livekit/token?room=jarvis-room&identity=user-${Date.now()}`);
        const data = await res.json();
        if (!data.token) throw new Error("No token received");

        await loadLiveKitSDK();

        const { Room, RoomEvent } = window.LivekitClient;

        // ── FIX 1 + FIX 4: Room with echo cancellation ────────
        livekitRoom = new Room({
            audioCaptureDefaults: {
                echoCancellation:   true,
                noiseSuppression:   true,
                autoGainControl:    true,
                channelCount:       1,
                sampleRate:         16000,
            },
            adaptiveStream: true,
            dynacast:       true,
        });

        livekitRoom.on(RoomEvent.TrackSubscribed, (track, pub, participant) => {
            if (track.kind === "audio") {
                // ── FIX 4: Only attach remote agent audio ──────
                if (participant.isAgent || !participant.isLocal) {
                    // NOTE: attach()'s return value (the <audio> element)
                    // was previously discarded — it was never appended to
                    // the DOM and never tagged "jarvis-audio", so
                    // deactivateVoice()'s
                    // `document.querySelectorAll(".jarvis-audio").forEach(el => el.remove())`
                    // could never find or clean it up, leaving orphaned
                    // <audio> elements (and their MediaStream) behind
                    // across voice sessions. Tag + append it so cleanup
                    // actually works.
                    const el = track.attach();
                    el.classList.add("jarvis-audio");
                    el.style.display = "none";
                    document.body.appendChild(el);
                    addLogEntry("JARVIS voice connected.", "success");
                    playChime("connect");

                    // ── FIX 4 (corrected): this track is ONE continuous
                    // connection-lifetime stream, not a fresh track per
                    // reply — so `playing`/`pause`/`ended` only fire
                    // once, at connect time, and never again per-turn.
                    // That's the actual reason "JARVIS SPEAKING" looked
                    // disconnected from any real reply: it was firing
                    // once when the (mostly silent) track first
                    // attached, not per-utterance.
                    //
                    // The real per-turn "speaking" signal now comes from
                    // agent.py's agent_state_changed event via the
                    // "voice_state" WebSocket message (see
                    // handleWsMessage) — that's the framework's own
                    // accurate, turn-scoped notion of when TTS is
                    // actually being generated/played for a specific
                    // reply. This local element is now only a safety net
                    // for genuine playback failures (autoplay blocked,
                    // decode error) — if the browser truly can't play
                    // the audio, force back to LISTENING even if the
                    // server-side state still says "speaking", so the UI
                    // never gets stuck lying about it.
                    el.addEventListener("error", () => {
                        if (voiceActive) setVoiceState("LISTENING");
                        const err = el.error;
                        console.error("[PLAYER] ERROR: playback failed", err);
                        addLogEntry(
                            `Playback failed: ${err ? err.message || err.code : "unknown error"}`,
                            "error"
                        );
                    });

                    // Some browsers reject the implicit autoplay attach()
                    // triggers internally (silent rejection — no error
                    // event fires, it just never plays). Explicitly retry
                    // .play() and surface a real error if it's blocked —
                    // this is one of the most likely explanations for
                    // "state says speaking but there's silence": the
                    // audio genuinely never started playing in the tab.
                    el.play().catch((err) => {
                        console.error("[PLAYER] ERROR: autoplay blocked or playback failed:", err);
                        addLogEntry(
                            "Playback blocked by browser autoplay policy — click anywhere on the page and retry.",
                            "error"
                        );
                        if (voiceActive) setVoiceState("LISTENING");
                    });
                }
            }
        });

        livekitRoom.on(RoomEvent.TrackUnsubscribed, (track) => {
            if (track.kind === "audio") {
                jarvisSpeaking = false;
                if (voiceActive) setVoiceState("LISTENING");
            }
        });

        livekitRoom.on(RoomEvent.Disconnected, () => {
            addLogEntry("Voice session ended.", "info");
            deactivateVoice();
        });

        // NOTE: ActiveSpeakersChanged deliberately no longer drives
        // setVoiceState — see the <audio> `playing`/`pause`/`ended`/
        // `error` listeners registered in TrackSubscribed above, which
        // are the accurate source of truth for whether JARVIS is
        // audibly speaking in THIS browser tab. Keeping this listener
        // only for the waveform's realtime color cue is fine since
        // that's cosmetic, not a state claim.
        livekitRoom.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
            const agentSpeaking = speakers.some(s => !s.isLocal);
            if (agentSpeaking) {
                jarvisSpeaking = true;
            } else if (voiceActive) {
                jarvisSpeaking = false;
            }
        });

        await livekitRoom.connect(data.url, data.token);

        // ── FIX 1: Enable mic with echo cancellation ──────────
        await livekitRoom.localParticipant.setMicrophoneEnabled(true, {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl:  true,
        });

        // ── DIAGNOSTIC: log which physical device was actually
        // selected as the input. If this prints "Stereo Mix",
        // "What U Hear", "Wave Out Mix", or anything with
        // "loopback"/"monitor" in the name, that is a Windows-level
        // setting routing your speaker output back into your mic —
        // JARVIS's code cannot fix that; open Windows Sound Settings
        // → Recording tab → disable/unset that device as default.
        try {
            const micPub = livekitRoom.localParticipant.audioTrackPublications
                .values().next().value;
            const track = micPub && micPub.track;
            const settings = track && track.mediaStreamTrack &&
                track.mediaStreamTrack.getSettings();
            const deviceLabel = track && track.mediaStreamTrack &&
                track.mediaStreamTrack.label;
            console.log("[VOICE] Input device:", deviceLabel || "(unknown)");
            addLogEntry(`Input device: ${deviceLabel || "unknown"}`, "info");
            if (deviceLabel && /stereo mix|what u hear|wave out|loopback|monitor of/i.test(deviceLabel)) {
                addLogEntry(
                    "WARNING: selected input looks like a loopback/monitor device — this will echo your speaker output back to JARVIS.",
                    "error"
                );
            }
        } catch (e) {
            console.warn("[JARVIS] Could not read input device label:", e);
        }

        // ── FIX 1: Use LiveKit's audio track for waveform ─────
        // Only get ONE stream — reuse the LiveKit mic, don't open a second one
        try {
            const tracks = livekitRoom.localParticipant.audioTrackPublications;
            for (const [, pub] of tracks) {
                if (pub.track && pub.track.mediaStream) {
                    micStream = pub.track.mediaStream;
                    break;
                }
            }
            if (!micStream) {
                micStream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl:  true,
                    }
                });
            }
        } catch (e) {
            console.warn("[JARVIS] Could not get mic stream for visualiser:", e);
        }

        if (micStream) startAudioVisualiser(micStream);

        voiceActive = true;

        if (btn) btn.classList.add("active");
        if (mic) mic.classList.add("active");
        setVoiceState("LISTENING");

        document.getElementById("audio-status").textContent = "ACTIVE";
        addLogEntry("Voice assistant activated.", "success");
        updateResponsePanel("Voice activated. Listening, Sir.");

    } catch (e) {
        console.error("[JARVIS] Voice error:", e);
        addLogEntry("Voice activation failed: " + e.message, "error");
        updateResponsePanel("Voice system unavailable. Using text interface, Sir.");
        voiceActive = false;
    }
}

async function deactivateVoice() {
    const btn = document.getElementById("voice-btn");
    const mic = document.getElementById("mic-indicator");

    if (voiceActive) playChime("disconnect");

    // Remove attached audio elements
    document.querySelectorAll(".jarvis-audio").forEach(el => el.remove());

    if (livekitRoom) {
        try { await livekitRoom.disconnect(); } catch (e) {}
        livekitRoom = null;
    }

    if (micStream) {
        micStream.getTracks().forEach(t => t.stop());
        micStream = null;
    }

    if (audioContext) {
        try { await audioContext.close(); } catch (e) {}
        audioContext = null;
        analyser     = null;
    }

    voiceActive    = false;
    jarvisSpeaking = false;

    if (btn) btn.classList.remove("active", "speaking");
    if (mic) mic.classList.remove("active");
    setVoiceState("ACTIVATE");

    document.getElementById("audio-status").textContent = "IDLE";
    addLogEntry("Voice assistant deactivated.", "info");
}

async function loadLiveKitSDK() {
    if (window.LivekitClient) return;
    return new Promise((resolve, reject) => {
        const script   = document.createElement("script");
        script.src     = "https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js";
        script.onload  = resolve;
        script.onerror = () => reject(new Error("Failed to load LiveKit SDK"));
        document.head.appendChild(script);
    });
}

// ── AUDIO VISUALISER ───────────────────────────────────
function startAudioVisualiser(stream) {
    if (audioContext) {
        try { audioContext.close(); } catch (e) {}
    }
    audioContext = new (window.AudioContext || window.webkitAudioContext)();
    analyser     = audioContext.createAnalyser();
    analyser.fftSize = 256;
    const source = audioContext.createMediaStreamSource(stream);
    source.connect(analyser);
    drawWaveform();
}

function initWaveform() {
    drawWaveform();
    drawRadialWaveform();
}

// Radial/circular waveform drawn directly into the HUD's SVG, centered
// on the voice button — reuses the same `analyser` audio data as the
// flat waveform card, just plotted as spikes around a ring instead of
// left-to-right. This is what gives the arc-reactor "system is alive"
// feel rather than a flat oscilloscope line.
function drawRadialWaveform() {
    const poly = document.getElementById("radial-waveform");
    if (!poly) return;

    const CX = 200, CY = 200;      // matches the SVG viewBox center (400x400)
    const BASE_R = 90;             // resting radius — inside the arc rings, around the button
    const MAX_SPIKE = 22;          // how far spikes reach outward at full volume
    const POINTS = 64;             // resolution of the ring

    function frame() {
        requestAnimationFrame(frame);
        poly.classList.toggle("jarvis-speaking", jarvisSpeaking);

        let pts = [];
        if (analyser && voiceActive) {
            const bufLen = analyser.frequencyBinCount;
            const data   = new Uint8Array(bufLen);
            analyser.getByteTimeDomainData(data);

            for (let i = 0; i < POINTS; i++) {
                const dataIdx = Math.floor((i / POINTS) * bufLen);
                const v = (data[dataIdx] - 128) / 128.0; // -1..1
                const r = BASE_R + v * MAX_SPIKE;
                const angle = (i / POINTS) * Math.PI * 2;
                pts.push(`${(CX + r * Math.cos(angle)).toFixed(1)},${(CY + r * Math.sin(angle)).toFixed(1)}`);
            }
        } else {
            // Idle: a gentle, slow-breathing near-perfect circle instead
            // of a flat line — feels like standby, not "broken".
            const t = performance.now() / 1000;
            const breathe = Math.sin(t * 0.8) * 2;
            for (let i = 0; i < POINTS; i++) {
                const angle = (i / POINTS) * Math.PI * 2;
                const r = BASE_R * 0.5 + breathe;
                pts.push(`${(CX + r * Math.cos(angle)).toFixed(1)},${(CY + r * Math.sin(angle)).toFixed(1)}`);
            }
        }
        poly.setAttribute("points", pts.join(" "));
    }
    frame();
}

function drawWaveform() {
    const canvas = document.getElementById("waveform-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    canvas.width = canvas.offsetWidth || 240;

    function draw() {
        waveformAnimId = requestAnimationFrame(draw);
        const W = canvas.width;
        const H = canvas.height;
        ctx.clearRect(0, 0, W, H);

        if (analyser && voiceActive) {
            const bufLen = analyser.frequencyBinCount;
            const data   = new Uint8Array(bufLen);
            analyser.getByteTimeDomainData(data);

            ctx.beginPath();
            // ── FIX 4: Different color when JARVIS is speaking ─
            ctx.strokeStyle = jarvisSpeaking
                ? "rgba(0, 255, 136, 0.9)"
                : "rgba(0, 212, 255, 0.9)";
            ctx.lineWidth   = 1.5;
            ctx.shadowBlur  = 6;
            ctx.shadowColor = jarvisSpeaking
                ? "rgba(0, 255, 136, 0.6)"
                : "rgba(0, 212, 255, 0.6)";

            const sliceW = W / bufLen;
            let x = 0;
            for (let i = 0; i < bufLen; i++) {
                const v = data[i] / 128.0;
                const y = (v * H) / 2;
                if (i === 0) ctx.moveTo(x, y);
                else         ctx.lineTo(x, y);
                x += sliceW;
            }
            ctx.lineTo(W, H / 2);
            ctx.stroke();

        } else {
            ctx.beginPath();
            ctx.strokeStyle = "rgba(0, 212, 255, 0.25)";
            ctx.lineWidth   = 1;
            ctx.shadowBlur  = 0;
            const midY = H / 2;
            ctx.moveTo(0, midY);
            for (let x = 0; x < W; x += 4) {
                const noise = (Math.random() - 0.5) * 2;
                ctx.lineTo(x, midY + noise);
            }
            ctx.lineTo(W, midY);
            ctx.stroke();
        }
    }
    draw();
}

// ── RING TICKS ─────────────────────────────────────────
function buildRingTicks() {
    buildTicks("tick-outer", 60, 0.5,   4, 8);
    buildTicks("tick-mid",   36, 0.408, 3, 6);
}

function buildTicks(containerId, count, radiusFactor, shortLen, longLen) {
    const container = document.getElementById(containerId);
    if (!container) return;
    for (let i = 0; i < count; i++) {
        const angle  = (i / count) * 360;
        const isLong = i % 5 === 0;
        const len    = isLong ? longLen : shortLen;
        const tick   = document.createElement("div");
        tick.className = "ring-tick";
        // Viewport fix: radius is expressed as calc(var(--reactor) *
        // factor) instead of a fixed px number, so these ticks stay
        // locked to the ring's actual (possibly shrunk) edge on short
        // viewports instead of drifting outside it — --reactor is set
        // on the .hud-wrapper ancestor and CSS custom properties
        // inherit into this inline style's var() reference normally.
        tick.style.cssText = `
            height: ${len}px;
            transform: rotate(${angle}deg) translate(-50%, calc(var(--reactor) * -${radiusFactor}));
            opacity: ${isLong ? 0.7 : 0.3};
        `;
        container.appendChild(tick);
    }
}

// ── PARTICLE FIELD ─────────────────────────────────────
function initParticles() {
    const canvas = document.getElementById("particle-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    function resize() {
        canvas.width  = window.innerWidth;
        canvas.height = window.innerHeight;
    }
    resize();
    window.addEventListener("resize", resize);

    const PARTICLE_COUNT = 80;
    const particles = [];

    for (let i = 0; i < PARTICLE_COUNT; i++) {
        particles.push({
            x:       Math.random() * window.innerWidth,
            y:       Math.random() * window.innerHeight,
            vx:      (Math.random() - 0.5) * 0.3,
            vy:      (Math.random() - 0.5) * 0.3,
            radius:  Math.random() * 1.5 + 0.3,
            opacity: Math.random() * 0.4 + 0.1,
        });
    }

    function drawParticles() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        for (let i = 0; i < particles.length; i++) {
            for (let j = i + 1; j < particles.length; j++) {
                const dx   = particles[i].x - particles[j].x;
                const dy   = particles[i].y - particles[j].y;
                const dist = Math.sqrt(dx * dx + dy * dy);
                if (dist < 120) {
                    ctx.beginPath();
                    ctx.strokeStyle = `rgba(0, 212, 255, ${0.06 * (1 - dist / 120)})`;
                    ctx.lineWidth   = 0.5;
                    ctx.moveTo(particles[i].x, particles[i].y);
                    ctx.lineTo(particles[j].x, particles[j].y);
                    ctx.stroke();
                }
            }
        }
        particles.forEach(p => {
            ctx.beginPath();
            ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
            ctx.fillStyle   = `rgba(0, 212, 255, ${p.opacity})`;
            ctx.shadowBlur  = 4;
            ctx.shadowColor = "rgba(0, 212, 255, 0.4)";
            ctx.fill();
            p.x += p.vx;
            p.y += p.vy;
            if (p.x < 0) p.x = canvas.width;
            if (p.x > canvas.width)  p.x = 0;
            if (p.y < 0) p.y = canvas.height;
            if (p.y > canvas.height) p.y = 0;
        });
        requestAnimationFrame(drawParticles);
    }
    drawParticles();
}

// ── AGENT CONTROLS ─────────────────────────────────────
async function restartAgent() {
    addLogEntry("Restarting agent...", "info");
    updateResponsePanel("Restarting voice agent, Sir. One moment.");
    try {
        const res  = await fetch(`${BACKEND_URL}/agent/restart`, { method: "POST" });
        const data = await res.json();
        addLogEntry(`Agent restarted. PID: ${data.pid}`, "success");
        updateResponsePanel(data.message || "Agent restarted, Sir.");
    } catch (e) {
        addLogEntry("Agent restart failed.", "error");
    }
}

async function stopAgent() {
    addLogEntry("Stopping agent...", "info");
    try {
        const res  = await fetch(`${BACKEND_URL}/agent/stop`, { method: "POST" });
        const data = await res.json();
        addLogEntry("Agent stopped.", "info");
        updateResponsePanel(data.message || "Agent stopped, Sir.");
    } catch (e) {
        addLogEntry("Agent stop failed.", "error");
    }
}

async function startAgent() {
    addLogEntry("Starting agent...", "info");
    try {
        const res  = await fetch(`${BACKEND_URL}/agent/start`, { method: "POST" });
        const data = await res.json();
        if (data.status === "already_running") {
            addLogEntry(`Agent already running. PID: ${data.pid}`, "info");
        } else {
            addLogEntry(`Agent started. PID: ${data.pid}`, "success");
            updateResponsePanel(data.message || "Agent started, Sir.");
        }
    } catch (e) {
        addLogEntry("Agent start failed.", "error");
    }
}