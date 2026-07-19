# reachy_app/runtime_config.py
"""Mutable, persistable runtime config for the dashboard-managed app.

Phase 1's config (`config.py::settings`) is a frozen, import-time snapshot of the
environment — right for the standalone `main.py`, but the packaged app needs a few
knobs the user can change *live* from the Settings tab without restarting the app
(which would kill the very GUI they are editing from).

`RuntimeConfig` seeds from `settings` (so `.env` / env vars still supply defaults),
then overlays a small JSON file of user edits (default
`~/.config/reachy-mini-claude/runtime.json`). Only the curated LIVE_FIELDS are
editable and persisted; connector url/token are read (from `settings`) for the
worker build but are not user-editable in this phase (Phase 3 owns the picker).

The supervisor reads `worker_params()` at (re)build time; a POST /config edit calls
`apply_updates()` (which persists) and then the app decides — via `config_actions()`
— whether to just set the log level, rebuild the worker, or flag an app restart.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from .config import settings

log = logging.getLogger("reachy.runtimeconfig")

RUNTIME_PATH = os.environ.get(
    "REACHY_APP_RUNTIME",
    os.path.expanduser("~/.config/reachy-mini-claude/runtime.json"),
)

# Curated fields the Settings UI edits and we persist. Everything else stays on
# `settings`. Order is display order.
LIVE_FIELDS = ("request_timeout_s", "max_utterance_s", "log_level", "reachy_media_backend")

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_MEDIA_BACKENDS = ("default", "local", "webrtc")


class RuntimeConfig:
    """Thread-safe, live-editable subset of the app's settings."""

    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path or RUNTIME_PATH
        # seed from the frozen env snapshot
        self.request_timeout_s = settings.request_timeout_s
        self.max_utterance_s = settings.max_utterance_s
        self.log_level = (settings.log_level or "INFO").upper()
        self.reachy_media_backend = settings.reachy_media_backend
        # needed by the worker build, not user-editable in Phase 2
        self.connector_url = settings.connector_url
        self.connector_token = settings.connector_token
        self._load()

    # -- validation / coercion (raises ValueError on bad input) --
    def _coerce(self, key: str, value) -> object:
        if key == "request_timeout_s":
            v = float(value)
            if not (1.0 <= v <= 600.0):
                raise ValueError("request_timeout_s must be between 1 and 600")
            return v
        if key == "max_utterance_s":
            v = float(value)
            if not (1.0 <= v <= 120.0):
                raise ValueError("max_utterance_s must be between 1 and 120")
            return v
        if key == "log_level":
            v = str(value).upper()
            if v not in _LOG_LEVELS:
                raise ValueError(f"log_level must be one of {_LOG_LEVELS}")
            return v
        if key == "reachy_media_backend":
            v = str(value).lower()
            if v not in _MEDIA_BACKENDS:
                raise ValueError(f"reachy_media_backend must be one of {_MEDIA_BACKENDS}")
            return v
        raise ValueError(f"unknown field {key!r}")

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for k in LIVE_FIELDS:
            if k in data:
                try:
                    setattr(self, k, self._coerce(k, data[k]))
                except ValueError as e:
                    log.warning("ignoring bad persisted %s: %s", k, e)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        payload = {k: getattr(self, k) for k in LIVE_FIELDS}
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self._path)  # atomic on the same filesystem

    def apply_updates(self, updates: dict) -> set[str]:
        """Validate + apply live-field updates atomically; persist on change.

        Returns the set of fields whose value actually changed. Raises ValueError
        (with NOTHING applied) if any field is unknown or invalid.
        """
        with self._lock:
            unknown = set(updates) - set(LIVE_FIELDS)
            if unknown:
                raise ValueError(f"unknown field(s): {sorted(unknown)}")
            # coerce everything first — if any raises, we apply none of it
            coerced = {k: self._coerce(k, v) for k, v in updates.items()}
            changed = {k for k, v in coerced.items() if getattr(self, k) != v}
            for k, v in coerced.items():
                setattr(self, k, v)
            if changed:
                self._save()
            return changed

    def public_dict(self) -> dict:
        with self._lock:
            return {k: getattr(self, k) for k in LIVE_FIELDS}

    def worker_params(self) -> dict:
        with self._lock:
            return dict(
                connector_url=self.connector_url,
                connector_token=self.connector_token,
                request_timeout_s=self.request_timeout_s,
                max_utterance_s=self.max_utterance_s,
            )


def config_actions(changed: set[str]) -> dict:
    """Given the set of changed live fields, what must the app do?"""
    return {
        "set_log_level": "log_level" in changed,
        "rebuild": bool(changed & {"request_timeout_s", "max_utterance_s"}),
        "restart_required": "reachy_media_backend" in changed,
    }


def restart_current_app(post=None, logger=log) -> bool:
    """Ask the Reachy daemon to restart THIS app (needed for a media-backend change).

    VERIFY-ON-HARDWARE: confirm the daemon base URL + path on the robot.
    Overridable via REACHY_DAEMON_URL. `post` is injectable for tests.
    """
    if post is None:
        import requests
        post = requests.post
    base = os.environ.get("REACHY_DAEMON_URL", "http://localhost:8000").rstrip("/")
    url = f"{base}/api/apps/restart-current-app"
    try:
        post(url, timeout=5).raise_for_status()
        return True
    except Exception as e:  # daemon down / wrong path -> report, don't crash the app
        logger.warning("restart-current-app failed (%s): %s", url, e)
        return False
