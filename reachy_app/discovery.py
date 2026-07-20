# reachy_app/discovery.py
"""Find connector Macs on the LAN, and prove one is really ours.

Two halves:

  * `BeaconListener` passively receives the Mac's UDP beacons and keeps a live
    picture of who is out there — id, friendly name, URL, last-seen. Entries older
    than the TTL vanish, so a Mac that goes away stops being offered.
  * `verify_server()` calls the Mac's token-gated `GET /whoami`. This is the step
    that actually matters for trust: the beacon is unauthenticated and anyone on
    the LAN can forge one, so we never bind a server on a beacon alone. A 200 with
    the right token proves reachability *and* the token; comparing the returned
    `id` with the beacon's claimed `id` defeats an impersonating beacon.

Wire format is duplicated from `server/beacon.py` (different venvs, cannot import
each other) — both sides' tests pin the same literal shape.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time

log = logging.getLogger("reachy.discovery")

DISCOVERY_PORT = 48569
DEFAULT_TTL_S = 30.0  # ~3 missed beacons at the default 10s interval


def parse_beacon(data: bytes) -> dict | None:
    """Validate one datagram. Returns {id, name, url} or None if it isn't ours."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("reachy_connector") != 1:
        return None
    sid, name, url = obj.get("id"), obj.get("name"), obj.get("url")
    if not (isinstance(sid, str) and isinstance(url, str) and sid and url):
        return None
    return {"id": sid, "name": name if isinstance(name, str) and name else sid, "url": url}


class BeaconListener:
    """Receives beacons on `port`, keeping entries fresh for `ttl_s`."""

    def __init__(self, port: int = DISCOVERY_PORT, ttl_s: float = DEFAULT_TTL_S) -> None:
        self._port = port
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._seen: dict[str, dict] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", self._port))
        except OSError as e:  # port busy — discovery degrades, app still runs
            log.warning("cannot bind discovery port %d: %s", self._port, e)
            sock.close()
            return
        sock.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    data, _addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                entry = parse_beacon(data)
                if entry is None:
                    continue
                entry["seen_at"] = time.time()
                with self._lock:
                    self._seen[entry["id"]] = entry
        finally:
            sock.close()

    def discovered(self) -> list[dict]:
        """Fresh entries only (within the TTL), most recently seen first."""
        cutoff = time.time() - self._ttl_s
        with self._lock:
            fresh = [dict(e) for e in self._seen.values() if e["seen_at"] >= cutoff]
        return sorted(fresh, key=lambda e: e["seen_at"], reverse=True)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()

    def start(self) -> None:
        # Guard on liveness, not merely "a thread object exists": if a previous
        # start() died early (e.g. the discovery port was briefly busy), we must be
        # able to retry rather than silently no-op forever.
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="reachy-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)

    def is_alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()


def verify_server(url: str, token: str, get=None, timeout_s: float = 5.0):
    """Prove a server is reachable AND that `token` is right, via GET /whoami.

    Returns (True, whoami_dict) or (False, reason). `get` is injectable for tests.
    """
    if get is None:
        import requests
        get = requests.get
    probe = f"{url.rstrip('/')}/whoami"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = get(probe, headers=headers, timeout=timeout_s)
    except Exception as e:
        return False, str(e)
    if getattr(r, "status_code", None) == 401:
        return False, "unauthorized"
    if getattr(r, "status_code", None) != 200:
        return False, f"http {getattr(r, 'status_code', '?')}"
    try:
        body = r.json()
    except Exception as e:
        return False, f"bad /whoami body: {e}"
    if not isinstance(body, dict):
        return False, "bad /whoami body: not a JSON object"
    return True, body
