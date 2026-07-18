# server/stt.py
"""Speech-to-text with faster-whisper (local, no API key).

The model loads once at startup and is reused. `base.en` is a good speed/accuracy
balance on a Mac CPU; bump to `small.en` for better accuracy if latency allows.
"""
from __future__ import annotations

import logging

from faster_whisper import WhisperModel

log = logging.getLogger("connector.stt")


class STT:
    def __init__(
        self,
        model: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "",
    ) -> None:
        log.info("loading faster-whisper model=%s device=%s compute=%s language=%s",
                 model, device, compute_type, language or "(auto)")
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        # Forcing the language (e.g. 'bg') is far more reliable than auto-detect,
        # especially for non-English on short or synthetic audio. Empty = auto.
        self.language = language or None

    def transcribe(self, wav_path: str) -> str:
        # vad_filter drops leading/trailing silence, which helps a lot with a
        # room mic. faster-whisper accepts a file path directly.
        segments, _info = self.model.transcribe(wav_path, vad_filter=True, language=self.language)
        text = "".join(seg.text for seg in segments).strip()
        log.info("transcript: %r", text)
        return text
