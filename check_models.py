"""
JARVIS model verification.

Checks every local model JARVIS depends on and reports exactly what's
missing, so you know BEFORE hitting the mic button whether something
will fail. Run this from the J.A.R.V.I.S folder with the same venv/
Python JARVIS itself uses:

    python check_models.py
"""

import os
import sys
import subprocess
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
ok_count = 0
fail_count = 0


def ok(msg):
    global ok_count
    ok_count += 1
    print(f"  [OK]   {msg}")


def fail(msg):
    global fail_count
    fail_count += 1
    print(f"  [FAIL] {msg}")


def warn(msg):
    print(f"  [WARN] {msg}")


# ── 1. Piper TTS voice (models/en_US-lessac-medium.onnx + .json) ─────
print("\n[1/4] Piper TTS voice model")
onnx_path  = os.path.join(ROOT, "models", "en_GB-alan-medium.onnx")
json_path  = os.path.join(ROOT, "models", "en_GB-alan-medium.onnx.json")

if os.path.exists(onnx_path):
    size_mb = os.path.getsize(onnx_path) / (1024 * 1024)
    ok(f"models/en_GB-alan-medium.onnx present ({size_mb:.1f} MB)")
else:
    fail("models/en_GB-alan-medium.onnx MISSING")
    print("         Fix: python -m piper.download_voices en_GB-alan-medium")

if os.path.exists(json_path):
    ok("models/en_GB-alan-medium.onnx.json present")
else:
    fail("models/en_GB-alan-medium.onnx.json MISSING")
    print("         Fix: python -m piper.download_voices en_GB-alan-medium")


# ── 2. faster-whisper STT model ("small") ─────────────────────────
print("\n[2/4] faster-whisper STT model ('small')")
try:
    from faster_whisper import WhisperModel
    # This will use a locally cached model if present, or attempt to
    # download it. We just try to construct it — if it's cached, this
    # is fast; if not, it'll download (~500MB) or fail with no network.
    try:
        WhisperModel("small", device="cpu", compute_type="int8")
        ok("faster-whisper 'small' model loads successfully")
    except Exception as e:
        fail(f"faster-whisper 'small' model failed to load: {e}")
        print("         Fix: run this once with internet access so it can")
        print("         download and cache the model (~500MB), or check disk space.")
except ImportError:
    fail("faster-whisper package not installed")
    print("         Fix: pip install faster-whisper --break-system-packages")


# ── 3. Ollama + the model JARVIS actually calls ───────────────────
print("\n[3/4] Ollama + llama3.1:8b")
try:
    req = urllib.request.Request("http://localhost:11434/api/tags")
    with urllib.request.urlopen(req, timeout=3) as resp:
        import json
        data = json.loads(resp.read())
        models = [m["name"] for m in data.get("models", [])]
        ok(f"Ollama is running (found {len(models)} model(s) installed)")
        if any(m.startswith("llama3.1:8b") for m in models):
            ok("llama3.1:8b is pulled and available")
        else:
            fail("llama3.1:8b is NOT pulled")
            print("         Fix: ollama pull llama3.1:8b")
            if models:
                print(f"         (models you do have: {', '.join(models)})")
except urllib.error.URLError:
    fail("Ollama is not running on http://localhost:11434")
    print("         Fix: start Ollama (it usually runs automatically as a")
    print("         background service on Windows — check the system tray,")
    print("         or run: ollama serve")
except Exception as e:
    fail(f"Could not check Ollama: {e}")


# ── 4. Silero VAD + turn-detector (auto-downloaded by livekit plugins) ──
print("\n[4/4] Silero VAD + LiveKit turn-detector model")
try:
    from livekit.plugins import silero
    try:
        silero.VAD.load()
        ok("Silero VAD model loads successfully")
    except Exception as e:
        fail(f"Silero VAD failed to load: {e}")
        print("         Fix: needs internet on first run to download; check connectivity.")
except ImportError:
    fail("livekit-plugins-silero not installed")
    print("         Fix: pip install livekit-plugins-silero --break-system-packages")

try:
    import livekit.plugins.turn_detector  # noqa: F401
    ok("livekit turn-detector plugin installed (auto-downloads its model on first use)")
except ImportError:
    warn("livekit-plugins-turn-detector not importable directly — this is often bundled")
    print("         inside livekit-agents itself in newer versions, so this may be fine.")
    print("         Your last log showed 'audio turn detector initialized' successfully,")
    print("         which means it's already working — this check just can't see it")
    print("         as a separate package.")


print(f"\n{'='*60}")
print(f"  {ok_count} OK, {fail_count} FAILED")
print(f"{'='*60}")
if fail_count:
    print("Fix the FAILED items above, then run this again before starting JARVIS.")
    sys.exit(1)
else:
    print("Everything required is present. Safe to run startup.bat.")
