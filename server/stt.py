# server/stt.py
"""Speech-to-text with faster-whisper (local, no API key).

The model loads once at startup and is reused. `base.en` is a good speed/accuracy
balance on a Mac CPU; `small`/`medium` are multilingual (needed for e.g. Bulgarian).

Two things that matter a lot for a *room mic on a small robot*:
  * Loudness. The robot mic tends to be quiet; we RMS-normalize each clip so quiet
    speech isn't treated as silence.
  * VAD. faster-whisper's `vad_filter` trims non-speech, but with quiet audio it
    over-trims and whisper then hallucinates from the fragment. We keep VAD but with
    a low threshold (the push-to-talk button already delimits the utterance).
"""
from __future__ import annotations

import logging
import wave

import numpy as np
from faster_whisper import WhisperModel

log = logging.getLogger("connector.stt")


def _load_wav_mono_f32(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    return audio, sr


def _normalize_rms(audio: np.ndarray, target_rms: float = 0.15, max_gain: float = 15.0) -> np.ndarray:
    """Bring quiet speech up to a consistent level (robust to peaks, capped so we
    don't blow up background noise in near-silent clips)."""
    if audio.size == 0:
        return audio
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    if rms < 1e-4:  # essentially silent — leave it, don't amplify noise
        return audio
    gain = min(target_rms / rms, max_gain)
    return np.clip(audio * gain, -1.0, 1.0)


class STT:
    def __init__(
        self,
        model: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "",
        vad_threshold: float = 0.2,
    ) -> None:
        log.info("loading faster-whisper model=%s device=%s compute=%s language=%s vad=%.2f",
                 model, device, compute_type, language or "(auto)", vad_threshold)
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        self.language = language or None
        self.vad_threshold = vad_threshold

    def transcribe(self, wav_path: str) -> str:
        audio, sr = _load_wav_mono_f32(wav_path)

        if sr == 16000:
            audio = _normalize_rms(audio)
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            log.info("audio: %.2fs, peak=%.2f (normalized)", audio.size / 16000, peak)
            source: object = audio
        else:
            # Unexpected rate: let faster-whisper resample the file itself, skip normalize.
            log.warning("expected 16 kHz, got %d — passing file through unnormalized", sr)
            source = wav_path

        segments, _info = self.model.transcribe(
            source,
            language=self.language,
            vad_filter=True,
            vad_parameters=dict(threshold=self.vad_threshold),
        )
        text = "".join(seg.text for seg in segments).strip()
        log.info("transcript: %r", text)
        return text
