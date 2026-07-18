# server/stt.py
"""Speech-to-text with faster-whisper (local, no API key).

The model loads once at startup and is reused. `base.en` is a good speed/accuracy
balance on a Mac CPU; `small`/`medium` are multilingual (needed for e.g. Bulgarian).

Two things that matter a lot for a *room mic on a small robot*:
  * Loudness. We **peak**-normalize each clip (scale so the loudest sample hits ~0.95).
    This lifts genuinely quiet audio without ever clipping loud audio — unlike RMS
    normalization, which amplified already-loud clips into distortion.
  * VAD. faster-whisper's `vad_filter` mangles this audio badly (measured: it turned
    recognizable speech into garbage), so it's **OFF by default** — the push-to-talk
    button already delimits the utterance, so there's nothing for VAD to do.
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


def _normalize_peak(audio: np.ndarray, target: float = 0.95) -> np.ndarray:
    """Scale so the loudest sample is `target`. Boosts quiet audio, never clips."""
    if audio.size == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak < 1e-4:  # essentially silent — leave it
        return audio
    return audio * (target / peak)


class STT:
    def __init__(
        self,
        model: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "",
        vad_filter: bool = False,
        vad_threshold: float = 0.2,
    ) -> None:
        log.info("loading faster-whisper model=%s device=%s compute=%s language=%s vad=%s",
                 model, device, compute_type, language or "(auto)", vad_filter)
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        self.language = language or None
        self.vad_filter = vad_filter
        self.vad_threshold = vad_threshold

    def transcribe(self, wav_path: str) -> str:
        audio, sr = _load_wav_mono_f32(wav_path)

        if sr == 16000:
            audio = _normalize_peak(audio)
            rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
            log.info("audio: %.2fs, rms=%.3f (peak-normalized)", audio.size / 16000, rms)
            source: object = audio
        else:
            log.warning("expected 16 kHz, got %d — passing file through", sr)
            source = wav_path

        kwargs = {"language": self.language, "vad_filter": self.vad_filter}
        if self.vad_filter:
            kwargs["vad_parameters"] = dict(threshold=self.vad_threshold)
        segments, _info = self.model.transcribe(source, **kwargs)
        text = "".join(seg.text for seg in segments).strip()
        log.info("transcript: %r", text)
        return text
