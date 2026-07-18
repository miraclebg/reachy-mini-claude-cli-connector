# reachy_app/connector_client.py
"""Client for the Mac connector server (server/main.py).

One method does the whole turn: POST captured WAV to /chat, get spoken-reply WAV
back. The transcript and reply text ride along in response headers (URL-encoded),
handy for logging on the robot. Pure `requests` — no robot dependency, so this is
fully testable on the Mac against a running server.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import unquote

import requests

log = logging.getLogger("reachy.connector")


class ConnectorError(RuntimeError):
    pass


@dataclass
class ChatReply:
    audio_wav: bytes      # spoken reply, WAV bytes to play
    transcript: str       # what the server heard (debug)
    reply_text: str       # what Claude said (debug)


class ConnectorClient:
    def __init__(self, base_url: str, timeout_s: float = 180.0, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        # Sent on every request; the connector requires it unless its auth is off.
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def health(self) -> dict:
        r = requests.get(f"{self.base_url}/health", headers=self._headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def reset(self) -> None:
        """Forget the conversation on the server (new session next turn)."""
        try:
            requests.post(f"{self.base_url}/reset", headers=self._headers, timeout=10).raise_for_status()
        except requests.RequestException as e:
            raise ConnectorError(f"reset failed: {e}") from e

    def chat(self, wav_bytes: bytes) -> ChatReply:
        """Send one utterance (WAV) and get the spoken reply (WAV)."""
        try:
            r = requests.post(
                f"{self.base_url}/chat",
                files={"audio": ("utterance.wav", wav_bytes, "audio/wav")},
                headers=self._headers,
                timeout=self.timeout_s,
            )
        except requests.RequestException as e:
            raise ConnectorError(f"POST /chat failed: {e}") from e

        if r.status_code != 200:
            raise ConnectorError(f"server returned {r.status_code}: {r.text[:200]}")

        transcript = unquote(r.headers.get("X-Transcript", ""))
        reply_text = unquote(r.headers.get("X-Reply", ""))
        log.info("heard=%r reply=%r", transcript, reply_text)
        return ChatReply(audio_wav=r.content, transcript=transcript, reply_text=reply_text)
