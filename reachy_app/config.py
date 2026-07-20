# reachy_app/config.py
"""Environment-driven configuration for the robot-side app.

Override any value via a .env file (see .env.example) or real environment
variables. Nothing here is secret — it's just wiring.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# User config path for the INSTALLED app (its package-sibling .env in site-packages
# isn't user-editable). Overridable via REACHY_APP_CONFIG.
USER_CONFIG = os.environ.get(
    "REACHY_APP_CONFIG",
    os.path.expanduser("~/.config/reachy-mini-claude/config.env"),
)

try:
    from dotenv import load_dotenv
    # 1) THIS package's .env (dev / standalone, launched from the repo root).
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    # 2) The user config file (for the packaged app on the robot); wins over #1.
    if os.path.exists(USER_CONFIG):
        load_dotenv(USER_CONFIG, override=True)
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
    # Must match the connector's CONNECTOR_TOKEN (empty = the connector has auth off).
    connector_token: str = os.environ.get("CONNECTOR_TOKEN", "")

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
    # Protects the phone page + /status + /history + press/release. Empty = auth OFF.
    # The phone opens the page as .../?token=<BUTTON_TOKEN>; the page reuses it.
    button_token: str = os.environ.get("BUTTON_TOKEN", "")

    # Comma-separated IPs/CIDRs allowed to CHANGE which connector the robot is bound to
    # (POST /servers/select|add|rescan on :8042). Empty = open (a warning is logged).
    # Loopback and the currently-bound connector's host are always allowed.
    settings_allow: str = os.environ.get("SETTINGS_ALLOW", "")

    # --- wake word ("Hey Reachy" via Porcupine) ---
    # Inactive unless BOTH an access key and a keyword .ppn are provided.
    wakeword_enabled: bool = _as_bool(os.environ.get("WAKEWORD_ENABLED", "true"))
    picovoice_access_key: str = os.environ.get("PICOVOICE_ACCESS_KEY", "")
    porcupine_keyword_path: str = os.environ.get("PORCUPINE_KEYWORD_PATH", "")
    porcupine_sensitivity: float = float(os.environ.get("PORCUPINE_SENSITIVITY", "0.5"))

    log_level: str = os.environ.get("LOG_LEVEL", "INFO")


settings = Settings()
