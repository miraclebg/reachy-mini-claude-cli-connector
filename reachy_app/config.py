# reachy_app/config.py
"""Environment-driven configuration for the robot-side app.

Override any value via a .env file (see .env.example) or real environment
variables. Nothing here is secret — it's just wiring.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # dotenv is optional; real env vars still work without it.
    pass


def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # --- where the Mac connector server lives (POST /chat) ---
    # On the Pi, set this to the Mac's LAN IP, e.g. http://192.168.1.20:8080
    connector_url: str = os.environ.get("CONNECTOR_URL", "http://localhost:8080")
    request_timeout_s: float = float(os.environ.get("REQUEST_TIMEOUT_S", "180"))

    # --- which audio/motion backend ---
    #   local   -> Mac mic + speakers (sounddevice), no robot. For testing.
    #   reachy  -> the real Reachy Mini SDK (robot required).
    backend: str = os.environ.get("REACHY_BACKEND", "local")

    # media backend passed to ReachyMini(...) — "default" | "local" | "webrtc".
    # (Only used when backend == "reachy".)
    reachy_media_backend: str = os.environ.get("REACHY_MEDIA_BACKEND", "default")

    # --- audio capture ---
    sample_rate: int = int(os.environ.get("SAMPLE_RATE", "16000"))  # good for whisper
    frame_ms: int = int(os.environ.get("FRAME_MS", "30"))           # capture block size
    max_utterance_s: float = float(os.environ.get("MAX_UTTERANCE_S", "15"))

    # --- end-of-speech (RMS silence endpointer; used on the wake-word path) ---
    vad_silence_ms: int = int(os.environ.get("VAD_SILENCE_MS", "800"))
    vad_rms_threshold: float = float(os.environ.get("VAD_RMS_THRESHOLD", "0.015"))
    vad_min_speech_ms: int = int(os.environ.get("VAD_MIN_SPEECH_MS", "300"))

    # --- phone hold-to-talk page (served on the LAN) ---
    button_enabled: bool = _as_bool(os.environ.get("BUTTON_ENABLED", "true"))
    button_host: str = os.environ.get("BUTTON_HOST", "0.0.0.0")
    button_port: int = int(os.environ.get("BUTTON_PORT", "8081"))

    # --- wake word ("Hey Reachy" via Porcupine) ---
    # Inactive unless BOTH an access key and a keyword .ppn are provided.
    wakeword_enabled: bool = _as_bool(os.environ.get("WAKEWORD_ENABLED", "true"))
    picovoice_access_key: str = os.environ.get("PICOVOICE_ACCESS_KEY", "")
    porcupine_keyword_path: str = os.environ.get("PORCUPINE_KEYWORD_PATH", "")
    porcupine_sensitivity: float = float(os.environ.get("PORCUPINE_SENSITIVITY", "0.5"))

    log_level: str = os.environ.get("LOG_LEVEL", "INFO")


settings = Settings()
