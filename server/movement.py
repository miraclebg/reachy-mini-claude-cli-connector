# server/movement.py
"""Movement helpers for the connector.

Parse the movement markers Claude emits and forward the resolved spec to the robot's
/move endpoint. Kept separate from main.py so the parser is unit-testable without
FastAPI or a robot (mirrors vision.py).

Markers:
  * named routine   -> [MOVE look_left]
  * improvised move -> [MOVE][{"yaw":20,"dur":0.3}, ...][/MOVE]
A co-occurring [LOOK] is left untouched (main.py handles vision).
"""
from __future__ import annotations

import json
import logging
import re

import requests

log = logging.getLogger("connector.movement")

# Keyframe block first (its inner JSON contains ']'), then the bareword named form.
_MOVE_BLOCK_RE = re.compile(r"\[move\](.*?)\[/move\]", re.IGNORECASE | re.DOTALL)
_MOVE_NAME_RE = re.compile(r"\[move\s+([a-z_]+)\]", re.IGNORECASE)


def parse_move(text: str) -> tuple[object | None, str]:
    """Return (spec, cleaned_text). spec is a preset name, a keyframe list, or None."""
    t = text or ""
    block = _MOVE_BLOCK_RE.search(t)
    if block:
        cleaned = _MOVE_BLOCK_RE.sub("", t).strip()
        raw = block.group(1).strip()
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("bad [MOVE] keyframe json: %r", raw)
            return None, cleaned
        return (spec if isinstance(spec, list) else None), cleaned
    named = _MOVE_NAME_RE.search(t)
    if named:
        cleaned = _MOVE_NAME_RE.sub("", t).strip()
        return named.group(1).lower(), cleaned
    return None, t


_STRIP_RE = re.compile(r"\[move\].*?\[/move\]|\[/?move\b[^\]]*\]", re.IGNORECASE | re.DOTALL)


def strip_markers(text: str) -> str:
    """Remove any movement marker — block form (incl. its JSON payload) or named form —
    so it never reaches TTS. Independent of parse_move / movement_enabled. Leaves [LOOK]
    untouched (main.py strips that separately)."""
    return _STRIP_RE.sub("", text or "")


def wants_move(text: str) -> bool:
    t = text or ""
    return bool(_MOVE_BLOCK_RE.search(t) or _MOVE_NAME_RE.search(t))


def post_move(base_url: str, spec, timeout_s: float = 8.0) -> bool:
    """POST {base_url}/move with {"spec": spec}. True on HTTP 200, else False."""
    url = base_url.rstrip("/") + "/move"
    try:
        r = requests.post(url, json={"spec": spec}, timeout=timeout_s)
    except requests.RequestException as e:
        log.warning("move POST failed (%s): %s", url, e)
        return False
    if r.status_code != 200:
        log.warning("move POST %s -> %s", url, r.status_code)
        return False
    return True
