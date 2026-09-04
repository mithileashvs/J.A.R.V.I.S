"""
Local speech-to-text for JARVIS using faster-whisper.

Runs entirely on your machine — no audio ever leaves your laptop.
Plugs into LiveKit's AgentSession as a standard STT provider.
"""

import asyncio
import io
import logging
import time
import wave

import numpy as np
from faster_whisper import WhisperModel

from livekit import rtc
from livekit.agents import APIConnectionError, APIConnectOptions, stt

logger = logging.getLogger("jarvis-whisper-stt")


class LocalWhisperSTT(stt.STT):
    """Non-streaming local STT backed by faster-whisper.

    Non-streaming means JARVIS waits for you to stop talking (handled by
    the VAD/turn-detection in AgentSession), then transcribes the whole
    utterance in one go. This is simpler and more reliable on CPU-only
    machines than trying to stream partial results.
    """

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
    ):
        """
        model_size: tiny / base / small / medium / large-v3
                    (bigger = more accurate, slower on CPU. "small" is a
                    reasonable balance for a laptop with no GPU.)
        device:     "cpu" or "cuda" if you have an NVIDIA GPU set up.
        compute_type: "int8" is the fastest sensible option on CPU.
        """
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False)
        )
        self._language = language
        logger.info(f"Loading faster-whisper model '{model_size}' on {device}...")
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        logger.info("faster-whisper model loaded and ready.")

    async def _recognize_impl(
        self,
        buffer,
        *,
        language: str | None,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        try:
            print("[VOICE] Audio captured, recognizing...")
            wav_bytes = rtc.combine_audio_frames(buffer).to_wav_bytes()

            # BUG FIX: to_wav_bytes() returns a full WAV file, including the
            # ~44-byte RIFF/WAVE header. Reading it straight into np.frombuffer
            # treated the header bytes as audio samples, which corrupted the
            # start of every utterance with noise spikes and threw off
            # sample alignment for everything after it. Use `wave` to parse
            # the file properly and pull out only the PCM payload.
            with wave.open(io.BytesIO(wav_bytes), "rb") as wav_reader:
                n_channels  = wav_reader.getnchannels()
                sample_rate = wav_reader.getframerate()
                n_frames    = wav_reader.getnframes()
                pcm_bytes   = wav_reader.readframes(n_frames)

            # 16-bit PCM -> float32 in [-1, 1], what faster-whisper expects
            audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

            # BUG FIX: the code previously assumed the WAV was always
            # mono and fed the raw interleaved samples straight to
            # Whisper. WebRTC/Opus audio frequently decodes to STEREO
            # even when mono capture was requested client-side — when
            # that happens, samples are interleaved as L,R,L,R,... and
            # reading them as one continuous mono stream scrambles the
            # waveform (each "sample" alternates between two different
            # channels). That's the kind of corruption that produces
            # confident-but-wrong transcriptions rather than an outright
            # failure — exactly the "recognizes completely different
            # words" symptom. Downmix to mono properly, same approach
            # already used in piper_tts.py for its own stereo output.
            if n_channels > 1:
                audio = audio.reshape(-1, n_channels).mean(axis=1)
                logger.info(f"[whisper] downmixed {n_channels}-channel audio to mono")

            print(f"[VOICE] Audio format: {n_channels}ch @ {sample_rate}Hz, {audio.size} samples ({audio.size / max(sample_rate,1):.2f}s)")

            if audio.size == 0:
                print("[VOICE] Could not understand audio (empty buffer)")
                logger.warning("[whisper] received empty audio buffer, skipping transcription")
                return stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[stt.SpeechData(text="", language=language or self._language)],
                )

            start = time.time()
            # BUG FIX (root cause of "stuck in LISTENING"): faster-whisper's
            # transcribe() is a synchronous, CPU-bound call — on "medium"
            # with beam_size=3 on CPU this can easily take several
            # seconds. Calling it directly here, inside an `async def`,
            # blocked the ONE asyncio event loop this whole agent process
            # runs on for that entire duration: no state-change events,
            # no LiveKit heartbeats/keepalives, no backend POSTs could
            # run until it finished. That's indistinguishable from "JARVIS
            # is stuck" from the browser side, and if the block lasted
            # long enough it could cause the room connection itself to
            # time out — with no useful error, since the exception-timer
            # logic lives on the very loop that's frozen. Note
            # piper_tts.py already does this correctly for its own
            # blocking synth call (see its loop.run_in_executor usage) —
            # this file just never got the same fix. Run the blocking
            # work in a thread instead so the event loop stays responsive.
            language_to_use = language or self._language
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(
                None, self._transcribe_sync, audio, language_to_use
            )
            logger.info(f"[whisper] transcribed in {time.time() - start:.2f}s: {text!r}")

            if text:
                print(f"[VOICE] Recognized: \"{text}\"")
            else:
                print("[VOICE] Could not understand audio")

            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(text=text, language=language or self._language)],
            )
        except Exception as e:
            print(f"[VOICE ERROR] Speech recognition failed: {e}")
            logger.error(f"Whisper transcription failed: {e}", exc_info=True)
            raise APIConnectionError() from e

    def _transcribe_sync(self, audio: np.ndarray, language: str) -> str:
        """The actual blocking faster-whisper call — always run this via
        loop.run_in_executor from async code, never call it directly on
        the event loop (see the BUG FIX note in _recognize_impl)."""
        segments, _info = self._model.transcribe(
            audio,
            language=language,
            # FIX (accuracy): bumped from 2 -> 3 now that there's
            # confirmed latency headroom. Not pushing all the way to
            # 5 in the same round as the model-size upgrade above —
            # changing two expensive knobs at once makes it hard to
            # tell which one helped if it's still not fast enough;
            # this is a controlled middle step.
            beam_size=3,
            condition_on_previous_text=False,
            vad_filter=True,
            # FIX (accuracy, zero latency cost): Whisper decodes with
            # context — giving it a hint about the vocabulary it's
            # likely to hear meaningfully improves recognition of
            # short/ambiguous words, and directly targets the exact
            # failure we saw ("JARVIS" misheard as "Pudgy!"). This
            # doesn't slow anything down; it just biases decoding.
            initial_prompt=(
                "JARVIS, open Chrome, what's the weather, search for, "
                "send an email, what time is it, open notepad, "
                "open calculator, hello JARVIS, thank you JARVIS."
            ),
        )
        # segments is a lazy generator -- the actual model computation
        # happens as it's iterated, which is why this whole method (not
        # just the .transcribe() call above) needs to run off the event
        # loop thread.
        return " ".join(seg.text.strip() for seg in segments).strip()
