# server/tts.py
"""Text-to-speech with Piper (local, no API key).

Uses the Piper Python API (piper-tts / OHF-Voice piper1-gpl). The voice model
loads once at startup and is reused, so synthesis is just a function call — no
per-utterance process spawn, and no dependence on CLI flag names.

Setup: download a voice with
    python -m piper.download_voices en_US-lessac-medium --data-dir ../voices
then point PIPER_MODEL at the resulting .onnx (its .onnx.json sits beside it).
"""
from __future__ import annotations

import io
import logging
import os
import wave

from piper import PiperVoice

log = logging.getLogger("connector.tts")


class TTSError(RuntimeError):
    pass


class TTS:
    def __init__(self, model_path: str) -> None:
        if not model_path:
            raise TTSError("PIPER_MODEL is not set — point it at a Piper .onnx voice file.")
        if not os.path.exists(model_path):
            raise TTSError(f"Piper voice not found: {model_path}")
        log.info("loading Piper voice: %s", model_path)
        self.voice = PiperVoice.load(model_path)

    def synthesize(self, text: str) -> bytes:
        """Return WAV bytes for the given text."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            self.voice.synthesize_wav(text, wav_file)
        return buf.getvalue()
