# server/beacon.py
"""UDP presence beacon so robots on the LAN can find this connector.

Every ~10 s we broadcast a tiny JSON datagram announcing who we are and where to
reach us. It deliberately carries **no secret**: the robot still has to prove the
token against the token-gated `GET /whoami` before it will talk to us, which is
also what stops a spoofed beacon from impersonating a known brain.

Wire format (duplicated in `reachy_app/discovery.py` — the two live in different
venvs and cannot import each other, so both sides' tests pin this literal shape):

    {"reachy_connector": 1, "id": "<uuid hex>", "name": "<friendly>", "url": "http://<ip>:<port>"}

`id` is generated once and persisted, so a Mac keeps a stable identity across
restarts and IP changes — that is what the robot's saved-servers store keys on.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import uuid

log = logging.getLogger("connector.beacon")

BROADCAST_ADDR = "255.255.255.255"


def server_id(path: str) -> str:
    """Read this connector's stable id, creating it on first use."""
    try:
        with open(path, encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    new = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, path)
    except OSError as e:  # non-fatal: we just won't be stable across restarts
        log.warning("could not persist server id at %s: %s", path, e)
    return new


def default_server_name() -> str:
    """Friendly name for this Mac: SERVER_NAME, else the hostname."""
    return os.environ.get("SERVER_NAME", "").strip() or socket.gethostname()


def local_ip() -> str:
    """The IP a LAN peer would reach us on (the outbound-route address).

    Uses a connect() on a UDP socket — no packets are actually sent; it just asks
    the routing table which local address would be used.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def beacon_payload(server_id: str, name: str, url: str) -> bytes:
    """The exact datagram we broadcast. NO SECRETS HERE — see module docstring."""
    return json.dumps({
        "reachy_connector": 1,
        "id": server_id,
        "name": name,
        "url": url,
    }).encode("utf-8")


class Beacon:
    """Broadcasts `payload_fn()` on `port` every `interval_s` from a daemon thread."""

    def __init__(self, payload_fn, port: int, interval_s: float = 10.0) -> None:
        self._payload_fn = payload_fn
        self._port = port
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while not self._stop.is_set():
                try:
                    sock.sendto(self._payload_fn(), (BROADCAST_ADDR, self._port))
                except OSError as e:
                    # e.g. no network / broadcast blocked — keep trying, don't die
                    log.debug("beacon send failed: %s", e)
                self._stop.wait(self._interval_s)
        finally:
            sock.close()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="connector-beacon", daemon=True)
        self._thread.start()
        log.info("discovery beacon broadcasting on udp/%d every %.0fs", self._port, self._interval_s)

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)

    def is_alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()


WHOAMI_VERSION = "1"


def whoami_payload(server_id: str, name: str) -> dict:
    """Body of the token-gated `GET /whoami`.

    Returning `id` is the point: the robot compares it with the `id` the beacon
    claimed, so a rogue beacon advertising someone else's identity fails the check
    even if it somehow reached the robot.
    """
    return {"id": server_id, "name": name, "version": WHOAMI_VERSION}
