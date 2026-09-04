"""
Local text-to-speech for JARVIS using Piper.

Runs entirely on your machine via the piper-tts Python package — no
Docker, no subprocess, no server to keep running. Plugs into LiveKit's
AgentSession as a standard TTS provider.
"""

import asyncio
import logging
import os
import uuid

import numpy as np
from piper import PiperVoice, SynthesisConfig

from livekit.agents import tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

logger = logging.getLogger("jarvis-piper-tts")

SAMPLE_RATE = 22050  # matches most Piper voice models (check your .onnx.json if unsure)


class LocalPiperTTS(tts.TTS):
    """Non-streaming local TTS backed by Piper.

    Non-streaming means JARVIS generates the full reply as audio before
    playing it back — simplest and most reliable option for a first pass.
    """

    def __init__(
        self,
        model_path: str,
        speed: float = 1.0,
        volume: float = 1.0,
        use_cuda: bool = False,
        noise_scale: float = 0.85,
        noise_w_scale: float = 1.0,
    ):
        """
        model_path: path to the downloaded .onnx voice model
                    (e.g. "models/en_US-lessac-medium.onnx")
        speed:      1.0 = normal, <1.0 = faster, >1.0 = slower
        use_cuda:   requires the onnxruntime-gpu package if True
        noise_scale:   controls vocal expressiveness/variation (Piper default
                        is ~0.667, which reads as flat/monotone). Raised to
                        0.85 by default here — more natural pitch/energy
                        variation between words, still clean at speech.
        noise_w_scale: controls variation in how long each sound is held
                        (Piper default ~0.8). Raised to 1.0 by default here —
                        less mechanically even timing, more like natural
                        speech rhythm. Push much past ~1.2 and it starts
                        sounding slurred instead of natural, so this is a
                        deliberately modest bump, not maxed out.
        """
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=1,
        )
        logger.info(f"Loading Piper voice model: {model_path}")

        # Piper requires a JSON config file alongside the .onnx model,
        # named "<model_path>.json" by default. Fail loudly and early
        # with a clear message instead of letting a bare FileNotFoundError
        # propagate and silently kill the agent process before TTS ever
        # gets a chance to speak.
        config_path = f"{model_path}.json"
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"[VOICE ERROR] Piper voice model not found at '{model_path}'. "
                f"Download it (e.g. with `python -m piper.download_voices "
                f"en_US-lessac-medium`) and place it in the models/ folder."
            )
        if not os.path.exists(config_path):
            raise FileNotFoundError(
                f"[VOICE ERROR] Piper voice config not found at '{config_path}'. "
                f"Piper needs both the .onnx model AND a matching .onnx.json "
                f"config file. Download it (e.g. with `python -m piper.download_voices "
                f"en_US-lessac-medium`) so both files land in models/."
            )

        self._voice = PiperVoice.load(model_path, use_cuda=use_cuda)
        self._speed = speed
        self._volume = volume
        self._noise_scale = noise_scale
        self._noise_w_scale = noise_w_scale
        logger.info("Piper voice model loaded and ready.")
        print("[VOICE] TTS engine (Piper) loaded and ready.")

    def synthesize(self, text: str, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        return _PiperStream(tts_plugin=self, text=text, conn_options=conn_options)


class _PiperStream(tts.ChunkedStream):
    def __init__(self, *, tts_plugin: LocalPiperTTS, text: str, conn_options):
        super().__init__(tts=tts_plugin, input_text=text, conn_options=conn_options)
        self._plugin = tts_plugin

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        # ROOT CAUSE OF "JARVIS NEVER SPEAKS", finally confirmed from a
        # real traceback:
        #   TypeError: _PiperStream._run() takes 1 positional argument
        #   but 2 were given
        #
        # The installed livekit-agents version's TTS.ChunkedStream base
        # class calls `await self._run(output_emitter)` — passing an
        # AudioEmitter that the subclass is required to write audio
        # into. This file was written against an older API where _run()
        # took no arguments and pushed SynthesizedAudio events directly
        # onto self._event_ch. That signature mismatch meant EVERY
        # single TTS call crashed immediately with a TypeError before
        # producing one byte of audio — which is why no voice has ever
        # come out, through every round of this debugging session, even
        # though the LLM's replies were being generated correctly the
        # whole time (see agent.py's conversation_item_added log showing
        # real response text). The crash was happening in a background
        # task whose exception was never surfaced to the console in an
        # obvious way ("Task exception was never retrieved") — that's
        # why this stayed hidden until we got the full raw log.
        #
        # Fixed to the current API: initialize() the emitter with the
        # audio format up front, then push() raw PCM bytes into it —
        # the framework handles chunking/framing internally now instead
        # of the plugin constructing rtc.AudioFrame objects itself.
        output_emitter.initialize(
            request_id=str(uuid.uuid4()),
            sample_rate=SAMPLE_RATE,
            num_channels=1,
            mime_type="audio/pcm",
        )

        if not self.input_text or not self.input_text.strip():
            # Don't attempt to speak empty text — nothing to synthesize,
            # and Piper can throw or produce a click/silence for empty input.
            logger.warning("[piper] skipping synthesis: empty input text")
            return

        print("[VOICE] Speaking...")
        config = SynthesisConfig(
            volume=self._plugin._volume,
            length_scale=self._plugin._speed,
            noise_scale=self._plugin._noise_scale,
            noise_w_scale=self._plugin._noise_w_scale,
            normalize_audio=True,
        )
        loop = asyncio.get_event_loop()
        try:
            audio_chunks = await loop.run_in_executor(None, self._synthesize, config)
            if not audio_chunks:
                print("[VOICE ERROR] TTS produced no audio output")
                logger.warning("[piper] synthesis produced zero audio chunks")
                return
            for chunk in audio_chunks:
                output_emitter.push(chunk)
            print("[VOICE] Speaking finished.")
        except Exception as e:
            print(f"[VOICE ERROR] TTS synthesis failed: {e}")
            logger.error(f"Piper synthesis failed: {e}", exc_info=True)

    def _synthesize(self, config: SynthesisConfig) -> list[bytes]:
        chunks = []
        for chunk in self._plugin._voice.synthesize(self.input_text, syn_config=config):
            audio = chunk.audio_int16_bytes
            if chunk.sample_channels == 2:
                stereo = np.frombuffer(audio, dtype=np.int16)
                audio = stereo.reshape(-1, 2).mean(axis=1).astype(np.int16).tobytes()
            chunks.append(audio)
        return chunks
