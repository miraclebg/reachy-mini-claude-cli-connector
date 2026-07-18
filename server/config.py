# server/config.py
"""Environment-driven configuration for the connector server.

All values can be overridden via a .env file (see .env.example) or real
environment variables. Nothing here is secret — it's just wiring.
"""
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # dotenv is optional; real env vars still work without it.
    pass


def _default_workspace() -> str:
    # The fixed directory Claude runs in. It scopes the session lookup (--resume
    # is per-directory) AND scopes Claude's filesystem blast radius.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "claude-workspace")


@dataclass(frozen=True)
class Settings:
    # --- server ---
    host: str = os.environ.get("HOST", "0.0.0.0")
    port: int = int(os.environ.get("PORT", "8080"))

    # --- Claude CLI ---
    claude_bin: str = os.environ.get("CLAUDE_BIN", "claude")
    claude_working_dir: str = os.environ.get("CLAUDE_WORKING_DIR", _default_workspace())
    # Leave empty to use whatever the CLI is already configured with.
    # Model alias ('opus' | 'sonnet' | 'haiku' | 'fable') or a full id.
    claude_model: str = os.environ.get("CLAUDE_MODEL", "")
    # Reasoning effort: low | medium | high | xhigh | max. Empty = CLI default.
    # Lower = snappier replies (better for a live voice loop); higher = more thinking.
    claude_effort: str = os.environ.get("CLAUDE_EFFORT", "")
    claude_timeout_s: int = int(os.environ.get("CLAUDE_TIMEOUT_S", "120"))
    # Permission mode (claude --permission-mode). Non-interactive options:
    #   auto     -> auto-approve tool calls (command execution + edits ON), deny list
    #               still blocks. Convenient; broad blast radius. THE DEFAULT.
    #   dontAsk  -> deny anything not in allowed_tools (the old read-only posture).
    #   plan     -> planning only, no side effects.
    # There's no human to approve prompts in a voice loop, so avoid interactive modes.
    permission_mode: str = os.environ.get("CLAUDE_PERMISSION_MODE", "auto")
    # Explicitly-allowed tools. Under `auto` this is largely moot (auto approves
    # beyond it); it matters under `dontAsk`. Web tools let Reachy answer live
    # questions (weather, news).
    allowed_tools: str = os.environ.get("CLAUDE_ALLOWED_TOOLS", "Read,Glob,Grep,WebSearch,WebFetch")
    # Belt-and-suspenders. Deny rules always win, even if the mode is loosened.
    disallowed_tools: str = os.environ.get(
        "CLAUDE_DISALLOWED_TOOLS",
        "Bash(rm *),Bash(sudo *),Bash(curl *),Bash(wget *),Bash(git push *)",
    )
    max_turns: int = int(os.environ.get("CLAUDE_MAX_TURNS", "6"))

    # --- STT (faster-whisper, local) ---
    whisper_model: str = os.environ.get("WHISPER_MODEL", "base.en")
    whisper_device: str = os.environ.get("WHISPER_DEVICE", "cpu")      # cpu | cuda | auto
    whisper_compute: str = os.environ.get("WHISPER_COMPUTE", "int8")   # int8 is fast on CPU
    # Force the spoken language (ISO code, e.g. 'bg'). Empty = auto-detect (less
    # reliable). Must use a multilingual model (not the *.en ones) for non-English.
    whisper_language: str = os.environ.get("WHISPER_LANGUAGE", "")

    # --- TTS (Piper, local) ---
    piper_model: str = os.environ.get("PIPER_MODEL", "")  # REQUIRED: path to a .onnx voice


settings = Settings()
