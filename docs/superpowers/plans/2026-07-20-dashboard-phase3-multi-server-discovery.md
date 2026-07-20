# Dashboard Phase 3 — Multi-Server LAN Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The robot finds connector Macs on the Wi-Fi by itself, so `CONNECTOR_URL` stops needing a hand edit every time the network changes — with **several** Macs discovered, named, saved, and chosen between from the Settings tab.

**Architecture:** The Mac broadcasts a small UDP beacon (`{reachy_connector, id, name, url}` — **no secret**) every ~10 s. The robot passively listens and keeps a live discovered list, merged by stable `id` with a saved-servers store on disk. Selecting a server calls the Mac's new **token-gated `GET /whoami`**, which proves both reachability *and* the token (and defeats a spoofed beacon), then binds it and rebuilds the Phase-2 worker. If the last-used server verifies at launch it binds silently; otherwise the app **parks** (no worker) and the Talk tab shows a "pick a brain" gate. **Never auto-switch.**

**Tech Stack:** Python 3.10+ (Mac venv `server/.venv`, robot venv `reachy_app/.venv` 3.12), stdlib `socket`/`json`/`threading`, `requests` (already a dep), vanilla HTML/CSS/JS. **Two different test conventions — do not mix them up:** `server/` uses **pytest** (`python -m pytest -q <file>`), `reachy_app/` uses the **custom runner** `reachy_app/tests/test_smoke.py` (`python -m reachy_app.tests.test_smoke`, NOT pytest).

## Global Constraints

- **The beacon carries NO secret.** Payload is exactly `{"reachy_connector": 1, "id": "<uuid>", "name": "<SERVER_NAME>", "url": "http://<ip>:<port>"}`. The token is entered in the robot UI and stored only on the robot.
- **`/health` stays open; `/whoami` is token-gated.** `server/main.py`'s existing `require_token` middleware protects everything not in `_OPEN_PATHS = {"/health"}` — so simply adding `/whoami` gets protection. **Do not add `/whoami` to `_OPEN_PATHS`.**
- **Wire format is a contract across two venvs.** The Mac's beacon and the robot's listener cannot import each other. The payload keys/values above are duplicated deliberately in both, and both sides' tests assert the same literal shape.
- **`DISCOVERY_PORT` default `48569`**, beacon interval default `10.0` s, discovered-entry TTL `30.0` s (3 missed beacons).
- **Saved-servers store:** `~/.config/reachy-mini-claude/servers.json`, shape `{"servers": [{"id","name","url","token","last_used_at"}], "last_selected_id": <id|null>}`. Atomic write via `os.replace` (same pattern as Phase 2's `runtime.json`).
- **Never auto-switch.** Launch binds only `last_selected_id` and only if `/whoami` verifies; otherwise park.
- **Tokens are write-only over HTTP.** `GET /servers` must return `has_token: bool`, **never** the token value — `:8042` is unauthenticated same-origin on the robot, so returning tokens would leak every connector secret to the LAN.
- **Phase-2 structure is preserved:** only `ConversationLoop` + `ConnectorClient` are rebuilt; the backend and button/status/history are shared and built once. Route logic stays **framework-free** (in importable modules) so it is testable without `reachy_mini` — `app.py` imports `reachy_mini` and can never be imported by the test suite.
- **Do not break** the standalone `reachy_app/main.py` (frozen `settings` + `loop.run_forever`) or Phase 2's `/config` behaviour.
- **Commits:** lowercase-prefixed subject (`feat:` / `fix:`) + append the trailer `Claude-Session: https://claude.ai/code/session_01PQEE7LUw4A7KFPi2wMh2Dz`.

## File Structure

**Mac (`server/`)**
- **Create** `server/beacon.py` — server identity (persisted uuid), payload builder, UDP broadcaster thread. One responsibility: "advertise this connector on the LAN."
- **Create** `server/test_beacon.py` — pytest.
- **Modify** `server/config.py` — `server_name`, `discovery_beacon`, `discovery_port`, `discovery_interval_s`, `server_id_file`.
- **Modify** `server/main.py` — `GET /whoami`; start the beacon at import (daemon thread), gated on `discovery_beacon`.
- **Modify** `server/.env.example`, `.gitignore` (the `.server_id` state file).

**Robot (`reachy_app/`)**
- **Create** `reachy_app/discovery.py` — `parse_beacon`, `BeaconListener`, `verify_server`. One responsibility: "who is out there, and is this one really mine?"
- **Create** `reachy_app/servers.py` — `SavedServer` + `ServerStore` (persistence, select/upsert/forget) and the framework-free route helpers.
- **Modify** `reachy_app/supervisor.py` — `server_provider` + park/bind.
- **Modify** `reachy_app/app.py` — launch/park logic + `/servers*` routes.
- **Modify** `reachy_app/static/index.html` — Server connection picker (Settings) + the gate (Talk).
- **Modify** `reachy_app/tests/test_smoke.py` — custom-runner tests for each of the above.

---

### Task 1: Mac — beacon module (identity + payload + broadcaster)

**Files:**
- Create: `server/beacon.py`
- Create: `server/test_beacon.py`
- Modify: `server/config.py` (5 new settings)
- Modify: `server/.env.example`, `.gitignore`

**Interfaces:**
- Produces:
  - `server_id(path: str) -> str` — read-or-create a persisted uuid4 hex.
  - `beacon_payload(server_id: str, name: str, url: str) -> bytes` — the exact wire JSON.
  - `default_server_name() -> str` — `SERVER_NAME` or the hostname.
  - `local_ip() -> str` — the outbound-route IP (for the advertised URL).
  - `Beacon(payload_fn, port, interval_s)` with `.start()` / `.stop()` — daemon thread broadcasting `payload_fn()` every `interval_s`.

- [ ] **Step 1: Write the failing tests**

Create `server/test_beacon.py`:

```python
import json
import os
import socket
import tempfile
import time

from beacon import Beacon, beacon_payload, default_server_name, local_ip, server_id


def test_server_id_is_created_then_stable():
    d = tempfile.mkdtemp()
    p = os.path.join(d, ".server_id")
    a = server_id(p)
    assert a and len(a) >= 8
    assert os.path.exists(p)
    assert server_id(p) == a  # stable across calls


def test_beacon_payload_wire_shape():
    raw = beacon_payload("abc123", "studio-mac", "http://10.0.0.5:8080")
    obj = json.loads(raw.decode())
    # This literal shape is the cross-venv contract with reachy_app/discovery.py.
    assert obj == {
        "reachy_connector": 1,
        "id": "abc123",
        "name": "studio-mac",
        "url": "http://10.0.0.5:8080",
    }


def test_beacon_payload_carries_no_secret():
    raw = beacon_payload("abc123", "studio-mac", "http://10.0.0.5:8080").decode().lower()
    for forbidden in ("token", "secret", "password", "authorization"):
        assert forbidden not in raw


def test_default_server_name_and_local_ip():
    assert default_server_name()          # never empty
    ip = local_ip()
    assert ip.count(".") == 3             # dotted quad


def test_beacon_broadcasts_on_the_wire():
    # Bind a listener first, then run one beacon tick at it.
    port = 48999
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("", port))
    rx.settimeout(3.0)
    b = Beacon(lambda: beacon_payload("id1", "n1", "http://127.0.0.1:8080"),
               port=port, interval_s=0.2)
    b.start()
    try:
        data, _addr = rx.recvfrom(2048)
        obj = json.loads(data.decode())
        assert obj["reachy_connector"] == 1 and obj["id"] == "id1"
    finally:
        b.stop()
        rx.close()


def test_beacon_stop_is_clean():
    b = Beacon(lambda: beacon_payload("i", "n", "http://127.0.0.1:8080"),
               port=48998, interval_s=0.05)
    b.start()
    time.sleep(0.15)
    b.stop()
    assert not b.is_alive()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server && source .venv/bin/activate && python -m pytest -q test_beacon.py 2>&1 | tail -5`
Expected: FAIL — `ModuleNotFoundError: No module named 'beacon'`.

- [ ] **Step 3: Create `server/beacon.py`**

```python
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
        if self._thread is not None:
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
```

- [ ] **Step 4: Add the settings**

In `server/config.py`, add to the `Settings` dataclass, right after the `movement` block (before the closing of the class):

```python
    # --- LAN discovery (robots find this connector by UDP beacon) ---
    # The beacon advertises {id, name, url} only — never the token.
    discovery_beacon: bool = _as_bool(os.environ.get("DISCOVERY_BEACON", "true"))
    discovery_port: int = int(os.environ.get("DISCOVERY_PORT", "48569"))
    discovery_interval_s: float = float(os.environ.get("DISCOVERY_INTERVAL_S", "10"))
    # Friendly name shown in the robot's server picker. Empty = this Mac's hostname.
    server_name: str = os.environ.get("SERVER_NAME", "")
    # Where this connector's stable id is persisted (gitignored).
    server_id_file: str = os.environ.get(
        "SERVER_ID_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".server_id"),
    )
```

In `server/.env.example`, append:

```
# --- LAN discovery ---
# Robots on the Wi-Fi discover this connector via a UDP beacon carrying {id,name,url}
# (never the token). Turn off if you prefer configuring the robot by hand.
DISCOVERY_BEACON=true
DISCOVERY_PORT=48569
DISCOVERY_INTERVAL_S=10
# Friendly name shown in the robot's server picker (default: this Mac's hostname).
SERVER_NAME=
```

In the repo-root `.gitignore`, add:

```
server/.server_id
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd server && source .venv/bin/activate && python -m pytest -q test_beacon.py 2>&1 | tail -5`
Expected: `6 passed`.

Run the existing suite to confirm nothing regressed:
`python -m pytest -q test_movement.py test_vision.py 2>&1 | tail -3` → all pass.

- [ ] **Step 6: Commit**

```bash
git add server/beacon.py server/test_beacon.py server/config.py server/.env.example .gitignore
git commit -m "feat: UDP discovery beacon for the Mac connector"
```

---

### Task 2: Mac — `GET /whoami` + start the beacon

**Files:**
- Modify: `server/main.py` (import, `/whoami` route, beacon startup)
- Modify: `server/test_beacon.py` (add the identity-payload test)

**Interfaces:**
- Consumes: `beacon.server_id`, `beacon.default_server_name`, `beacon.local_ip`, `beacon.beacon_payload`, `beacon.Beacon`; `settings.discovery_*`, `settings.server_name`, `settings.server_id_file`.
- Produces: `whoami_payload(server_id, name) -> dict` (in `beacon.py`, framework-free so it is unit-testable without importing `main.py`); `GET /whoami` → that payload, **token-gated** by the existing middleware.

**Why the route itself has no automated test:** importing `server/main.py` constructs the whole pipeline (faster-whisper model load, Claude client, Piper) — far too heavy and side-effecting for the unit suite. So the *payload* is tested here, the *auth* is the already-existing middleware (unchanged), and the *end-to-end* `/whoami` 200/401 is verified by the robot's `verify_server` against the live server in Task 7's hardware pass.

- [ ] **Step 1: Write the failing test**

Add to `server/test_beacon.py`:

```python
def test_whoami_payload_shape_and_no_secret():
    from beacon import whoami_payload
    p = whoami_payload("abc123", "studio-mac")
    assert p["id"] == "abc123"
    assert p["name"] == "studio-mac"
    assert "version" in p
    # the robot cross-checks this id against the beacon's claimed id
    assert set(p) == {"id", "name", "version"}
    blob = json.dumps(p).lower()
    for forbidden in ("token", "secret", "password"):
        assert forbidden not in blob
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd server && python -m pytest -q test_beacon.py::test_whoami_payload_shape_and_no_secret 2>&1 | tail -4`
Expected: FAIL — `ImportError: cannot import name 'whoami_payload'`.

- [ ] **Step 3: Add `whoami_payload` to `server/beacon.py`**

Append at the end of `server/beacon.py`:

```python
WHOAMI_VERSION = "1"


def whoami_payload(server_id: str, name: str) -> dict:
    """Body of the token-gated `GET /whoami`.

    Returning `id` is the point: the robot compares it with the `id` the beacon
    claimed, so a rogue beacon advertising someone else's identity fails the check
    even if it somehow reached the robot.
    """
    return {"id": server_id, "name": name, "version": WHOAMI_VERSION}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `cd server && python -m pytest -q test_beacon.py 2>&1 | tail -3`
Expected: `7 passed`.

- [ ] **Step 5: Wire `/whoami` + the beacon into `server/main.py`**

Read `server/main.py` and confirm each block below before editing (match on content, not line numbers).

**(a)** Extend the imports — replace:

```python
from movement import parse_move, post_move, strip_markers
```

with:

```python
from movement import parse_move, post_move, strip_markers
from beacon import (
    Beacon, beacon_payload, default_server_name, local_ip, server_id, whoami_payload,
)
```

**(b)** After the auth-warning block (the `if settings.connector_token: ... else: log.warning(...)` that ends the "init the pipeline once" section, just before `@app.get("/health")`), add:

```python
# --- LAN discovery: advertise ourselves so robots can find us ---
SERVER_ID = server_id(settings.server_id_file)
SERVER_NAME = settings.server_name.strip() or default_server_name()

beacon: Beacon | None = None
if settings.discovery_beacon:
    beacon = Beacon(
        lambda: beacon_payload(SERVER_ID, SERVER_NAME, f"http://{local_ip()}:{settings.port}"),
        port=settings.discovery_port,
        interval_s=settings.discovery_interval_s,
    )
    beacon.start()
else:
    log.info("discovery beacon disabled (DISCOVERY_BEACON=false)")
```

(The URL is rebuilt on every tick via `local_ip()`, so the advertisement follows the Mac onto a new network without a restart.)

**(c)** Add the route immediately after the `health()` function:

```python
@app.get("/whoami")
def whoami():
    """Token-gated identity probe. The robot calls this to prove BOTH that we are
    reachable AND that its stored token is right, before binding us as its brain.
    Protected by the require_token middleware (it is not in _OPEN_PATHS)."""
    return whoami_payload(SERVER_ID, SERVER_NAME)
```

- [ ] **Step 6: Verify it compiles and the suite still passes**

Run: `cd server && python -m py_compile main.py && echo COMPILE_OK`
Expected: `COMPILE_OK`

Run: `python -m pytest -q test_beacon.py test_movement.py test_vision.py 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add server/main.py server/beacon.py server/test_beacon.py
git commit -m "feat: token-gated /whoami and start the discovery beacon"
```

---

### Task 3: Robot — discovery listener + server verification

**Files:**
- Create: `reachy_app/discovery.py`
- Modify: `reachy_app/tests/test_smoke.py` (custom runner — add tests, register in `main()`)

**Interfaces:**
- Produces:
  - `parse_beacon(data: bytes) -> dict | None` — validated `{id, name, url}` or `None`.
  - `BeaconListener(port=DISCOVERY_PORT, ttl_s=30.0)` — `.start()`, `.stop()`, `.discovered() -> list[dict]` (fresh only, newest first), `.clear()`.
  - `verify_server(url, token, get=None) -> tuple[bool, dict | str]` — `(True, whoami)` on 200, `(False, "unauthorized")` on 401, `(False, "<error>")` otherwise.
  - `DISCOVERY_PORT = 48569`, `DEFAULT_TTL_S = 30.0`.

- [ ] **Step 1: Write the failing tests**

Add to `reachy_app/tests/test_smoke.py`. Add the import near the other `reachy_app.*` imports:

```python
from reachy_app.discovery import BeaconListener, DISCOVERY_PORT, parse_beacon, verify_server
```

Add these tests (after the supervisor tests):

```python
def _send_beacon(port, obj) -> None:
    import json as _json, socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        s.sendto(_json.dumps(obj).encode(), ("127.0.0.1", port))
    finally:
        s.close()


def test_parse_beacon_accepts_and_rejects() -> None:
    print("discovery: beacon parsing accepts the contract, rejects junk")
    import json as _json
    good = _json.dumps({"reachy_connector": 1, "id": "i1", "name": "mac",
                        "url": "http://10.0.0.5:8080"}).encode()
    got = parse_beacon(good)
    check("accepts a valid beacon", got is not None and got["id"] == "i1", str(got))
    check("keeps name+url", got and got["name"] == "mac" and got["url"] == "http://10.0.0.5:8080", str(got))
    check("rejects non-JSON", parse_beacon(b"not json") is None)
    check("rejects wrong magic", parse_beacon(_json.dumps({"id": "x", "name": "n", "url": "u"}).encode()) is None)
    check("rejects missing url", parse_beacon(_json.dumps(
        {"reachy_connector": 1, "id": "x", "name": "n"}).encode()) is None)
    check("rejects non-dict", parse_beacon(_json.dumps([1, 2]).encode()) is None)


def test_beacon_listener_collects_and_dedupes() -> None:
    print("discovery: listener collects beacons, dedupes by id, honours clear()")
    port = 48997
    lis = BeaconListener(port=port)
    lis.start()
    try:
        time.sleep(0.3)
        _send_beacon(port, {"reachy_connector": 1, "id": "a", "name": "mac-a", "url": "http://1.1.1.1:8080"})
        _send_beacon(port, {"reachy_connector": 1, "id": "b", "name": "mac-b", "url": "http://2.2.2.2:8080"})
        _send_beacon(port, {"reachy_connector": 1, "id": "a", "name": "mac-a2", "url": "http://1.1.1.9:8080"})
        _send_beacon(port, b"garbage" and {"nope": 1})
        check("both servers discovered", _wait_until(lambda: len(lis.discovered()) == 2), str(lis.discovered()))
        by_id = {d["id"]: d for d in lis.discovered()}
        check("dedupes by id (latest wins)", by_id.get("a", {}).get("url") == "http://1.1.1.9:8080", str(by_id))
        check("junk ignored", set(by_id) == {"a", "b"}, str(by_id))
        lis.clear()
        check("clear() empties the list", lis.discovered() == [], str(lis.discovered()))
    finally:
        lis.stop()
    check("listener stops cleanly", not lis.is_alive())


def test_beacon_listener_expires_stale() -> None:
    print("discovery: entries older than the TTL disappear")
    port = 48996
    lis = BeaconListener(port=port, ttl_s=0.3)
    lis.start()
    try:
        time.sleep(0.3)
        _send_beacon(port, {"reachy_connector": 1, "id": "z", "name": "m", "url": "http://3.3.3.3:8080"})
        check("appears", _wait_until(lambda: len(lis.discovered()) == 1), str(lis.discovered()))
        check("expires after ttl", _wait_until(lambda: lis.discovered() == [], timeout=3.0), str(lis.discovered()))
    finally:
        lis.stop()


def test_verify_server_token_outcomes() -> None:
    print("discovery: /whoami verifies reachability AND the token")
    class _R:
        def __init__(self, code, payload=None): self.status_code = code; self._p = payload or {}
        def json(self): return self._p

    calls = []
    def ok_get(url, headers=None, timeout=0):
        calls.append((url, headers))
        return _R(200, {"id": "i1", "name": "mac", "version": "1"})
    ok, info = verify_server("http://1.1.1.1:8080", "tok", get=ok_get)
    check("200 -> verified", ok is True and info["id"] == "i1", str(info))
    check("hits /whoami", calls and calls[0][0].endswith("/whoami"), str(calls))
    check("sends bearer token", calls and "tok" in str(calls[0][1]), str(calls))

    ok2, err2 = verify_server("http://1.1.1.1:8080", "bad", get=lambda *a, **k: _R(401))
    check("401 -> unauthorized", ok2 is False and err2 == "unauthorized", str(err2))

    ok3, err3 = verify_server("http://1.1.1.1:8080", "t", get=lambda *a, **k: _R(500))
    check("500 -> not verified", ok3 is False, str(err3))

    def boom(*a, **k): raise OSError("no route to host")
    ok4, err4 = verify_server("http://1.1.1.1:8080", "t", get=boom)
    check("unreachable -> not verified", ok4 is False and "no route" in err4.lower(), str(err4))
```

Register the four in the `main()` runner tuple (keep every existing entry):

```python
        test_parse_beacon_accepts_and_rejects, test_beacon_listener_collects_and_dedupes,
        test_beacon_listener_expires_stale, test_verify_server_token_outcomes,
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source reachy_app/.venv/bin/activate && python -m reachy_app.tests.test_smoke 2>&1 | grep -Ei "discovery:|ModuleNotFound|ImportError" | head`
Expected: FAIL — `ModuleNotFoundError: No module named 'reachy_app.discovery'`.

- [ ] **Step 3: Create `reachy_app/discovery.py`**

```python
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
        if self._thread is not None:
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
        return True, r.json()
    except Exception as e:
        return False, f"bad /whoami body: {e}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A6 "discovery:"`
Expected: all `discovery:` checks pass; summary `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add reachy_app/discovery.py reachy_app/tests/test_smoke.py
git commit -m "feat: LAN beacon listener and token-verifying server probe"
```

---

### Task 4: Robot — saved-servers store

**Files:**
- Create: `reachy_app/servers.py`
- Modify: `reachy_app/tests/test_smoke.py`

**Interfaces:**
- Produces:
  - `SERVERS_PATH` (default `~/.config/reachy-mini-claude/servers.json`, overridable via `REACHY_APP_SERVERS`).
  - `ServerStore(path=None)`: `.list_saved() -> list[dict]`, `.get(id) -> dict | None`, `.upsert(id, name, url, token) -> dict`, `.select(id) -> bool`, `.selected() -> dict | None`, `.selected_id`, `.forget(id) -> bool`, `.touch(id)`.
  - `public_server(entry: dict) -> dict` — the LAN-safe projection `{id, name, url, has_token, last_used_at}` (**never** the token).

- [ ] **Step 1: Write the failing tests**

Add to `reachy_app/tests/test_smoke.py` (import near the others):

```python
from reachy_app.servers import ServerStore, public_server
```

```python
def _tmp_servers_path() -> str:
    import os, tempfile
    return os.path.join(tempfile.mkdtemp(prefix="reachy-srv-"), "servers.json")


def test_server_store_roundtrip_and_select() -> None:
    print("servers: store persists, selects, and reloads")
    p = _tmp_servers_path()
    s = ServerStore(path=p)
    check("starts empty", s.list_saved() == [] and s.selected() is None)
    s.upsert("id-a", "studio", "http://1.1.1.1:8080", "tok-a")
    s.upsert("id-b", "office", "http://2.2.2.2:8080", "tok-b")
    check("two saved", len(s.list_saved()) == 2, str(s.list_saved()))
    check("select unknown -> False", s.select("nope") is False)
    check("select known -> True", s.select("id-b") is True)
    check("selected is id-b", s.selected() and s.selected()["id"] == "id-b", str(s.selected()))
    s2 = ServerStore(path=p)  # reload from disk
    check("selection persisted", s2.selected_id == "id-b", str(s2.selected_id))
    check("token persisted (on disk, not over HTTP)", s2.get("id-b")["token"] == "tok-b")
    check("upsert updates in place", (s2.upsert("id-a", "studio2", "http://9.9.9.9:8080", "tok-a2"),
                                      len(s2.list_saved()))[1] == 2, str(s2.list_saved()))
    check("updated fields stuck", s2.get("id-a")["url"] == "http://9.9.9.9:8080", str(s2.get("id-a")))
    check("forget removes", s2.forget("id-a") is True and s2.get("id-a") is None)


def test_server_store_never_leaks_token() -> None:
    print("servers: the public projection hides the token")
    p = _tmp_servers_path()
    s = ServerStore(path=p)
    s.upsert("id-a", "studio", "http://1.1.1.1:8080", "sup3rs3cret")
    pub = public_server(s.get("id-a"))
    check("has_token flag instead of the token", pub.get("has_token") is True, str(pub))
    check("token value absent", "token" not in pub, str(pub))
    check("secret string nowhere in the projection", "sup3rs3cret" not in str(pub), str(pub))
    check("keeps id/name/url", pub["id"] == "id-a" and pub["name"] == "studio"
          and pub["url"] == "http://1.1.1.1:8080", str(pub))
    s.upsert("id-c", "no-token", "http://3.3.3.3:8080", "")
    check("empty token -> has_token False", public_server(s.get("id-c"))["has_token"] is False)


def test_server_store_survives_corrupt_file() -> None:
    print("servers: a corrupt or non-dict store degrades to empty, not a crash")
    p = _tmp_servers_path()
    import os
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    s = ServerStore(path=p)  # must not raise
    check("corrupt -> empty store", s.list_saved() == [] and s.selected() is None)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("[1,2,3]")
    s2 = ServerStore(path=p)  # non-dict JSON must not raise either
    check("non-dict -> empty store", s2.list_saved() == [])
```

Register all three in the `main()` tuple.

- [ ] **Step 2: Run to verify failure**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -Ei "servers:|ModuleNotFound" | head`
Expected: FAIL — `ModuleNotFoundError: No module named 'reachy_app.servers'`.

- [ ] **Step 3: Create `reachy_app/servers.py`**

```python
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
```

- [ ] **Step 4: Run to verify passing**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A8 "servers:"`
Expected: all `servers:` checks pass; `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add reachy_app/servers.py reachy_app/tests/test_smoke.py
git commit -m "feat: saved-servers store keyed by stable connector id"
```

---

### Task 5: Robot — supervisor park/bind

**Files:**
- Modify: `reachy_app/supervisor.py`
- Modify: `reachy_app/tests/test_smoke.py`

**Interfaces:**
- Changed: `Supervisor(..., server_provider: Callable[[], dict | None] | None = None)` — returns `{"url", "token"}` for the bound server, or `None` to **park**.
- Produces: `.is_parked() -> bool`. When parked, `start()`/`rebuild()` run **no worker thread** and publish status `"parked"`.
- Unchanged: everything from Phase 2 (`start`, `rebuild`, `stop`, `current_loop`, `_thread_alive`, crash backoff). With no `server_provider` (the default) behaviour is exactly as before, so Phase 2's tests keep passing.

- [ ] **Step 1: Write the failing tests**

Add to `reachy_app/tests/test_smoke.py` (after the existing supervisor tests):

```python
def test_supervisor_parks_without_a_server() -> None:
    print("supervisor: no bound server -> parked, no worker, status 'parked'")
    cfg = RuntimeConfig(path=_tmp_runtime_path())
    status = StatusState()
    bound = {"v": None}  # nothing bound yet
    fake = FakeBackend(pcm_to_wav(np.zeros(1600, dtype=np.float32), 16000))
    sup = Supervisor(backend=fake, config=cfg, button=ButtonState(),
                     status=status, history=History(),
                     client_factory=_RecordingClientFactory(),
                     server_provider=lambda: bound["v"])
    sup.start()
    try:
        check("parked", sup.is_parked() is True)
        check("no worker thread", not sup._thread_alive())
        check("status published as parked", _wait_until(lambda: status.get() == "parked"), status.get())
        check("no loop built", sup.current_loop is None, str(sup.current_loop))
        # bind a server and rebuild -> worker starts
        bound["v"] = {"url": "http://1.1.1.1:8080", "token": "t"}
        sup.rebuild()
        check("worker runs once bound", _wait_until(lambda: sup.current_loop is not None))
        check("not parked anymore", sup.is_parked() is False)
        # unbind -> parks again, worker torn down
        bound["v"] = None
        sup.rebuild()
        check("parks again on unbind", _wait_until(lambda: not sup._thread_alive()))
        check("is_parked true again", sup.is_parked() is True)
    finally:
        sup.stop()


def test_supervisor_binds_the_provided_server() -> None:
    print("supervisor: the bound server's url+token reach the rebuilt client")
    cfg = RuntimeConfig(path=_tmp_runtime_path())
    factory = _RecordingClientFactory()
    bound = {"v": {"url": "http://5.5.5.5:8080", "token": "tok-5"}}
    fake = FakeBackend(pcm_to_wav(np.zeros(1600, dtype=np.float32), 16000))
    sup = Supervisor(backend=fake, config=cfg, button=ButtonState(),
                     status=StatusState(), history=History(),
                     client_factory=factory, server_provider=lambda: bound["v"])
    sup.start()
    try:
        check("worker built", _wait_until(lambda: sup.current_loop is not None))
        check("client got the bound url", factory.calls[-1][0] == "http://5.5.5.5:8080", str(factory.calls[-1]))
        check("client got the bound token", factory.calls[-1][2] == "tok-5", str(factory.calls[-1]))
        bound["v"] = {"url": "http://6.6.6.6:8080", "token": "tok-6"}
        sup.rebuild()
        check("switch rebinds url", _wait_until(lambda: factory.calls[-1][0] == "http://6.6.6.6:8080"),
              str(factory.calls[-1]))
        check("switch rebinds token", factory.calls[-1][2] == "tok-6", str(factory.calls[-1]))
    finally:
        sup.stop()
```

Register both in the `main()` tuple.

- [ ] **Step 2: Run to verify failure**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -Ei "no bound server|TypeError|unexpected keyword" | head`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'server_provider'`.

- [ ] **Step 3: Add park/bind to `reachy_app/supervisor.py`**

**(a)** Extend `__init__` — replace:

```python
        client_factory: Callable[..., ConnectorClient] = ConnectorClient,
        crash_backoff: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0),
    ) -> None:
```

with:

```python
        client_factory: Callable[..., ConnectorClient] = ConnectorClient,
        crash_backoff: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0),
        server_provider: Callable[[], dict | None] | None = None,
    ) -> None:
```

and add, next to the other assignments in `__init__`:

```python
        # Returns {"url", "token"} for the bound server, or None -> park (no worker).
        # None provider = Phase-2 behaviour: always run, using config's url/token.
        self._server_provider = server_provider
        self._parked = False
```

**(b)** Make `_build_loop` use the bound server — replace:

```python
    def _build_loop(self) -> ConversationLoop:
        p = self._config.worker_params()
        client = self._client_factory(
            p["connector_url"], timeout_s=p["request_timeout_s"], token=p["connector_token"],
        )
```

with:

```python
    def _current_server(self) -> dict | None:
        """The bound server, or None when parked. Falls back to config (Phase 2)."""
        if self._server_provider is None:
            p = self._config.worker_params()
            return {"url": p["connector_url"], "token": p["connector_token"]}
        return self._server_provider()

    def _build_loop(self) -> ConversationLoop:
        p = self._config.worker_params()
        srv = self._current_server() or {"url": p["connector_url"], "token": p["connector_token"]}
        client = self._client_factory(
            srv["url"], timeout_s=p["request_timeout_s"], token=srv.get("token", ""),
        )
```

**(c)** Park instead of spawning when nothing is bound — replace:

```python
    def _spawn_locked(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker_main, args=(self._stop,), name="reachy-worker", daemon=True,
        )
        self._thread.start()
```

with:

```python
    def _spawn_locked(self) -> None:
        if self._current_server() is None:
            # No brain bound: run no worker at all and let the UI show the gate.
            self._parked = True
            self._thread = None
            self.current_loop = None
            self._status.set("parked")
            log.info("no server bound — parked (UI shows the picker)")
            return
        self._parked = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker_main, args=(self._stop,), name="reachy-worker", daemon=True,
        )
        self._thread.start()
```

**(d)** Add the accessor after `_thread_alive`:

```python
    def is_parked(self) -> bool:
        return self._parked
```

- [ ] **Step 4: Run to verify passing**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A6 "supervisor:"`
Expected: the two new checks pass **and** the Phase-2 supervisor tests still pass (they pass no `server_provider`, so they take the config fallback); `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add reachy_app/supervisor.py reachy_app/tests/test_smoke.py
git commit -m "feat: supervisor parks when no server is bound, binds the selected one"
```

---

### Task 6: Robot — `/servers*` route logic + app wiring

**Files:**
- Modify: `reachy_app/servers.py` (framework-free route helpers)
- Modify: `reachy_app/tests/test_smoke.py`
- Modify: `reachy_app/app.py` (launch/park logic + routes)

**Interfaces:**
- Produces in `servers.py` (framework-free, unit-tested — `app.py` imports `reachy_mini` and can never be imported by the suite):
  - `servers_view(store, listener) -> dict` → `{"discovered": [...], "saved": [public_server...], "selected_id": ...}`; discovered entries gain `"saved": bool`.
  - `select_server(store, supervisor, payload, listener=None, verify=verify_server) -> tuple[int, dict]` — resolve id-or-url → verify via `/whoami` → upsert+select → `supervisor.rebuild()`.
  - `add_server(store, supervisor, payload, verify=verify_server) -> tuple[int, dict]` — manual add-by-address.
- Produces in `app.py`: `GET /servers`, `POST /servers/select`, `POST /servers/add`, `POST /servers/rescan`; launch logic that binds `last_selected_id` if it verifies, else parks.

- [ ] **Step 1: Write the failing tests**

Add to `reachy_app/tests/test_smoke.py` (extend the servers import):

```python
from reachy_app.servers import ServerStore, public_server, servers_view, select_server, add_server
```

```python
class _FakeSup:
    def __init__(self): self.rebuilds = 0
    def rebuild(self): self.rebuilds += 1


def test_servers_view_merges_and_hides_tokens() -> None:
    print("servers: view merges discovered+saved and never exposes a token")
    store = ServerStore(path=_tmp_servers_path())
    store.upsert("id-a", "studio", "http://1.1.1.1:8080", "secret-a")
    store.select("id-a")

    class _Lis:
        def discovered(self): return [
            {"id": "id-a", "name": "studio", "url": "http://1.1.1.1:8080", "seen_at": 1.0},
            {"id": "id-z", "name": "newmac", "url": "http://9.9.9.9:8080", "seen_at": 2.0},
        ]
    v = servers_view(store, _Lis())
    check("selected_id reported", v["selected_id"] == "id-a", str(v["selected_id"]))
    check("saved listed", len(v["saved"]) == 1, str(v["saved"]))
    check("discovered listed", len(v["discovered"]) == 2, str(v["discovered"]))
    by_id = {d["id"]: d for d in v["discovered"]}
    check("known server flagged saved", by_id["id-a"]["saved"] is True, str(by_id["id-a"]))
    check("unknown server flagged unsaved", by_id["id-z"]["saved"] is False, str(by_id["id-z"]))
    check("NO token anywhere in the view", "secret-a" not in json.dumps(v), json.dumps(v)[:200])


def test_select_server_verifies_then_binds() -> None:
    print("servers: select verifies the token via /whoami, then binds + rebuilds")
    store = ServerStore(path=_tmp_servers_path())
    sup = _FakeSup()
    ok_verify = lambda url, token, **k: (True, {"id": "id-a", "name": "studio", "version": "1"})

    code, body = select_server(store, sup, {"url": "http://1.1.1.1:8080", "token": "t"}, verify=ok_verify)
    check("verified select -> 200", code == 200 and body["ok"] is True, str(body))
    check("server saved", store.get("id-a") is not None, str(store.list_saved()))
    check("server selected", store.selected_id == "id-a", str(store.selected_id))
    check("worker rebuilt", sup.rebuilds == 1, str(sup.rebuilds))
    check("response hides the token", "token" not in json.dumps(body), json.dumps(body))

    bad = lambda url, token, **k: (False, "unauthorized")
    code2, body2 = select_server(store, sup, {"id": "id-a", "token": "wrong"}, verify=bad)
    check("bad token -> 401", code2 == 401 and body2["ok"] is False, str(body2))
    check("no rebuild on failure", sup.rebuilds == 1, str(sup.rebuilds))

    code3, body3 = select_server(store, sup, {}, verify=ok_verify)
    check("missing id and url -> 400", code3 == 400, str(body3))

    code4, body4 = select_server(store, sup, {"id": "ghost", "token": "t"}, verify=ok_verify)
    check("unknown saved id -> 404", code4 == 404, str(body4))


def test_select_reuses_stored_token_when_omitted() -> None:
    print("servers: selecting a saved server reuses its stored token")
    store = ServerStore(path=_tmp_servers_path())
    store.upsert("id-a", "studio", "http://1.1.1.1:8080", "stored-tok")
    seen = {}
    def verify(url, token, **k):
        seen["token"] = token
        return True, {"id": "id-a", "name": "studio", "version": "1"}
    code, body = select_server(store, _FakeSup(), {"id": "id-a"}, verify=verify)
    check("selected ok", code == 200, str(body))
    check("used the stored token", seen.get("token") == "stored-tok", str(seen))


def test_add_server_by_address() -> None:
    print("servers: add-by-address verifies then saves (beacon-blocked LAN fallback)")
    store = ServerStore(path=_tmp_servers_path())
    sup = _FakeSup()
    verify = lambda url, token, **k: (True, {"id": "id-m", "name": "manual", "version": "1"})
    code, body = add_server(store, sup, {"url": "http://7.7.7.7:8080", "token": "t"}, verify=verify)
    check("added -> 200", code == 200 and body["ok"] is True, str(body))
    check("saved under the id /whoami reported", store.get("id-m") is not None, str(store.list_saved()))
    code2, body2 = add_server(store, sup, {"token": "t"}, verify=verify)
    check("missing url -> 400", code2 == 400, str(body2))
    bad = lambda url, token, **k: (False, "unauthorized")
    code3, body3 = add_server(store, sup, {"url": "http://8.8.8.8:8080", "token": "x"}, verify=bad)
    check("unverified -> 401, not saved", code3 == 401 and store.get("id-x") is None, str(body3))
```

Add `import json` at the top of the test file if not already imported, and register the four tests in `main()`.

- [ ] **Step 2: Run to verify failure**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -Ei "servers:|ImportError|cannot import" | head`
Expected: FAIL — `ImportError: cannot import name 'servers_view'`.

- [ ] **Step 3: Add the route helpers to `reachy_app/servers.py`**

Extend the imports at the top of `servers.py`:

```python
from .discovery import verify_server
```

Append at the end of `servers.py`:

```python
def servers_view(store: ServerStore, listener) -> dict:
    """Body of `GET /servers`: who is out there, who we know, who is bound.

    Never includes a token — see `public_server`.
    """
    saved = store.list_saved()
    known = {s["id"] for s in saved}
    discovered = []
    for d in (listener.discovered() if listener is not None else []):
        e = dict(d)
        e["saved"] = e.get("id") in known
        discovered.append(e)
    return {
        "discovered": discovered,
        "saved": [public_server(s) for s in saved],
        "selected_id": store.selected_id,
    }


def _bind(store: ServerStore, supervisor, server_id: str, name: str, url: str, token: str) -> dict:
    """Save + select + restart the worker against the newly bound server."""
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
```

- [ ] **Step 4: Run to verify passing**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A10 "servers:"`
Expected: all `servers:` checks pass; `0 failed`.

- [ ] **Step 5: Commit the tested route logic**

```bash
git add reachy_app/servers.py reachy_app/tests/test_smoke.py
git commit -m "feat: framework-free /servers select, add and view helpers"
```

- [ ] **Step 6: Wire it into `reachy_app/app.py` (VERIFY-ON-HARDWARE glue)**

Read `app.py` and confirm each block before editing (match on content).

**(a)** Extend the imports — replace:

```python
from .runtime_config import RuntimeConfig, restart_current_app
from .supervisor import Supervisor, apply_config_request
```

with:

```python
from .discovery import BeaconListener, verify_server
from .runtime_config import RuntimeConfig, restart_current_app
from .servers import ServerStore, add_server, select_server, servers_view
from .supervisor import Supervisor, apply_config_request
```

**(b)** Replace the supervisor construction — replace:

```python
        backend = ReachyMiniBackend(mini=reachy_mini)
        supervisor = Supervisor(
            backend=backend, config=config,
            button=button, status=status, history=history,
        )
```

with:

```python
        backend = ReachyMiniBackend(mini=reachy_mini)

        # --- multi-server discovery + binding ---
        store = ServerStore()
        listener = BeaconListener()
        listener.start()

        def _bound_server() -> dict | None:
            """The server the worker should talk to, or None -> park + show the gate."""
            return store.selected()

        supervisor = Supervisor(
            backend=backend, config=config,
            button=button, status=status, history=history,
            server_provider=_bound_server,
        )

        # Launch logic: rebind the last-used server ONLY if it still verifies.
        # Never auto-switch to some other discovered Mac — the user chooses.
        sel = store.selected()
        if sel is not None:
            ok, info = verify_server(sel["url"], sel.get("token", ""))
            if ok:
                self.logger.info("bound last-used server %s (%s)", sel.get("name"), sel["url"])
            else:
                self.logger.warning("last-used server %s unreachable (%s) — parking",
                                    sel.get("url"), info)
                store.selected_id = None  # park; discovery will offer candidates
        else:
            self.logger.info("no server selected yet — parking until one is picked")
```

**(c)** Add the routes immediately after the existing `POST /restart-app` route:

```python
        @app.get("/servers")
        def _get_servers() -> dict:
            return servers_view(store, listener)

        @app.post("/servers/select")
        def _select_server(payload: dict) -> Response:
            code, body = select_server(store, supervisor, payload, listener=listener)
            return JSONResponse(body, status_code=code)

        @app.post("/servers/add")
        def _add_server(payload: dict) -> Response:
            code, body = add_server(store, supervisor, payload)
            return JSONResponse(body, status_code=code)

        @app.post("/servers/rescan")
        def _rescan_servers() -> dict:
            listener.clear()
            return {"ok": True}
```

**(d)** Stop the listener on shutdown — replace:

```python
        finally:
            supervisor.stop()
```

with:

```python
        finally:
            supervisor.stop()
            listener.stop()
```

- [ ] **Step 7: Verify it compiles and the suite still passes**

Run: `source reachy_app/.venv/bin/activate && python -m py_compile reachy_app/app.py && echo COMPILE_OK`
Expected: `COMPILE_OK`

Run: `python -m reachy_app.tests.test_smoke 2>&1 | tail -4`
Expected: `0 failed`.

- [ ] **Step 8: Commit**

```bash
git add reachy_app/app.py
git commit -m "feat: serve /servers routes and bind the last-used server at launch"
```

---

### Task 7: UI — server picker + the parked gate

**Files:**
- Modify: `reachy_app/static/index.html`
- Modify: `reachy_app/tests/test_smoke.py`

**Interfaces:**
- Consumes: `GET /servers`, `POST /servers/select`, `POST /servers/add`, `POST /servers/rescan`, and `GET /status` returning `"parked"`.
- Produces: a **Server connection** block at the TOP of the Settings tab (above the existing config groups), and a **gate** overlay on the Talk tab shown when `status == "parked"`.

**Design:** Reuse Phase 2's `AUTH` const and graceful-degradation pattern — every fetch is guarded and a failure leaves a notice, never throws. The picker lists discovered + saved servers merged by id, each row showing the name, url, a `LAST USED` badge for the selected one, and a token input that is only required when the server has no stored token (`has_token: false`) or a previous attempt returned 401. The gate reuses the same row renderer so there is one implementation, not two.

- [ ] **Step 1: Write the failing test**

Add to `reachy_app/tests/test_smoke.py` (after `test_settings_panel`):

```python
def test_server_picker_and_gate_markup() -> None:
    print("servers ui: picker in Settings and the parked gate in Talk")
    srv = ButtonServer("127.0.0.1", 8095)
    srv.start()
    time.sleep(0.2)
    try:
        page = urllib.request.urlopen("http://127.0.0.1:8095/", timeout=2).read().decode()
        check("picker container present", 'id="srv-list"' in page, "")
        check("talks to /servers", "/servers" in page, "")
        check("can select a server", "/servers/select" in page, "")
        check("has add-by-address", "/servers/add" in page, "")
        check("has rescan", "/servers/rescan" in page, "")
        check("gate element present", 'id="gate"' in page, "")
        check("gate reacts to parked state", '"parked"' in page or "'parked'" in page, "")
        # the Talk tab and Phase-2 config panel must survive
        check("hold-to-talk preserved", "Hold" in page and "/press" in page, "")
        check("config panel preserved", 'data-cfg="max_utterance_s"' in page, "")
    finally:
        srv.stop()
```

Register it in `main()` right after `test_settings_panel,`.

- [ ] **Step 2: Run to verify failure**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A9 "servers ui:"`
Expected: FAIL — `❌ picker container present`, `❌ gate element present`, etc.

- [ ] **Step 3: Add the gate to the Talk tab**

In `reachy_app/static/index.html`, replace the Talk section:

```html
    <section id="tab-talk" class="tab" data-tab="talk">
      <div id="log"><div id="empty">Hold the button and say something to Reachy…</div></div>
    </section>
```

with:

```html
    <section id="tab-talk" class="tab" data-tab="talk">
      <div id="gate" class="gate hidden">
        <div class="gate-title">🧠 Pick a brain to start</div>
        <div class="gate-sub">Reachy isn't connected to a Mac yet.</div>
        <div id="gate-list" class="srv-list"></div>
        <button id="gate-settings" class="cfg-btn">Open server settings</button>
      </div>
      <div id="log"><div id="empty">Hold the button and say something to Reachy…</div></div>
    </section>
```

- [ ] **Step 4: Add the picker to the Settings tab**

Insert this block immediately AFTER `<p id="cfg-note" class="cfg-note">Loading settings…</p>` and BEFORE the first `<div class="cfg-group">`:

```html
        <div class="cfg-group">
          <div class="cfg-label">Server connection</div>
          <div id="srv-list" class="srv-list"></div>
          <p id="srv-note" class="cfg-note">Looking for connector Macs…</p>
          <div class="srv-actions">
            <button id="srv-rescan" class="cfg-btn ghost">⟳ Rescan</button>
            <button id="srv-addtoggle" class="cfg-btn ghost">＋ Add by address</button>
          </div>
          <div id="srv-add" class="srv-add hidden">
            <input id="srv-add-url" type="text" placeholder="http://192.168.1.20:8080" />
            <input id="srv-add-token" type="password" placeholder="token" />
            <button id="srv-add-go" class="cfg-btn">Connect</button>
          </div>
        </div>
```

- [ ] **Step 5: Add the styles**

Add inside the existing `<style>`, right after the `.cfg-btn:disabled` rule:

```css
  .cfg-btn.ghost { background: transparent; color: var(--fg); border: 1px solid var(--line); }
  .srv-list { display: flex; flex-direction: column; gap: .4rem; padding: .5rem 0; }
  .srv-row { display: flex; align-items: center; gap: .6rem; padding: .6rem .7rem;
             border: 1px solid var(--line); border-radius: .6rem; background: var(--bg); cursor: pointer; }
  .srv-row.offline { opacity: .45; }
  .srv-row.on { border-color: var(--orange); }
  .srv-dot { width: .6rem; height: .6rem; border-radius: 50%; background: var(--grey); flex: 0 0 auto; }
  .srv-row.online .srv-dot { background: var(--green); }
  .srv-meta { flex: 1 1 auto; min-width: 0; }
  .srv-name { font-weight: 600; font-size: .95rem; }
  .srv-url { font-size: .75rem; opacity: .55; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .srv-badge { font-size: .6rem; letter-spacing: .05em; padding: .15rem .4rem; border-radius: .3rem;
               background: var(--line); text-transform: uppercase; }
  .srv-tok { display: flex; gap: .4rem; padding: .1rem 0 .5rem; }
  .srv-tok.hidden { display: none; }
  .srv-tok input { flex: 1 1 auto; background: var(--bg); color: var(--fg);
                   border: 1px solid var(--line); border-radius: .5rem; padding: .4rem .5rem; font: inherit; }
  .srv-actions { display: flex; gap: .5rem; }
  .srv-add { display: flex; flex-direction: column; gap: .4rem; padding: .2rem 0 .6rem; }
  .srv-add.hidden { display: none; }
  .srv-add input { background: var(--bg); color: var(--fg); border: 1px solid var(--line);
                   border-radius: .5rem; padding: .5rem; font: inherit; }
  .gate { margin: auto; padding: 1.5rem; text-align: center; max-width: 22rem; }
  .gate.hidden { display: none; }
  .gate-title { font-size: 1.1rem; font-weight: 600; margin-bottom: .3rem; }
  .gate-sub { font-size: .85rem; opacity: .6; margin-bottom: 1rem; }
  #gate ~ #log.hidden { display: none; }
```

- [ ] **Step 6: Add the picker script**

Append inside the existing `<script>`, immediately before its closing `</script>` (after the Phase-2 config code):

```javascript
  // ---- server picker (Settings) + parked gate (Talk) ----
  const srvList = document.getElementById("srv-list");
  const srvNote = document.getElementById("srv-note");
  const gate = document.getElementById("gate");
  const gateList = document.getElementById("gate-list");
  let srvState = { discovered: [], saved: [], selected_id: null };

  function srvRow(s, online, selected) {
    const row = document.createElement("div");
    row.className = "srv-row" + (online ? " online" : " offline") + (selected ? " on" : "");
    const dot = document.createElement("span"); dot.className = "srv-dot";
    const meta = document.createElement("div"); meta.className = "srv-meta";
    const nm = document.createElement("div"); nm.className = "srv-name"; nm.textContent = s.name || s.id;
    const u = document.createElement("div"); u.className = "srv-url"; u.textContent = s.url || "";
    meta.appendChild(nm); meta.appendChild(u);
    row.appendChild(dot); row.appendChild(meta);
    if (selected) { const b = document.createElement("span"); b.className = "srv-badge"; b.textContent = "last used"; row.appendChild(b); }
    row.addEventListener("click", () => srvSelect(s, row));
    return row;
  }

  function renderServers(target) {
    if (!target) return;
    target.innerHTML = "";
    const online = new Set(srvState.discovered.map(d => d.id));
    const merged = [];
    srvState.discovered.forEach(d => merged.push(d));
    srvState.saved.forEach(s => { if (!online.has(s.id)) merged.push(s); });
    if (!merged.length) {
      srvNote && (srvNote.textContent = "No connector Macs found. Use ＋ Add by address.");
      return;
    }
    merged.forEach(s => target.appendChild(srvRow(s, online.has(s.id), s.id === srvState.selected_id)));
    srvNote && (srvNote.textContent = "Tap a Mac to connect.");
  }

  async function srvLoad() {
    try {
      srvState = await (await fetch("/servers", { headers: AUTH, cache: "no-store" })).json();
      renderServers(srvList); renderServers(gateList);
    } catch (e) {
      srvNote && (srvNote.textContent = "Server list unavailable here.");
    }
  }

  async function srvPost(path, body) {
    try {
      const res = await fetch(path, {
        method: "POST", headers: { "Content-Type": "application/json", ...AUTH },
        body: JSON.stringify(body),
      });
      return { ok: res.ok, data: await res.json().catch(() => ({})) };
    } catch (e) { return { ok: false, data: { error: "unreachable" } }; }
  }

  async function srvSelect(s, row) {
    // Ask for a token only when we have none stored, or the server rejected the last one.
    let token = "";
    if (s.has_token === false || row.dataset.needsToken === "1") {
      const box = document.createElement("div"); box.className = "srv-tok";
      const inp = document.createElement("input"); inp.type = "password"; inp.placeholder = "token";
      const go = document.createElement("button"); go.className = "cfg-btn"; go.textContent = "Connect";
      box.appendChild(inp); box.appendChild(go); row.after(box); inp.focus();
      go.addEventListener("click", async () => {
        const out = await srvPost("/servers/select", { id: s.id, url: s.url, token: inp.value });
        box.remove();
        if (!out.ok) { row.dataset.needsToken = "1"; srvNote && (srvNote.textContent = out.data.error || "Rejected"); }
        else { srvNote && (srvNote.textContent = "Connected."); srvLoad(); }
      });
      return;
    }
    srvNote && (srvNote.textContent = "Connecting…");
    const out = await srvPost("/servers/select", { id: s.id, url: s.url, token });
    if (!out.ok) { row.dataset.needsToken = "1"; srvNote && (srvNote.textContent = out.data.error || "Rejected"); }
    else { srvNote && (srvNote.textContent = "Connected."); srvLoad(); }
  }

  const srvRescan = document.getElementById("srv-rescan");
  if (srvRescan) srvRescan.addEventListener("click", async () => {
    srvNote && (srvNote.textContent = "Rescanning…");
    await srvPost("/servers/rescan", {}); setTimeout(srvLoad, 1200);
  });
  const srvAddToggle = document.getElementById("srv-addtoggle");
  if (srvAddToggle) srvAddToggle.addEventListener("click", () =>
    document.getElementById("srv-add").classList.toggle("hidden"));
  const srvAddGo = document.getElementById("srv-add-go");
  if (srvAddGo) srvAddGo.addEventListener("click", async () => {
    const url = document.getElementById("srv-add-url").value.trim();
    const tok = document.getElementById("srv-add-token").value;
    if (!url) return;
    srvNote && (srvNote.textContent = "Connecting…");
    const out = await srvPost("/servers/add", { url, token: tok });
    srvNote && (srvNote.textContent = out.ok ? "Connected." : (out.data.error || "Rejected"));
    if (out.ok) { document.getElementById("srv-add").classList.add("hidden"); srvLoad(); }
  });
  const gateSettings = document.getElementById("gate-settings");
  if (gateSettings) gateSettings.addEventListener("click", () =>
    document.querySelector('.seg-btn[data-tab="settings"]').click());

  // show the gate whenever the app is parked (no brain bound)
  function applyGate(state) {
    const parked = state === "parked";
    gate.classList.toggle("hidden", !parked);
    logEl.classList.toggle("hidden", parked);
    footer.classList.toggle("hidden", parked || document.querySelector('.seg-btn.on').dataset.tab !== "talk");
    if (parked && !gateList.children.length) srvLoad();
  }
  const _renderStatus = renderStatus;
  renderStatus = function (state) { _renderStatus(state); applyGate(state); };

  document.querySelector('.seg-btn[data-tab="settings"]').addEventListener("click", srvLoad);
  srvLoad();
```

Also change the status-label map so `parked` reads nicely — replace:

```javascript
  const LABELS = { idle: "Ready", listening: "Listening…", thinking: "Thinking…",
                   speaking: "Speaking…", error: "Error", offline: "Offline" };
```

with:

```javascript
  const LABELS = { idle: "Ready", listening: "Listening…", thinking: "Thinking…",
                   speaking: "Speaking…", error: "Error", offline: "Offline",
                   parked: "No server" };
```

and change `function renderStatus(state)` to `let renderStatus = function (state)` so the gate wrapper above can reassign it (a `function` declaration cannot be reassigned safely before use).

- [ ] **Step 7: Run to verify passing**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A10 "servers ui:"`
Expected: all `servers ui:` checks pass, `shell:`/`settings:` checks still pass, `0 failed`.

- [ ] **Step 8: Commit**

```bash
git add reachy_app/static/index.html reachy_app/tests/test_smoke.py
git commit -m "feat: server picker in Settings and the parked gate on Talk"
```

---

## Self-Review

**Spec coverage (Phase 3 rows of the design):**
- Beacon: Mac broadcasts `{reachy_connector,id,name,url}` every ~10 s, no token, gated by `DISCOVERY_BEACON`, `id` persisted, `name` from `SERVER_NAME`/hostname → Task 1 + 2. ✓
- `GET /whoami` token-protected returning `{id,name,version}`; `/health` stays open → Task 2. ✓
- Robot listener → live discovered list with `seen_at` + TTL → Task 3. ✓
- Verify+connect: `/whoami` with the token; 200 + id confirms reachability AND token and defeats a spoofed beacon; 401 → inline token prompt → Task 3 (`verify_server`), Task 6 (`select_server` trusts `/whoami`'s id), Task 7 (inline prompt on 401). ✓
- Saved-servers store `{id,name,url,token,last_used_at}` + `last_selected_id` → Task 4. ✓
- Launch logic: last-used if it verifies → bind silently; else park + gate; **never auto-switch** → Task 6(b). ✓
- Switch anytime from Settings; manual add-by-address for beacon-blocked LANs → Tasks 6, 7. ✓
- Routes `/servers`, `/servers/select`, `/servers/add`, `/servers/rescan` → Task 6. ✓
- UI: picker rows with live status, `LAST USED` badge, offline dimmed, inline token, ＋ Add, ⟳ Rescan; gate with the same rows → Task 7. ✓
- Security posture: beacon carries no secret; token stored only on the robot; `/whoami` cross-checks the id → Tasks 1, 4 (`public_server`), 6. ✓
- **Deferred (correctly absent):** PWA manifest/service worker (spec lists it under UI but it is independent of discovery — call it out at handoff); wake word; auto-switching; mDNS.

**Placeholder scan:** No TBD/TODO. Every code step is complete.

**Automation coverage:** Tasks 1–5 and the Task-6 helpers are fully automated (pytest on the Mac, custom runner on the robot). Only the `app.py` wiring (Task 6 Steps 6–8) is `py_compile`-only, for the same reason as Phase 2: `app.py` imports `reachy_mini`. The `/whoami` route body is unit-tested via `whoami_payload`; its auth comes from the unchanged middleware.

**Type/name consistency:** The wire payload `{"reachy_connector":1,"id","name","url"}` is identical in `server/beacon.py::beacon_payload`, `reachy_app/discovery.py::parse_beacon`, and both test suites. `DISCOVERY_PORT` 48569 matches `settings.discovery_port`'s default and `discovery.DISCOVERY_PORT`. `verify_server(url, token, get=None)` matches its call sites in `select_server`/`add_server` (which pass it as `verify=`) and the tests' fakes. `public_server` returns `has_token` (never `token`) and is the only projection used by `servers_view`/`_bind` responses. `Supervisor(..., server_provider=)` matches Task 5's tests and Task 6's `app.py` wiring; omitting it preserves Phase-2 behaviour so Phase-2 tests still pass.

## VERIFY-ON-HARDWARE (robot + Mac on the same Wi-Fi)

1. **Beacon actually crosses the LAN.** Some APs block broadcast / isolate clients. With the connector running on the Mac: on the robot, `GET http://<robot>:8042/servers` lists the Mac under `discovered` within ~10 s. If not, that is the AP — fall back to ＋ Add by address (which must work).
2. **Token verification end-to-end:** selecting with the right token → 200 and the worker binds (a conversation turn then works); selecting with a wrong token → **401** and the inline token prompt appears; the robot never binds an unverified server.
3. **Launch rebind:** restart the app with a saved+reachable server → it binds silently and the Talk tab is `Ready` (no gate). Stop the Mac connector, restart the app → it **parks**, `/status` is `parked`, and the gate lists candidates.
4. **Switching brains live:** with two connector Macs up, select the other one → the worker rebuilds and the next turn goes to the new Mac (check that Mac's log), with **no app restart**.
5. **Moving networks (the whole point):** move the Mac to another network/IP; its beacon re-advertises the new URL (the URL is rebuilt each tick from `local_ip()`), and re-selecting it reconnects **without** editing `config.env`.

## Execution Handoff

The **PWA** bits from the design (manifest, apple-touch-icon, theme-color, optional service worker) are the only UI item left after this and are independent of discovery — worth a small separate plan. After Phase 3 lands, `CONNECTOR_URL` in `~/.config/reachy-mini-claude/config.env` becomes a fallback only (used when no server is bound); consider retiring it in a follow-up.
