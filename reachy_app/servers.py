# reachy_app/servers.py
"""The robot's saved connector Macs ("brains") and which one is bound.

Discovery tells us who is *out there* right now; this store remembers who we
*know* — friendly name, URL, and the per-server token the operator typed — plus
which one was last selected, so the next launch can rebind it silently.

Keyed by the connector's stable `id` (from its beacon / `/whoami`), NOT by URL, so
a Mac that changes IP is still recognised as the same brain.

Tokens live here on the robot's disk and must never travel back over HTTP — the
settings API is unauthenticated same-origin on the robot, so `public_server()` is
the only shape that may leave the process.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("reachy.servers")

SERVERS_PATH = os.environ.get(
    "REACHY_APP_SERVERS",
    os.path.expanduser("~/.config/reachy-mini-claude/servers.json"),
)


def public_server(entry: dict | None) -> dict | None:
    """LAN-safe projection of a saved server: never includes the token itself."""
    if entry is None:
        return None
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "url": entry.get("url"),
        "has_token": bool(entry.get("token")),
        "last_used_at": entry.get("last_used_at"),
    }


class ServerStore:
    """Saved servers + the current selection, persisted as JSON."""

    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path or SERVERS_PATH
        self._servers: list[dict] = []
        self.selected_id: str | None = None
        self._load()

    # -- persistence --
    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("could not read servers store %s: %s", self._path, e)
            return
        if not isinstance(data, dict):
            log.warning("ignoring servers store at %s: not a JSON object", self._path)
            return
        raw = data.get("servers")
        if isinstance(raw, list):
            self._servers = [s for s in raw if isinstance(s, dict) and s.get("id") and s.get("url")]
        sel = data.get("last_selected_id")
        self.selected_id = sel if isinstance(sel, str) else None

    def _save_locked(self) -> None:
        payload = {"servers": self._servers, "last_selected_id": self.selected_id}
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("could not persist servers store %s: %s", self._path, e)

    # -- queries --
    def list_saved(self) -> list[dict]:
        with self._lock:
            return [dict(s) for s in self._servers]

    def get(self, server_id: str) -> dict | None:
        with self._lock:
            for s in self._servers:
                if s["id"] == server_id:
                    return dict(s)
        return None

    def selected(self) -> dict | None:
        return self.get(self.selected_id) if self.selected_id else None

    # -- mutations --
    def upsert(self, server_id: str, name: str, url: str, token: str) -> dict:
        """Add or update a server. An empty `token` keeps any existing one."""
        with self._lock:
            for s in self._servers:
                if s["id"] == server_id:
                    s["name"] = name or s.get("name") or server_id
                    s["url"] = url or s["url"]
                    if token:
                        s["token"] = token
                    self._save_locked()
                    return dict(s)
            entry = {"id": server_id, "name": name or server_id, "url": url,
                     "token": token, "last_used_at": None}
            self._servers.append(entry)
            self._save_locked()
            return dict(entry)

    def select(self, server_id: str) -> bool:
        with self._lock:
            if not any(s["id"] == server_id for s in self._servers):
                return False
            self.selected_id = server_id
            self._save_locked()
        self.touch(server_id)
        return True

    def touch(self, server_id: str) -> None:
        with self._lock:
            for s in self._servers:
                if s["id"] == server_id:
                    s["last_used_at"] = time.time()
                    self._save_locked()
                    return

    def forget(self, server_id: str) -> bool:
        with self._lock:
            before = len(self._servers)
            self._servers = [s for s in self._servers if s["id"] != server_id]
            if self.selected_id == server_id:
                self.selected_id = None
            changed = len(self._servers) != before
            if changed:
                self._save_locked()
            return changed
