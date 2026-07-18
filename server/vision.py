# server/vision.py
"""Vision helpers for the connector.

Two tiny, pure-ish functions: decide whether an utterance is asking to *see*, and
pull a single JPEG frame from the robot's /frame endpoint. Kept separate from main.py
so they're unit-testable without FastAPI or a robot.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("connector.vision")


def transcript_wants_vision(text: str, triggers: list[str]) -> bool:
    """True if any trigger substring appears in the transcript (case-insensitive)."""
    low = (text or "").lower()
    return any(t and t.lower() in low for t in triggers)


def fetch_frame(base_url: str, timeout_s: float = 4.0) -> bytes | None:
    """GET {base_url}/frame -> JPEG bytes, or None on any failure."""
    url = base_url.rstrip("/") + "/frame"
    try:
        r = requests.get(url, timeout=timeout_s)
    except requests.RequestException as e:
        log.warning("frame fetch failed (%s): %s", url, e)
        return None
    if r.status_code != 200 or not r.content:
        log.warning("frame fetch %s -> %s (%d bytes)", url, r.status_code, len(r.content or b""))
        return None
    return r.content
