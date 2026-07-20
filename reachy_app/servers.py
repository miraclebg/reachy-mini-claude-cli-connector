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

import ipaddress
import json
import logging
import os
import threading
import time
from urllib.parse import urlsplit

from .discovery import verify_server

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
            os.makedirs(os.path.dirname(self._path) or ".", mode=0o700, exist_ok=True)
            tmp = self._path + ".tmp"
            # This file holds per-server TOKENS. Create it 0600 *at open time* rather
            # than chmod-ing after writing — a chmod-after-write leaves a window where
            # the secrets are world-readable. Keeping tokens out of HTTP responses
            # (see `public_server`) would be pointless if any local user could just
            # read them off disk.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self._path)  # preserves the 0600 mode
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


def servers_view(store: ServerStore, listener) -> dict:
    """Body of `GET /servers`: who is out there, who we know, who is bound.

    Never includes a token — see `public_server`.
    """
    saved = store.list_saved()
    saved_by_id = {s["id"]: s for s in saved}
    discovered = []
    for d in (listener.discovered() if listener is not None else []):
        e = dict(d)
        known_entry = saved_by_id.get(e.get("id"))
        e["saved"] = known_entry is not None
        # The UI prompts for a token when has_token is false; without this a freshly
        # discovered server looked "already credentialed" and the first tap 401'd.
        e["has_token"] = bool(known_entry and known_entry.get("token"))
        discovered.append(e)
    return {
        "discovered": discovered,
        "saved": [public_server(s) for s in saved],
        "selected_id": store.selected_id,
    }


def _bind(store: ServerStore, supervisor, server_id: str, name: str, url: str, token: str) -> dict:
    """Save + select + rebuild the worker thread against the newly bound server."""
    store.upsert(server_id, name, url, token)
    store.select(server_id)
    supervisor.rebuild()
    return public_server(store.get(server_id))


def select_server(store: ServerStore, supervisor, payload: dict,
                  listener=None, verify=verify_server) -> tuple[int, dict]:
    """Body of `POST /servers/select` — `{id?|url?, token?}`.

    Resolves the target (a saved id, a discovered id, or a raw url), proves it with
    the token via `/whoami`, then binds it. We never bind on a beacon alone.
    """
    payload = payload or {}
    server_id = (payload.get("id") or "").strip()
    url = (payload.get("url") or "").strip()
    token = payload.get("token") or ""
    name = (payload.get("name") or "").strip()

    if not server_id and not url:
        return 400, {"ok": False, "error": "provide an id or a url"}

    if server_id:
        saved = store.get(server_id)
        if saved is None and listener is not None:
            saved = next((d for d in listener.discovered() if d.get("id") == server_id), None)
        if saved is None:
            return 404, {"ok": False, "error": f"unknown server {server_id!r}"}
        url = url or saved.get("url", "")
        name = name or saved.get("name") or server_id
        token = token or saved.get("token") or ""

    ok, info = verify(url, token)
    if not ok:
        code = 401 if info == "unauthorized" else 502
        return code, {"ok": False, "error": info}

    # Trust /whoami's id over the beacon's claim — that is what defeats a spoofed beacon.
    real_id = info.get("id") or server_id or url
    real_name = name or info.get("name") or real_id
    return 200, {"ok": True, "server": _bind(store, supervisor, real_id, real_name, url, token)}


def add_server(store: ServerStore, supervisor, payload: dict,
               verify=verify_server) -> tuple[int, dict]:
    """Body of `POST /servers/add` — manual add-by-address for beacon-blocked LANs."""
    payload = payload or {}
    url = (payload.get("url") or "").strip()
    token = payload.get("token") or ""
    if not url:
        return 400, {"ok": False, "error": "provide a url"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    ok, info = verify(url, token)
    if not ok:
        code = 401 if info == "unauthorized" else 502
        return code, {"ok": False, "error": info}
    real_id = info.get("id") or url
    real_name = (payload.get("name") or "").strip() or info.get("name") or real_id
    return 200, {"ok": True, "server": _bind(store, supervisor, real_id, real_name, url, token)}


def client_allowed(client_host: str | None, allow_spec: str, bound_url: str | None = None) -> bool:
    """May this client change which connector the robot is bound to?

    `:8042` is unauthenticated and binds 0.0.0.0, and a token cannot help here — the
    page is served from the same open port, so any token given to the browser is
    readable by any LAN client. So the mutating /servers routes are gated on source IP.

    Rules:
      * empty `allow_spec` -> open (today's behaviour; the app warns at startup)
      * loopback is always allowed (on-robot tooling; never lock ourselves out)
      * the currently-bound connector's host is always allowed — it is already the
        trusted brain, and it is where the desktop dashboard runs, so the picker keeps
        working in the embed without any configuration
      * otherwise the client must match an entry in `allow_spec` (comma-separated bare
        IPs and/or CIDRs, e.g. "10.10.9.15, 192.168.1.0/24")
    """
    if not (allow_spec or "").strip():
        return True
    if not client_host:
        return False  # cannot identify the caller while restricted -> deny
    try:
        client_ip = ipaddress.ip_address(client_host.strip("[]"))
    except ValueError:
        return False
    if client_ip.is_loopback:
        return True
    if bound_url:
        host = urlsplit(bound_url).hostname
        if host:
            try:
                if ipaddress.ip_address(host) == client_ip:
                    return True
            except ValueError:
                pass  # bound via a hostname, not an IP -> fall through to the allowlist
    for entry in allow_spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if client_ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            log.warning("ignoring bad SETTINGS_ALLOW entry %r", entry)
    return False


def server_host_allowed(entry: dict | None, allow_spec: str) -> bool:
    """May we (re)bind to this saved server under the CURRENT policy?

    Deliberately passes `bound_url=None`: a saved server must never vouch for ITSELF.
    Otherwise a host that bound the robot while the allowlist was open would stay
    trusted forever — the launch rebind would re-establish it, and the bound-host rule
    in `client_allowed` would then keep approving that same host even after the
    operator restricted `SETTINGS_ALLOW`. Trust has to be re-derived from the current
    policy on every launch, not inherited from whatever was true at bind time.
    """
    if entry is None:
        return False
    return client_allowed(urlsplit(entry.get("url") or "").hostname, allow_spec, bound_url=None)
