# reachy_app/vad.py
"""End-of-speech detection via trailing silence (RMS).

Deliberately dependency-free: no torch, no silero — keeps the Pi light. You feed it
each captured audio block; it returns True once it has heard some speech and then a
run of silence long enough to call the utterance finished.

This is the v1 endpointer for the wake-word path (the phone button doesn't need it —
button release *is* the end signal). Silero VAD is a drop-in upgrade later if the
RMS gate proves too blunt in a noisy room.
"""
from __future__ import annotations

import numpy as np


class SilenceEndpointer:
    def __init__(
        self,
        samplerate: int,
        *,
        rms_threshold: float = 0.015,
        silence_ms: int = 800,
        min_speech_ms: int = 300,
    ) -> None:
        self.samplerate = samplerate
        self.rms_threshold = rms_threshold
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.reset()

    def reset(self) -> None:
        self._speech_ms = 0.0
        self._trailing_silence_ms = 0.0
        self._heard_speech = False

    def feed(self, block: np.ndarray) -> bool:
        """Return True when the utterance looks finished."""
        if block.size == 0:
            return False
        block_ms = 1000.0 * block.size / self.samplerate
        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))

        if rms >= self.rms_threshold:
            self._speech_ms += block_ms
            self._trailing_silence_ms = 0.0
            if self._speech_ms >= self.min_speech_ms:
                self._heard_speech = True
        else:
            if self._heard_speech:
                self._trailing_silence_ms += block_ms

        return self._heard_speech and self._trailing_silence_ms >= self.silence_ms
