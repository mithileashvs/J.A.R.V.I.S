// ── CONFIG ─────────────────────────────────────────────
// Same-origin when UI is served by FastAPI (port 8000).
// Falls back to localhost:8000 for standalone static previews (:3000 / :5500 / file://).
const BACKEND_URL = (() => {
    const { protocol, origin, port } = window.location;
    if (protocol === "http:" || protocol === "https:") {
        if (port === "3000" || port === "5500") return "http://localhost:8000";
        return origin;
    }
    return "http://localhost:8000";
})();
const WS_URL = BACKEND_URL.replace(/^http/, "ws") + "/ws/conversation";

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

// ── SCENE CONTROLLER ───────────────────────────────────
// Constructs / dissolves holographic scenes. Never hard-cuts panels.
const SceneController = {
    current: "IDLE",
    phase: "idle", // idle | constructing | settled | dissolving
    _timer: null,
    _queue: null,
    CONSTRUCT_MS: 520,
    DISSOLVE_MS: 380,

    stage() { return document.getElementById("scene-stage"); },
    body() { return document.body; },
    label() { return document.getElementById("scene-label"); },

    _setBodyScene(name) {
        const b = this.body();
        if (!b) return;
        b.classList.remove(
            "scene-idle", "scene-active", "scene-constructing", "scene-dissolving", "scene-decision"
        );
        if (name === "IDLE") b.classList.add("scene-idle");
        else {
            b.classList.add("scene-active");
            if (this.phase === "constructing") b.classList.add("scene-constructing");
            if (this.phase === "dissolving") b.classList.add("scene-dissolving");
            if (name === "DECISION") b.classList.add("scene-decision");
        }
    },

    _setStagePhase(phase) {
        const stage = this.stage();
        if (!stage) return;
        stage.classList.remove("is-idle", "is-constructing", "is-settled", "is-dissolving");
        if (phase === "idle") stage.classList.add("is-idle");
        else if (phase === "constructing") stage.classList.add("is-constructing");
        else if (phase === "settled") stage.classList.add("is-settled");
        else if (phase === "dissolving") stage.classList.add("is-dissolving");
        this.phase = phase;
    },

    _hideAllLayers() {
        document.querySelectorAll(".scene-layer").forEach((el) => {
            el.hidden = true;
        });
    },

    _showLayer(scene) {
        this._hideAllLayers();
        const layer = document.querySelector(`.scene-layer[data-scene="${scene}"]`);
        if (layer) layer.hidden = false;
    },

    _updateLabel(scene) {
        const el = this.label();
        if (el) el.textContent = scene;
    },

    enter(scene, payload) {
        if (!scene) return;
        if (scene === this.current && this.phase === "settled") {
            this.update(scene, payload);
            return;
        }
        if (this.phase === "constructing" || this.phase === "dissolving") {
            this._queue = { scene, payload };
            return;
        }
        if (this.current !== "IDLE" && scene !== this.current) {
            this._dissolveThen(() => this._construct(scene, payload));
            return;
        }
        if (scene === "IDLE") {
            this.dissolveToIdle();
            return;
        }
        this._construct(scene, payload);
    },

    update(scene, payload) {
        if (scene !== this.current) {
            this.enter(scene, payload);
            return;
        }
        this._applyPayload(scene, payload);
    },

    dissolveToIdle() {
        if (this.current === "IDLE" && this.phase === "idle") return;
        this._dissolveThen(() => {
            this.current = "IDLE";
            this._hideAllLayers();
            this._setStagePhase("idle");
            this._setBodyScene("IDLE");
            this._updateLabel("IDLE");
            this._flushQueue();
        });
    },

    _construct(scene, payload) {
        clearTimeout(this._timer);
        this.current = scene;
        this._showLayer(scene);
        this._applyPayload(scene, payload);
        this._setStagePhase("constructing");
        this._setBodyScene(scene);
        this._updateLabel(scene);
        if (typeof AnimationSystem !== "undefined") {
            AnimationSystem.onSceneConstruct(scene);
            if (scene === "DECISION") AnimationSystem.enterDecisionFocus();
        }
        if (typeof CinematicSystem !== "undefined") {
            CinematicSystem.onScene(scene);
        }
        this._timer = setTimeout(() => {
            this._setStagePhase("settled");
            this._setBodyScene(scene);
            this._flushQueue();
        }, this.CONSTRUCT_MS);
    },

    _dissolveThen(next) {
        clearTimeout(this._timer);
        if (this.current === "IDLE" || this.phase === "idle") {
            next();
            return;
        }
        this._setStagePhase("dissolving");
        this._setBodyScene(this.current);
        if (typeof AnimationSystem !== "undefined") {
            AnimationSystem.onSceneDissolve(this.current);
        }
        if (typeof CinematicSystem !== "undefined") {
            CinematicSystem.onSceneDissolve();
        }
        this._timer = setTimeout(() => {
            if (typeof AnimationSystem !== "undefined") AnimationSystem.exitDecisionFocus();
            if (typeof CinematicSystem !== "undefined") CinematicSystem.onScene("IDLE");
            next();
        }, this.DISSOLVE_MS);
    },

    _flushQueue() {
        if (!this._queue) return;
        const next = this._queue;
        this._queue = null;
        this.enter(next.scene, next.payload);
    },

    _applyPayload(scene, payload) {
        if (!payload) return;
        if (scene === "VOICE") {
            const cap = document.getElementById("voice-scene-caption");
            if (cap && payload.state) cap.textContent = payload.state;
        }
        if (scene === "FILES") {
            const name = document.getElementById("scene-files-name");
            const status = document.getElementById("scene-files-status");
            const thumb = document.getElementById("scene-files-thumb");
            if (name && payload.name) name.textContent = payload.name;
            if (status && payload.status) status.textContent = payload.status;
            if (thumb) {
                if (payload.preview) {
                    thumb.src = payload.preview;
                    thumb.hidden = false;
                } else if (payload.clearThumb) {
                    thumb.hidden = true;
                    thumb.src = "";
                }
            }
            if (typeof AnimationSystem !== "undefined") {
                AnimationSystem.setFileScanState(payload.status || "");
            }
            if (typeof CinematicSystem !== "undefined" && payload.status) {
                const s = String(payload.status).toLowerCase();
                if (/fail|error/.test(s)) CinematicSystem.setFileResult(false);
                else if (/done|complete|success|ready|analys/.test(s) && !/standby|upload|pending|scan/.test(s)) {
                    CinematicSystem.setFileResult(true);
                }
            }
        }
        if (scene === "SYSTEM_ACTION") {
            const steps = document.getElementById("scene-system-steps");
            if (steps && payload.detail) {
                steps.innerHTML = `<div class="system-step-label">${_escapeHtml(payload.detail)}</div>`;
            }
            if (typeof AnimationSystem !== "undefined") {
                AnimationSystem.advanceSystemPipeline(payload.detail || "");
            }
            if (typeof CinematicSystem !== "undefined" && payload.detail) {
                const t = String(payload.detail).toUpperCase();
                if (/INIT/.test(t)) CinematicSystem.advanceExec(0);
                else if (/EXEC|RUN|START/.test(t)) CinematicSystem.advanceExec(1);
                else if (/VERIFY|CHECK|VALID/.test(t)) CinematicSystem.advanceExec(2);
                else if (/COMPLETE|DONE|SUCCESS|FINISH/.test(t)) CinematicSystem.setExecComplete();
                else if (/FAIL|ERROR/.test(t)) {
                    const g = CinematicSystem.el("cin-exec-graph");
                    if (g) g.classList.add("failed");
                    CinematicSystem.playError();
                }
            }
        }
        if (scene === "DECISION") {
            const body = document.getElementById("scene-decision-body");
            if (body && payload.reason) body.textContent = payload.reason;
        }
    },
};

// ── ANIMATION SYSTEM (spritesheet language) ────────────
const AnimationSystem = {
    reduced: typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches,
    _activateLock: false,
    particleMode: "idle", // idle | voice | thinking | speaking | action | error

    setCoreState(state) {
        const wrap = document.getElementById("hud-wrapper");
        if (!wrap) return;
        const map = {
            READY: "ready",
            LISTENING: "listening",
            THINKING: "thinking",
            SPEAKING: "speaking",
            ERROR: "error",
            EXECUTING: "executing",
            VERIFYING: "verifying",
            ACTIVATING: "activating",
        };
        const key = map[state] || "ready";
        wrap.className = wrap.className
            .split(/\s+/)
            .filter((c) => c && !c.startsWith("core-state-"))
            .concat([`core-state-${key}`])
            .join(" ");
        wrap.dataset.coreState = state;
        document.body.classList.remove(
            "core-thinking", "core-speaking", "core-listening", "core-error"
        );
        if (key === "thinking") document.body.classList.add("core-thinking");
        if (key === "speaking") document.body.classList.add("core-speaking");
        if (key === "listening") document.body.classList.add("core-listening");
        if (key === "error") document.body.classList.add("core-error");

        this.particleMode =
            key === "thinking" ? "thinking" :
            key === "speaking" ? "speaking" :
            key === "listening" || key === "activating" ? "voice" :
            key === "error" ? "error" :
            key === "executing" || key === "verifying" ? "action" : "idle";
    },

    async playActivationSequence() {
        if (this.reduced) return;
        if (this._activateLock) return;
        this._activateLock = true;
        this.setCoreState("ACTIVATING");
        this.burst();
        this.travelLight("core", "core", "outward");
        await new Promise((r) => setTimeout(r, 720));
        this._activateLock = false;
    },

    onSceneConstruct(scene) {
        if (this.reduced) return;
        const from = "core";
        const to =
            scene === "RESPONSE" ? "response" :
            scene === "FILES" ? "files" :
            scene === "SYSTEM_ACTION" ? "system" :
            scene === "DECISION" ? "decision" : "core";
        this.travelLight(from, to, "outward");
        if (scene === "SYSTEM_ACTION") {
            this.resetSystemPipeline();
            this.advanceSystemPipeline("INITIALIZE");
            this.particleMode = "action";
            this.setCoreState("EXECUTING");
        }
        if (scene === "FILES") this.setFileScanState("scanning");
    },

    onSceneDissolve(scene) {
        if (this.reduced) return;
        this.travelLight(
            scene === "RESPONSE" ? "response" :
            scene === "FILES" ? "files" :
            scene === "SYSTEM_ACTION" ? "system" :
            scene === "DECISION" ? "decision" : "core",
            "core",
            "inward"
        );
        this.exitDecisionFocus();
        this.particleMode = "idle";
    },

    enterDecisionFocus() {
        document.body.classList.add("decision-focus", "scene-decision");
    },
    exitDecisionFocus() {
        document.body.classList.remove("decision-focus", "scene-decision");
    },

    _anchor(name) {
        const map = {
            core: document.getElementById("hud-wrapper") || document.getElementById("voice-btn"),
            response: document.getElementById("response-panel") || document.getElementById("scene-response"),
            files: document.getElementById("files-scan-frame") || document.getElementById("scene-files"),
            system: document.getElementById("scene-system-steps") || document.getElementById("scene-system"),
            decision: document.getElementById("scene-decision-body") || document.getElementById("scene-decision"),
            conversation: document.getElementById("conversation-panel"),
        };
        return map[name] || map.core;
    },

    travelLight(fromName, toName) {
        if (this.reduced) return;
        const svg = document.getElementById("light-trail");
        const path = document.getElementById("light-trail-path");
        const bead = document.getElementById("light-trail-bead");
        if (!svg || !path || !bead) return;

        const fromEl = this._anchor(fromName);
        const toEl = this._anchor(toName);
        if (!fromEl || !toEl) return;

        const vw = window.innerWidth || 1;
        const vh = window.innerHeight || 1;
        const a = fromEl.getBoundingClientRect();
        const b = toEl.getBoundingClientRect();
        const x1 = ((a.left + a.width / 2) / vw) * 100;
        const y1 = ((a.top + a.height / 2) / vh) * 100;
        const x2 = ((b.left + b.width / 2) / vw) * 100;
        const y2 = ((b.top + b.height / 2) / vh) * 100;
        const mx = (x1 + x2) / 2;
        const my = (y1 + y2) / 2 - 6;

        path.setAttribute("d", `M${x1} ${y1} Q${mx} ${my} ${x2} ${y2}`);
        const len = path.getTotalLength ? path.getTotalLength() : 100;
        path.style.strokeDasharray = String(len);
        path.style.strokeDashoffset = String(len);

        svg.classList.add("active", "traveling");
        path.style.transition = "none";
        path.getBoundingClientRect();
        path.style.transition = "stroke-dashoffset 880ms cubic-bezier(0.22, 1, 0.36, 1)";
        path.style.strokeDashoffset = "0";

        const start = performance.now();
        const dur = 880;
        const step = (now) => {
            const t = Math.min(1, (now - start) / dur);
            // smoother ease-out cubic
            const ease = 1 - Math.pow(1 - t, 3);
            try {
                const pt = path.getPointAtLength(ease * len);
                bead.setAttribute("cx", pt.x);
                bead.setAttribute("cy", pt.y);
            } catch (_) {}
            if (t < 1) requestAnimationFrame(step);
            else {
                svg.classList.remove("traveling");
                setTimeout(() => svg.classList.remove("active"), 280);
                if (toName === "conversation" || toName === "response") {
                    const panel = document.getElementById("conversation-panel");
                    if (panel) {
                        panel.classList.add("energy-hit");
                        setTimeout(() => panel.classList.remove("energy-hit"), 500);
                    }
                }
            }
        };
        requestAnimationFrame(step);
    },

    burst() {
        if (this.reduced) return;
        const fx = document.getElementById("fx-overlay");
        if (!fx) return;
        fx.classList.remove("burst", "success", "error");
        void fx.offsetWidth;
        fx.classList.add("burst");
        const core = document.getElementById("hud-wrapper") || document.getElementById("voice-btn");
        if (core && typeof window.__jarvisParticleBurst === "function") {
            const r = core.getBoundingClientRect();
            window.__jarvisParticleBurst(r.left + r.width / 2, r.top + r.height / 2, "255,176,0");
        }
        setTimeout(() => fx.classList.remove("burst"), 750);
    },

    success() {
        if (this.reduced) return;
        const fx = document.getElementById("fx-overlay");
        if (!fx) return;
        fx.classList.remove("burst", "success", "error");
        void fx.offsetWidth;
        fx.classList.add("success");
        setTimeout(() => fx.classList.remove("success"), 850);
    },

    error() {
        if (this.reduced) return;
        const fx = document.getElementById("fx-overlay");
        if (!fx) return;
        fx.classList.remove("burst", "success", "error");
        void fx.offsetWidth;
        fx.classList.add("error");
        document.body.classList.add("fx-error-ambient");
        setTimeout(() => {
            fx.classList.remove("error");
            document.body.classList.remove("fx-error-ambient");
        }, 900);
    },

    setFileScanState(status) {
        const frame = document.getElementById("files-scan-frame");
        if (!frame) return;
        const s = String(status || "").toLowerCase();
        frame.classList.remove("scanning", "success", "error");
        if (/fail|error/.test(s)) {
            frame.classList.add("error");
            this.error();
        } else if (/done|complete|success|ready|analys/.test(s) && !/standby|upload|pending|scan/.test(s)) {
            frame.classList.add("success");
            this.success();
        } else if (s && s !== "standby") {
            frame.classList.add("scanning");
        }
    },

    resetSystemPipeline() {
        const pipe = document.getElementById("system-pipeline");
        if (!pipe) return;
        pipe.classList.add("running");
        pipe.querySelectorAll(".sys-step").forEach((el) => {
            el.classList.remove("active", "done", "fail");
        });
    },

    advanceSystemPipeline(detail) {
        const pipe = document.getElementById("system-pipeline");
        if (!pipe) return;
        const text = String(detail || "").toUpperCase();
        const steps = [...pipe.querySelectorAll(".sys-step")];
        let idx = 0;
        if (/EXEC|RUN|START/.test(text)) idx = 1;
        if (/VERIF|CHECK/.test(text)) idx = 2;
        if (/COMPLETE|DONE|SUCCESS|FINISH/.test(text)) idx = 3;
        if (/FAIL|ERROR/.test(text)) {
            steps.forEach((el, i) => {
                el.classList.toggle("done", i < 1);
                el.classList.toggle("fail", i === 1);
                el.classList.remove("active");
            });
            this.error();
            return;
        }
        steps.forEach((el, i) => {
            el.classList.toggle("done", i < idx);
            el.classList.toggle("active", i === idx);
            el.classList.remove("fail");
        });
        if (idx === 3) {
            this.success();
            pipe.classList.remove("running");
        } else {
            pipe.classList.add("running");
        }
    },

    onSpeakingEnergy() {
        // Keep speaking energy on the reactor — no outward throw to panels
    },
};

// ── CINEMATIC HERO SYSTEM (large contextual FX) ────────
const CinematicSystem = {
    reduced: typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches,
    _bootDone: false,
    _neuralTimer: null,
    _heroMode: false,
    _execStep: 0,

    el(id) { return document.getElementById(id); },

    _setEnv(mode) {
        document.body.classList.remove(
            "env-light-listen", "env-light-think", "env-light-exec",
            "env-light-success", "env-light-error"
        );
        if (mode) document.body.classList.add(`env-light-${mode}`);
    },

    _setHero(on) {
        this._heroMode = !!on;
        document.body.classList.toggle("cin-hero-active", !!on);
        document.body.classList.toggle("cin-dim-ui", !!on);
    },

    _show(id, ms) {
        const node = this.el(id);
        if (!node) return;
        node.classList.add("active");
        if (ms) {
            clearTimeout(node._cinHide);
            node._cinHide = setTimeout(() => node.classList.remove("active"), ms);
        }
    },

    _hide(id) {
        const node = this.el(id);
        if (node) node.classList.remove("active", "success", "error", "complete", "failed", "leaving", "collapsing");
    },

    _hideAllHero() {
        [
            "cin-hero-ring", "cin-neural-field", "cin-energy-flow", "cin-data-wall",
            "cin-data-rain", "cin-speak-field", "cin-voice-activate",
            "cin-scan-field", "cin-exec-graph", "cin-decision-field",
            "cin-success-burst", "cin-error-collapse", "cin-dissolve"
        ].forEach((id) => this._hide(id));
        this._setHero(false);
        document.body.classList.remove("cin-voice-activate");
    },

    async playBoot() {
        if (this._bootDone) return;
        this._bootDone = true;
        if (this.reduced) return;
        const boot = this.el("cin-boot");
        if (!boot) return;
        document.body.classList.add("cin-booting");
        boot.classList.add("active");
        boot.style.display = "";
        await new Promise((r) => setTimeout(r, 1600));
        boot.classList.add("leaving");
        await new Promise((r) => setTimeout(r, 350));
        boot.classList.remove("active", "leaving");
        document.body.classList.remove("cin-booting");
        this._show("cin-ambient");
        if (typeof playChime === "function") playChime("connect");
    },

    onVoiceActivate() {
        if (this.reduced) return;
        this._setHero(true);
        this._setEnv("listen");
        this._show("cin-hero-ring");
        this._show("cin-voice-activate", 1600);
        this._show("cin-energy-flow", 2000);
        this._show("cin-data-rain", 2200);
        setTimeout(() => {
            if (!document.body.classList.contains("core-listening") &&
                !document.body.classList.contains("core-thinking") &&
                !document.body.classList.contains("core-speaking")) {
                // keep hero if still in voice scene
            }
        }, 1800);
    },

    onVoiceState(state) {
        if (this.reduced) return;
        const s = state;
        if (s === "LISTENING") {
            this._setHero(true);
            this._setEnv("listen");
            this._show("cin-hero-ring");
            this._show("cin-energy-flow");
            this._show("cin-data-rain");
            this._hide("cin-neural-field");
            this._hide("cin-speak-field");
            this._hide("cin-data-wall");
        } else if (s === "THINKING") {
            this._setHero(true);
            this._setEnv("think");
            this._show("cin-hero-ring");
            this._show("cin-data-wall");
            this._show("cin-data-rain");
            this._hide("cin-energy-flow");
            this._hide("cin-speak-field");
            this.buildNeuralField();
            this._show("cin-neural-field");
        } else if (s === "SPEAKING") {
            this._setHero(true);
            this._setEnv("listen");
            this._hide("cin-neural-field");
            this._hide("cin-energy-flow");
            this._show("cin-hero-ring");
            this._show("cin-speak-field");
            this._show("cin-data-wall");
            this._show("cin-data-rain");
            if (typeof AnimationSystem !== "undefined") {
                AnimationSystem.onSpeakingEnergy();
            }
        } else if (s === "ERROR") {
            this.playError();
        } else if (s === "READY" || s === "IDLE") {
            this._setEnv(null);
            this._hide("cin-neural-field");
            this._hide("cin-energy-flow");
            this._hide("cin-speak-field");
            this._hide("cin-data-wall");
            this._hide("cin-data-rain");
            this._hide("cin-hero-ring");
            this._setHero(false);
        }
    },

    onScene(scene) {
        if (this.reduced) return;
        this._hide("cin-decision-field");
        this._hide("cin-scan-field");
        this._hide("cin-exec-graph");
        this._hide("cin-dissolve");

        if (scene === "VOICE") {
            this._setHero(true);
            this._show("cin-hero-ring");
            this._show("cin-data-rain");
        } else if (scene === "RESPONSE") {
            this._setHero(true);
            this._show("cin-hero-ring");
            this._show("cin-speak-field");
            this._show("cin-data-wall");
            this._show("cin-data-rain");
        } else if (scene === "FILES") {
            this._setHero(true);
            this._setEnv("think");
            this._show("cin-hero-ring");
            this._show("cin-scan-field");
            this._show("cin-data-rain");
        } else if (scene === "SYSTEM_ACTION") {
            this._setHero(true);
            this._setEnv("exec");
            this._execStep = 0;
            this._show("cin-hero-ring");
            this._show("cin-exec-graph");
            this._show("cin-data-rain");
            this.advanceExec(0);
            if (typeof AnimationSystem !== "undefined") {
                AnimationSystem.setCoreState("EXECUTING");
            }
        } else if (scene === "DECISION") {
            this._setHero(true);
            this._setEnv("error");
            this._show("cin-decision-field");
            this._show("cin-hero-ring");
        } else if (scene === "IDLE") {
            this._setEnv(null);
            this._hideAllHero();
            this._show("cin-ambient");
        }
    },

    onSceneDissolve() {
        if (this.reduced) return;
        this._show("cin-dissolve", 750);
        this._hide("cin-data-wall");
        this._hide("cin-neural-field");
        this._hide("cin-scan-field");
        this._hide("cin-exec-graph");
        this._hide("cin-decision-field");
        this._hide("cin-speak-field");
        this._hide("cin-energy-flow");
        this._hide("cin-data-rain");
        this._setHero(false);
        if (typeof AnimationSystem !== "undefined") {
            AnimationSystem.travelLight("response", "core");
            const wrap = document.getElementById("hud-wrapper");
            if (wrap && /executing|verifying/i.test(wrap.dataset.coreState || "")) {
                AnimationSystem.setCoreState("READY");
            }
        }
    },

    advanceExec(step) {
        const g = this.el("cin-exec-graph");
        if (!g) return;
        this._execStep = step;
        g.querySelectorAll(".cin-exec-step").forEach((el) => {
            const n = Number(el.getAttribute("data-step"));
            el.classList.toggle("on", n < step);
            el.classList.toggle("active-step", n === step);
        });
        if (typeof AnimationSystem !== "undefined") {
            AnimationSystem.setCoreState(step >= 2 ? "VERIFYING" : "EXECUTING");
        }
    },

    setFileResult(ok) {
        const scan = this.el("cin-scan-field");
        if (!scan || !scan.classList.contains("active")) return;
        scan.classList.toggle("success", !!ok);
        scan.classList.toggle("error", !ok);
        if (ok) this.playSuccess();
        else this.playError();
    },

    setExecComplete() {
        const g = this.el("cin-exec-graph");
        if (g) {
            g.classList.add("complete");
            g.querySelectorAll(".cin-exec-step").forEach((el) => {
                el.classList.add("on");
                el.classList.remove("active-step");
            });
        }
        this.advanceExec(3);
        this.playSuccess();
        setTimeout(() => {
            if (typeof AnimationSystem !== "undefined") AnimationSystem.setCoreState("READY");
        }, 900);
    },

    playSuccess() {
        if (this.reduced) return;
        this._setEnv("success");
        this._show("cin-success-burst", 1050);
        setTimeout(() => this._setEnv(null), 1100);
        if (typeof AnimationSystem !== "undefined") AnimationSystem.success();
    },

    playError() {
        if (this.reduced) return;
        this._setEnv("error");
        this._setHero(true);
        this._show("cin-error-collapse", 1100);
        setTimeout(() => {
            this._setEnv(null);
            if (!document.body.classList.contains("core-listening") &&
                !document.body.classList.contains("core-thinking") &&
                !document.body.classList.contains("core-speaking")) {
                this._setHero(false);
            }
        }, 1200);
        if (typeof AnimationSystem !== "undefined") AnimationSystem.error();
    },

    buildNeuralField() {
        const svg = this.el("cin-neural-field");
        if (!svg) return;
        svg.innerHTML = "";
        svg.classList.remove("collapsing");
        const W = 900, H = 560;
        const nodes = [];
        const count = window.innerWidth < 900 ? 28 : 48;
        // staged layout: sparse ring → denser cloud around center
        for (let i = 0; i < count; i++) {
            const ang = (i / count) * Math.PI * 2;
            const ring = 0.25 + (i % 4) * 0.18;
            const jitter = (Math.random() - 0.5) * 70;
            nodes.push({
                x: W / 2 + Math.cos(ang) * (ring * Math.min(W, H) * 0.42) + jitter,
                y: H / 2 + Math.sin(ang) * (ring * Math.min(W, H) * 0.38) + jitter * 0.6,
                r: 2.2 + Math.random() * 3.2,
            });
        }
        nodes[0] = { x: W / 2, y: H / 2, r: 6 };
        const ns = "http://www.w3.org/2000/svg";
        const maxDist = window.innerWidth < 900 ? 150 : 170;
        for (let i = 0; i < nodes.length; i++) {
            for (let j = i + 1; j < nodes.length; j++) {
                const dx = nodes[i].x - nodes[j].x;
                const dy = nodes[i].y - nodes[j].y;
                const d = Math.sqrt(dx * dx + dy * dy);
                if (d < maxDist || (i === 0 && d < 260)) {
                    const line = document.createElementNS(ns, "line");
                    line.setAttribute("x1", nodes[i].x);
                    line.setAttribute("y1", nodes[i].y);
                    line.setAttribute("x2", nodes[j].x);
                    line.setAttribute("y2", nodes[j].y);
                    line.setAttribute("class", "nn-link");
                    line.style.opacity = "0";
                    line.style.transition = `opacity 0.5s ease ${40 + i * 8}ms`;
                    svg.appendChild(line);
                    requestAnimationFrame(() => {
                        line.style.opacity = String(0.2 + Math.random() * 0.45);
                    });
                }
            }
        }
        nodes.forEach((n, idx) => {
            const c = document.createElementNS(ns, "circle");
            c.setAttribute("cx", n.x);
            c.setAttribute("cy", n.y);
            c.setAttribute("r", n.r);
            c.setAttribute("class", "nn-node");
            c.style.opacity = "0";
            c.style.transition = `opacity 0.4s ease ${idx * 14}ms, transform 0.4s ease`;
            svg.appendChild(c);
            requestAnimationFrame(() => { c.style.opacity = "1"; });
        });
        const packetCount = window.innerWidth < 900 ? 5 : 8;
        for (let p = 0; p < packetCount; p++) {
            const a = nodes[1 + (p * 5) % (nodes.length - 1)];
            const b = nodes[0];
            if (!a) continue;
            const pkt = document.createElementNS(ns, "circle");
            pkt.setAttribute("r", "3");
            pkt.setAttribute("fill", "#FFB000");
            pkt.style.filter = "drop-shadow(0 0 4px #FFB000)";
            const anim = document.createElementNS(ns, "animateMotion");
            anim.setAttribute("dur", `${1.2 + p * 0.22}s`);
            anim.setAttribute("repeatCount", "indefinite");
            anim.setAttribute("path", `M${a.x} ${a.y} Q${(a.x + b.x) / 2} ${(a.y + b.y) / 2 - 40} ${b.x} ${b.y}`);
            pkt.appendChild(anim);
            svg.appendChild(pkt);
        }
    },

    collapseNeural() {
        const svg = this.el("cin-neural-field");
        if (!svg || !svg.classList.contains("active")) return;
        svg.classList.add("collapsing");
        setTimeout(() => this._hide("cin-neural-field"), 520);
    },

    initParallax() {
        // Parallax disabled — page should stay fixed under mouse move
    },
};

// ── HUD POPUPS (declutter docks) ───────────────────────
function initHudPopups() {
    document.querySelectorAll("[data-open-popup]").forEach((btn) => {
        btn.addEventListener("click", () => openHudPopup(btn.getAttribute("data-open-popup")));
    });
    const backdrop = document.getElementById("hud-popup-backdrop");
    if (backdrop) backdrop.addEventListener("click", () => closeHudPopup());
    document.querySelectorAll("[data-close-popup]").forEach((btn) => {
        btn.addEventListener("click", () => closeHudPopup());
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeHudPopup();
    });
}

function openHudPopup(name) {
    if (!name || name === "system-overview" || name === "system-health") return;
    const layer = document.getElementById("hud-popup-layer");
    if (!layer) return;
    layer.hidden = false;
    document.body.classList.add("hud-popup-open");
    layer.querySelectorAll(".hud-popup").forEach((panel) => {
        const match = panel.getAttribute("data-popup") === name;
        panel.hidden = !match;
        panel.classList.toggle("is-open", match);
        if (match) {
            panel.classList.remove("is-closing", "is-settled", "is-constructing");
            void panel.offsetWidth;
            panel.classList.add("is-entering");
            setTimeout(() => {
                panel.classList.remove("is-entering");
                panel.classList.add("is-settled");
            }, 360);
        } else {
            panel.classList.remove("is-open", "is-entering", "is-constructing", "is-settled", "is-closing");
        }
    });
    document.querySelectorAll(".ref-tool, .dock-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.getAttribute("data-open-popup") === name);
    });
}

function closeHudPopup() {
    const layer = document.getElementById("hud-popup-layer");
    if (!layer || layer.hidden) return;
    const open = layer.querySelector(".hud-popup.is-open");
    if (open) {
        open.classList.remove("is-settled", "is-entering", "is-constructing");
        open.classList.add("is-closing");
        setTimeout(() => {
            open.hidden = true;
            open.classList.remove("is-open", "is-closing");
            layer.hidden = true;
            document.body.classList.remove("hud-popup-open");
        }, 280);
    } else {
        layer.hidden = true;
        document.body.classList.remove("hud-popup-open");
    }
    document.querySelectorAll(".ref-tool, .dock-btn").forEach((btn) => btn.classList.remove("active"));
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
    SceneController._setStagePhase("idle");
    SceneController._setBodyScene("IDLE");
    initHudPopups();
    if (typeof CinematicSystem !== "undefined") {
        CinematicSystem.initParallax();
        CinematicSystem.playBoot();
    }

    // Preview: ?fx=exec | ?fx=verify
    const fx = new URLSearchParams(location.search).get("fx");
    if (fx === "exec" || fx === "verify") {
        setTimeout(() => {
            if (typeof AnimationSystem === "undefined") return;
            if (fx === "verify") {
                AnimationSystem.setCoreState("VERIFYING");
                setTimeout(() => AnimationSystem.setCoreState("READY"), 4500);
            } else {
                AnimationSystem.setCoreState("EXECUTING");
                setTimeout(() => AnimationSystem.setCoreState("VERIFYING"), 2200);
                setTimeout(() => AnimationSystem.setCoreState("READY"), 4500);
            }
        }, 1700);
    }

    // Interactive HUD affordances
    document.querySelectorAll(".module-row[role='button']").forEach((row) => {
        const activate = () => {
            const label = row.querySelector(".mod-label")?.textContent?.trim() || "module";
            quickCmd(`Status check: ${label}`);
            closeHudPopup();
        };
        row.addEventListener("click", activate);
        row.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                activate();
            }
        });
    });

    document.querySelectorAll("#state-ladder li[data-state]:not([hidden])").forEach((li) => {
        li.addEventListener("click", () => {
            if (typeof setVoiceState === "function") {
                setVoiceState(li.dataset.state);
            } else {
                document.querySelectorAll("#state-ladder li").forEach((n) => n.classList.remove("on"));
                li.classList.add("on");
            }
        });
        li.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                li.click();
            }
        });
    });

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
    const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"];
    function tick() {
        const now  = new Date();
        const hh = String(now.getHours()).padStart(2, "0");
        const mm = String(now.getMinutes()).padStart(2, "0");
        const time = `${hh}:${mm}`;
        const dd = now.getDate();
        const date = `${days[now.getDay()]}, ${dd} ${months[now.getMonth()]} ${now.getFullYear()}`;
        const clockEl = document.getElementById("clock-display");
        const dateEl  = document.getElementById("date-display");
        if (clockEl) clockEl.textContent = time;
        if (dateEl)  dateEl.textContent  = date;
        const healthClock = document.getElementById("health-clock");
        const healthDate = document.getElementById("health-date");
        if (healthClock) healthClock.textContent = time;
        if (healthDate) healthDate.textContent = date;
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
                // Update response text, but NEVER leave VOICE while the
                // mic session is live — RESPONSE dims the reactor to 15%
                // (see style.css), which made thinking/speaking look like
                // the UI was frozen on LISTENING after integration.
                updateResponsePanel(data.content, { stayInVoice: voiceActive });
                setResponseStatus("STANDBY");
                if (!voiceActive) {
                    SceneController.enter("RESPONSE");
                }
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
            if (data.state === "EXECUTING") {
                SceneController.enter("SYSTEM_ACTION", { detail: data.detail || "EXECUTING…" });
            } else if (data.state === "IDLE" || data.state === "READY" || data.state === "SPEAKING") {
                if (SceneController.current === "SYSTEM_ACTION") {
                    setTimeout(() => {
                        if (SceneController.current === "SYSTEM_ACTION") {
                            SceneController.dissolveToIdle();
                        }
                    }, 900);
                }
            }
            break;

        case "voice_state":
            // Real agent-side state pushed from agent.py's
            // agent_state_changed event via the backend.
            if (!voiceActive) break;
            {
                const raw = String(data.state || "").toLowerCase();
                if (raw === "thinking") {
                    setVoiceState("THINKING");
                    SceneController.enter("VOICE", { state: "THINKING" });
                } else if (raw === "speaking") {
                    jarvisSpeaking = true;
                    setVoiceState("SPEAKING");
                    playChime("speakStart");
                    SceneController.enter("VOICE", { state: "SPEAKING" });
                } else if (raw === "listening" || raw === "idle") {
                    // "idle" = agent ready/waiting — treat like listening
                    // while the mic session is still open.
                    jarvisSpeaking = false;
                    setVoiceState("LISTENING");
                    playChime("listenStart");
                    SceneController.enter("VOICE", { state: "LISTENING" });
                }
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
                const chip = document.getElementById("idle-mode-chip");
                if (chip) chip.textContent = data.mode || "ASSISTANT";
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
            SceneController.enter("DECISION", {
                reason: data.reason || `Confirm "${data.tool}"?`,
            });
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
    const link = document.getElementById("idle-link-status");
    if (link) {
        link.textContent = status === "ONLINE" ? "ONLINE" : status;
        link.className = "plate-val" + (status === "ONLINE" ? " ok" : "");
    }
    const dot = document.getElementById("dot-backend");
    if (dot && status === "RECONNECTING") {
        dot.classList.add("reconnecting");
    }
}

// ── FIX 4: Visual voice state ─────────────────────────
function setVoiceState(state) {
    const label = document.getElementById("voice-label");
    const btn   = document.getElementById("voice-btn");
    const normalized = (state === "ACTIVATE" || state === "IDLE") ? "READY" : state;
    const statusReadout = document.getElementById("voice-status-value");
    if (statusReadout) statusReadout.textContent = normalized === "READY" ? "IDLE" : normalized;
    if (!label || !btn) return;

    label.textContent = normalized === "READY" ? "READY" : normalized;

    btn.classList.remove("active", "speaking", "thinking", "error");
    if (normalized === "LISTENING") btn.classList.add("active");
    if (normalized === "SPEAKING")  btn.classList.add("speaking");
    if (normalized === "THINKING")  btn.classList.add("thinking");
    if (normalized === "ERROR")     btn.classList.add("error");

    const voiceChip = document.getElementById("idle-voice-chip");
    if (voiceChip) voiceChip.textContent = normalized;

    document.querySelectorAll("#state-ladder li").forEach((li) => {
        li.classList.toggle("on", li.dataset.state === normalized);
    });

    if (typeof AnimationSystem !== "undefined") {
        AnimationSystem.setCoreState(normalized);
        if (normalized === "ERROR") AnimationSystem.error();
    }
    if (typeof CinematicSystem !== "undefined") {
        CinematicSystem.onVoiceState(normalized);
    }
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
    const onlineLabel = document.getElementById("header-online-label");
    if (onlineLabel) {
        onlineLabel.textContent = data.backend === "online" ? "ONLINE" : "OFFLINE";
        onlineLabel.style.color = data.backend === "online" ? "" : "var(--red)";
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
    const raw = String(value || "").toLowerCase();
    el.textContent = String(value || "").toUpperCase();
    el.className = "info-value " + (
        raw === "online" || raw === "configured" || raw === "connected" ? "online" :
        raw === "offline" || raw === "error" ? "offline" :
        raw.includes("reconnect") || raw === "connecting" || raw === "not configured" ? "warning" : ""
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
    if (!events.length) {
        const empty = document.createElement("div");
        empty.className = "ref-event";
        empty.innerHTML = `<span class="ref-event-time">—</span><span class="ref-event-msg">No events yet</span>`;
        list.appendChild(empty);
        return;
    }
    events.slice().reverse().forEach((ev) => {
        const entry = document.createElement("div");
        entry.className = "ref-event log-entry " + (
            ev.event_type === "error" ? "error" :
            ev.event_type === "agent_start" || ev.event_type === "system_start" ? "success" : "info"
        );
        const d = new Date(ev.timestamp);
        const time = Number.isNaN(d.getTime())
            ? "—"
            : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
        entry.innerHTML = `<span class="ref-event-time">${time}</span><span class="ref-event-msg"></span>`;
        entry.querySelector(".ref-event-msg").textContent = ev.message || ev.event_type || "Event";
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

    SceneController.enter("FILES", {
        name: file.name,
        status: "QUEUED",
        clearThumb: true,
    });

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
            SceneController.update("FILES", {
                name: file.name,
                status: "READY",
                preview: dataUrl,
            });
        };
        reader.onerror = () => {
            showFileChip(file.name);
            setFileChipState("idle", "");
        };
        reader.readAsDataURL(file);
    } else {
        showFileChip(file.name);
        setFileChipState("idle", "");
        SceneController.update("FILES", {
            name: file.name,
            status: "READY",
            clearThumb: true,
        });
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
    if (SceneController.current === "FILES") SceneController.dissolveToIdle();
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
        SceneController.update("SYSTEM_ACTION", { detail: data.detail });
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

function updateResponsePanel(text, opts = {}) {
    const el = document.getElementById("response-text");
    if (!el) return;
    const stayInVoice = !!(opts && opts.stayInVoice);
    if (text && !stayInVoice) SceneController.enter("RESPONSE");
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
            // After a response settles, quietly return to idle if no
            // other scene has taken over — but never dissolve away from
            // an active voice session.
            setTimeout(() => {
                if (voiceActive) return;
                if (SceneController.current === "RESPONSE" && SceneController.phase === "settled") {
                    SceneController.dissolveToIdle();
                }
            }, 12000);
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
        (approved ? allowBtn : denyBtn).classList.add("energy-select");
        if (typeof AnimationSystem !== "undefined") {
            AnimationSystem.burst();
            if (!approved) AnimationSystem.error();
            else AnimationSystem.success();
        }
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
                if (SceneController.current === "DECISION") SceneController.dissolveToIdle();
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
            if (SceneController.current === "DECISION") SceneController.dissolveToIdle();
        } catch (e) {
            statusEl.textContent = `Request failed: ${e.message}`;
            setVoiceState("ERROR");
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

function ensureConvEmptyState() {
    const list = document.getElementById("conversation-list");
    if (!list) return;
    if (list.querySelector(".chat-bubble")) return;
    if (list.querySelector("#conv-empty")) return;
    list.innerHTML = `
        <div class="conv-empty" id="conv-empty">
            <div class="conv-empty-frame">
                <span class="conv-empty-bracket tl"></span>
                <span class="conv-empty-bracket tr"></span>
                <span class="conv-empty-bracket bl"></span>
                <span class="conv-empty-bracket br"></span>
                <div class="conv-empty-scan"></div>
                <div class="conv-empty-title">STREAM STANDBY</div>
                <div class="conv-empty-copy">Dialogue will assemble here as you speak or issue commands.</div>
                <div class="conv-empty-channels">
                    <span>TEXT</span>
                    <span>VOICE</span>
                    <span>SYSTEM</span>
                </div>
            </div>
        </div>`;
}

function clearConversation() {
    const list = document.getElementById("conversation-list");
    if (list) list.innerHTML = "";
    ensureConvEmptyState();
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
        if (typeof AnimationSystem !== "undefined") {
            await AnimationSystem.playActivationSequence();
        }
        if (typeof CinematicSystem !== "undefined") {
            CinematicSystem.onVoiceActivate();
        }
        playChime("listenStart");

        const res  = await fetch(`${BACKEND_URL}/livekit/token?room=jarvis-room&identity=user-${Date.now()}`);
        const data = await res.json();
        if (!data.token) throw new Error("No token received");
        if (data.agent_dispatched === false) {
            addLogEntry(
                "Agent dispatch failed — voice may not hear you. Check that the agent process is running.",
                "error"
            );
            if (data.dispatch_error) {
                console.warn("[VOICE] Dispatch error:", data.dispatch_error);
            }
        }

        await loadLiveKitSDK();

        const { Room, RoomEvent } = window.LivekitClient;

        // Do NOT force sampleRate: 16000 — many Windows devices reject
        // that constraint and end up with a muted/broken mic track while
        // the UI still shows LISTENING.
        livekitRoom = new Room({
            audioCaptureDefaults: {
                echoCancellation:   true,
                noiseSuppression:   true,
                autoGainControl:    true,
                channelCount:       1,
            },
            adaptiveStream: true,
            dynacast:       true,
        });

        livekitRoom.on(RoomEvent.TrackSubscribed, (track, pub, participant) => {
            if (track.kind === "audio") {
                // Only attach remote agent audio
                if (participant.isAgent || !participant.isLocal) {
                    const el = track.attach();
                    el.classList.add("jarvis-audio");
                    el.style.display = "none";
                    document.body.appendChild(el);
                    addLogEntry("JARVIS voice connected.", "success");
                    playChime("connect");

                    el.addEventListener("error", () => {
                        if (voiceActive) setVoiceState("LISTENING");
                        const err = el.error;
                        console.error("[PLAYER] ERROR: playback failed", err);
                        addLogEntry(
                            `Playback failed: ${err ? err.message || err.code : "unknown error"}`,
                            "error"
                        );
                    });

                    el.play().catch((err) => {
                        console.error("[PLAYER] ERROR: autoplay blocked or playback failed:", err);
                        addLogEntry(
                            "Playback blocked by browser autoplay policy — click anywhere on the page and retry.",
                            "error"
                        );
                    });
                }
            }
        });

        livekitRoom.on(RoomEvent.TrackUnsubscribed, (track) => {
            // Continuous agent audio can blip unsubscribe between turns.
            // Never force LISTENING here — voice_state is the source of truth.
            if (track.kind === "audio") {
                jarvisSpeaking = false;
            }
        });

        livekitRoom.on(RoomEvent.Disconnected, () => {
            addLogEntry("Voice session ended.", "info");
            deactivateVoice();
        });

        livekitRoom.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
            const agentSpeaking = speakers.some(s => !s.isLocal);
            if (agentSpeaking) {
                jarvisSpeaking = true;
            } else if (voiceActive) {
                jarvisSpeaking = false;
            }
        });

        await livekitRoom.connect(data.url, data.token);

        await livekitRoom.localParticipant.setMicrophoneEnabled(true, {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl:  true,
        });

        // Confirm the published mic is live — muted/ended tracks mean
        // the agent will never receive speech.
        try {
            const micPub = livekitRoom.localParticipant.audioTrackPublications
                .values().next().value;
            const mst = micPub && micPub.track && micPub.track.mediaStreamTrack;
            const deviceLabel = (mst && mst.label) || "(unknown)";
            console.log("[VOICE] Input device:", deviceLabel, "enabled=", mst && mst.enabled, "readyState=", mst && mst.readyState);
            addLogEntry(`Input device: ${deviceLabel}`, "info");
            if (!mst || mst.readyState === "ended" || mst.enabled === false) {
                throw new Error("Microphone track is muted or ended — check browser mic permission.");
            }
            if (/stereo mix|what u hear|wave out|loopback|monitor of/i.test(deviceLabel)) {
                addLogEntry(
                    "WARNING: selected input looks like a loopback/monitor device — this will echo your speaker output back to JARVIS.",
                    "error"
                );
            }
        } catch (e) {
            if (e && e.message && e.message.includes("Microphone track")) throw e;
            console.warn("[JARVIS] Could not read input device label:", e);
        }

        // Waveform must reuse LiveKit's published track. Opening a second
        // getUserMedia() on Windows often steals the mic from LiveKit, so
        // the UI "listens" while the agent receives silence.
        try {
            const tracks = livekitRoom.localParticipant.audioTrackPublications;
            for (const [, pub] of tracks) {
                if (pub.track && pub.track.mediaStream) {
                    micStream = pub.track.mediaStream;
                    break;
                }
                if (pub.track && pub.track.mediaStreamTrack) {
                    micStream = new MediaStream([pub.track.mediaStreamTrack]);
                    break;
                }
            }
        } catch (e) {
            console.warn("[JARVIS] Could not get mic stream for visualiser:", e);
        }

        if (micStream) startAudioVisualiser(micStream);

        // Wait for the agent worker to actually join — otherwise we sit
        // on LISTENING forever with nobody subscribed to the mic.
        const agentReady = await waitForVoiceAgent(livekitRoom, 20000);
        if (!agentReady) {
            addLogEntry(
                "Voice agent did not join the room. Restart the backend (agent process) and try again.",
                "error"
            );
            updateResponsePanel(
                "Voice agent is not in the room, Sir. I can hear nothing until it joins — restart JARVIS and try voice again."
            );
            await deactivateVoice();
            return;
        }
        addLogEntry("Voice agent joined the room.", "success");

        voiceActive = true;

        if (btn) btn.classList.add("active");
        if (mic) mic.classList.add("active");
        setVoiceState("LISTENING");
        SceneController.enter("VOICE", { state: "LISTENING" });

        document.getElementById("audio-status").textContent = "ACTIVE";
        addLogEntry("Voice assistant activated.", "success");
        const resp = document.getElementById("response-text");
        if (resp) {
            clearTypewriter();
            resp.classList.remove("thinking", "markdown-body", "typing-cursor");
            resp.textContent = "Voice activated. Listening, Sir.";
        }
    } catch (e) {
        console.error("[JARVIS] Voice error:", e);
        addLogEntry("Voice activation failed: " + e.message, "error");
        updateResponsePanel("Voice system unavailable. Using text interface, Sir.");
        voiceActive = false;
        try { await deactivateVoice(); } catch (_) {}
    }
}

function roomHasVoiceAgent(room) {
    if (!room) return false;
    for (const p of room.remoteParticipants.values()) {
        if (p.isAgent) return true;
        const id = (p.identity || "").toLowerCase();
        const name = (p.name || "").toLowerCase();
        if (id.includes("agent") || id.includes("jarvis") || name.includes("jarvis")) {
            return true;
        }
    }
    return false;
}

function waitForVoiceAgent(room, timeoutMs) {
    if (roomHasVoiceAgent(room)) return Promise.resolve(true);
    const { RoomEvent } = window.LivekitClient;
    return new Promise((resolve) => {
        let settled = false;
        const finish = (ok) => {
            if (settled) return;
            settled = true;
            try { room.off(RoomEvent.ParticipantConnected, onJoin); } catch (_) {}
            clearTimeout(timer);
            resolve(ok);
        };
        const onJoin = () => {
            if (roomHasVoiceAgent(room)) finish(true);
        };
        const timer = setTimeout(() => finish(false), timeoutMs);
        room.on(RoomEvent.ParticipantConnected, onJoin);
        // Agent may already be mid-join
        setTimeout(() => {
            if (roomHasVoiceAgent(room)) finish(true);
        }, 250);
    });
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

    // Do not stop() tracks on micStream — it usually wraps LiveKit's
    // published MediaStreamTrack, which disconnect() already releases.
    // Stopping here caused Windows devices to leave the mic in a bad
    // state for the next activateVoice() call.
    micStream = null;

    if (audioContext) {
        try { await audioContext.close(); } catch (e) {}
        audioContext = null;
        analyser     = null;
    }

    voiceActive    = false;
    jarvisSpeaking = false;

    if (btn) btn.classList.remove("active", "speaking");
    if (mic) mic.classList.remove("active");
    setVoiceState("READY");
    if (SceneController.current === "VOICE") SceneController.dissolveToIdle();

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
    const BASE_R = 90;
    const MAX_SPIKE = 22;
    const POINTS = 48;
    let lastIdle = 0;
    let idlePts = "";

    function frame(now) {
        requestAnimationFrame(frame);
        poly.classList.toggle("jarvis-speaking", jarvisSpeaking);

        if (analyser && voiceActive) {
            const bufLen = analyser.frequencyBinCount;
            const data = new Uint8Array(bufLen);
            analyser.getByteTimeDomainData(data);
            const pts = [];
            for (let i = 0; i < POINTS; i++) {
                const dataIdx = Math.floor((i / POINTS) * bufLen);
                const v = (data[dataIdx] - 128) / 128.0;
                const r = BASE_R + v * MAX_SPIKE;
                const angle = (i / POINTS) * Math.PI * 2;
                pts.push(`${(CX + r * Math.cos(angle)).toFixed(1)},${(CY + r * Math.sin(angle)).toFixed(1)}`);
            }
            poly.setAttribute("points", pts.join(" "));
            return;
        }

        // Idle: update infrequently — static soft circle
        if (now - lastIdle < 200) return;
        lastIdle = now;
        const t = now / 1000;
        const breathe = Math.sin(t * 0.6) * 1.5;
        const pts = [];
        for (let i = 0; i < POINTS; i++) {
            const angle = (i / POINTS) * Math.PI * 2;
            const r = BASE_R * 0.5 + breathe;
            pts.push(`${(CX + r * Math.cos(angle)).toFixed(1)},${(CY + r * Math.sin(angle)).toFixed(1)}`);
        }
        idlePts = pts.join(" ");
        poly.setAttribute("points", idlePts);
    }
    requestAnimationFrame(frame);
}

function drawWaveform() {
    const canvas = document.getElementById("waveform-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d", { alpha: true });
    let lastIdleDraw = 0;

    function size() {
        const w = Math.max(1, Math.floor(canvas.clientWidth || 240));
        if (canvas.width !== w) canvas.width = w;
    }

    function drawIdleLine() {
        size();
        const W = canvas.width;
        const H = canvas.height;
        ctx.clearRect(0, 0, W, H);
        ctx.beginPath();
        ctx.strokeStyle = "rgba(212, 160, 23, 0.28)";
        ctx.lineWidth = 1.25;
        ctx.shadowBlur = 0;
        const midY = H / 2;
        ctx.moveTo(0, midY);
        ctx.lineTo(W, midY);
        ctx.stroke();
    }

    function draw() {
        waveformAnimId = requestAnimationFrame(draw);
        if (!(analyser && voiceActive)) {
            const now = performance.now();
            if (now - lastIdleDraw > 400) {
                drawIdleLine();
                lastIdleDraw = now;
            }
            return;
        }
        size();
        const W = canvas.width;
        const H = canvas.height;
        ctx.clearRect(0, 0, W, H);
        const bufLen = analyser.frequencyBinCount;
        const data = new Uint8Array(bufLen);
        analyser.getByteTimeDomainData(data);
        // downsample for smoother/cheaper draw
        const step = Math.max(1, Math.floor(bufLen / Math.min(W, 180)));
        ctx.beginPath();
        ctx.strokeStyle = jarvisSpeaking
            ? "rgba(111, 164, 125, 0.9)"
            : "rgba(255, 176, 0, 0.9)";
        ctx.lineWidth = 1.5;
        ctx.shadowBlur = 0;
        let x = 0;
        const sliceW = (W * step) / bufLen;
        for (let i = 0; i < bufLen; i += step) {
            const v = data[i] / 128.0;
            const y = (v * H) / 2;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
            x += sliceW;
        }
        ctx.stroke();
    }
    drawIdleLine();
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
    const ctx = canvas.getContext("2d", { alpha: true });
    const reduced = typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
        canvas.style.display = "none";
        return;
    }

    function resize() {
        const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
        canvas.width = Math.floor(window.innerWidth * dpr);
        canvas.height = Math.floor(window.innerHeight * dpr);
        canvas.style.width = window.innerWidth + "px";
        canvas.style.height = window.innerHeight + "px";
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    let resizeTimer = 0;
    window.addEventListener("resize", () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(resize, 120);
    });

    const isNarrow = window.innerWidth < 1100;
    const PARTICLE_COUNT = isNarrow ? 16 : 24;
    const particles = [];
    let burstSparks = [];
    let coreCache = { x: window.innerWidth / 2, y: window.innerHeight / 2 };
    let coreTick = 0;
    let lastFrame = 0;

    for (let i = 0; i < PARTICLE_COUNT; i++) {
        particles.push({
            x: Math.random() * window.innerWidth,
            y: Math.random() * window.innerHeight,
            vx: (Math.random() - 0.5) * 0.18,
            vy: (Math.random() - 0.5) * 0.18,
            radius: Math.random() * 1.1 + 0.25,
            opacity: Math.random() * 0.28 + 0.06,
            attract: Math.random() > 0.6,
        });
    }

    window.__jarvisParticleBurst = function (cx, cy, color) {
        const n = isNarrow ? 12 : 18;
        for (let i = 0; i < n; i++) {
            const ang = (Math.PI * 2 * i) / n + Math.random() * 0.2;
            const sp = 1.1 + Math.random() * 2;
            burstSparks.push({
                x: cx, y: cy,
                vx: Math.cos(ang) * sp,
                vy: Math.sin(ang) * sp,
                life: 1,
                radius: 1 + Math.random(),
                color: color || "255,176,0",
            });
        }
    };

    function refreshCore() {
        const el = document.getElementById("hud-wrapper") || document.getElementById("voice-btn");
        if (!el) return;
        const r = el.getBoundingClientRect();
        coreCache = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }

    function drawParticles(now) {
        requestAnimationFrame(drawParticles);
        const mode = (typeof AnimationSystem !== "undefined" && AnimationSystem.particleMode) || "idle";
        const active = mode !== "idle";
        // Idle: ~20fps. Active: full rate.
        const minDelta = active ? 16 : 48;
        if (now - lastFrame < minDelta) return;
        lastFrame = now;

        const w = window.innerWidth;
        const h = window.innerHeight;
        ctx.clearRect(0, 0, w, h);

        if (++coreTick % 30 === 0) refreshCore();

        const linkAlpha = active ? 0.08 : 0;
        const speed = mode === "thinking" ? 1.5 : mode === "voice" ? 1.25 : mode === "speaking" ? 1.35 : 1;
        let rgb = "255,176,0";
        if (mode === "error") rgb = "200,60,60";

        // Skip expensive link graph when idle
        if (active && linkAlpha > 0) {
            const maxD = mode === "thinking" ? 110 : 90;
            for (let i = 0; i < particles.length; i++) {
                for (let j = i + 1; j < particles.length; j++) {
                    const dx = particles[i].x - particles[j].x;
                    const dy = particles[i].y - particles[j].y;
                    const dist = Math.sqrt(dx * dx + dy * dy);
                    if (dist < maxD) {
                        ctx.beginPath();
                        ctx.strokeStyle = `rgba(${rgb}, ${linkAlpha * (1 - dist / maxD)})`;
                        ctx.lineWidth = 0.5;
                        ctx.moveTo(particles[i].x, particles[i].y);
                        ctx.lineTo(particles[j].x, particles[j].y);
                        ctx.stroke();
                    }
                }
            }
        }

        ctx.shadowBlur = 0;
        particles.forEach((p) => {
            if ((mode === "voice" || mode === "thinking") && p.attract) {
                p.vx += (coreCache.x - p.x) * 0.00003;
                p.vy += (coreCache.y - p.y) * 0.00003;
            }
            ctx.beginPath();
            ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(${rgb}, ${p.opacity * (active ? 1.3 : 0.85)})`;
            ctx.fill();
            p.x += p.vx * speed;
            p.y += p.vy * speed;
            p.vx *= 0.996;
            p.vy *= 0.996;
            if (p.x < 0) p.x = w;
            if (p.x > w) p.x = 0;
            if (p.y < 0) p.y = h;
            if (p.y > h) p.y = 0;
        });

        if (burstSparks.length) {
            burstSparks = burstSparks.filter((s) => s.life > 0.02);
            burstSparks.forEach((s) => {
                ctx.beginPath();
                ctx.arc(s.x, s.y, s.radius * s.life, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(${s.color}, ${s.life})`;
                ctx.fill();
                s.x += s.vx;
                s.y += s.vy;
                s.life *= 0.93;
            });
        }
    }
    refreshCore();
    requestAnimationFrame(drawParticles);
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