# Dashboard Phase 2 — Supervisor/Worker + Live Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the packaged app's conversation loop run in a **rebuildable worker thread** owned by a **persistent supervisor**, so a curated set of tunables can be edited **live** from the Settings tab (GET/POST `/config`) without restarting the app — the GUI you edit from never dies under you.

**Architecture:** Today `app.run()` builds a `ConversationLoop` and blocks on `loop.run_forever(stop_event)`. Phase 2 introduces a `Supervisor` that owns the shared `ReachyMini` backend + button/status/history + movement, and runs the loop in a **worker thread** it can stop → join → rebuild from a mutable `RuntimeConfig`. Only the `ConversationLoop` + its `ConnectorClient` are rebuilt; the hardware-bound backend and shared state live for the whole app lifetime. `run()` becomes the supervisor host: it registers the routes on `settings_app`, starts the supervisor, then blocks on `stop_event`.

**Tech Stack:** Python 3.10+ (robot venv 3.12), stdlib `threading` + `json`, `requests` (already a dep), vanilla HTML/CSS/JS. Tests: the repo's custom runner `reachy_app/tests/test_smoke.py` (NOT pytest).

## Global Constraints

- **`config.py::settings` (frozen) STAYS** — it is the default source that `RuntimeConfig` seeds from, and the standalone `reachy_app/main.py` + existing tests still import it. Do not remove or change its fields.
- **Do not break `reachy_app/main.py`** (the standalone `python -m reachy_app.main` path). It keeps using `settings` and `loop.run_forever()` directly — it is NOT converted to the supervisor.
- **The shared `ReachyMini` handle is built once by the framework and never closed by us.** `ReachyMiniBackend(mini=...)` is external-handle mode; its `close()` is a no-op (`self._cm is None`). The supervisor reuses one backend instance across all worker rebuilds — never constructs a second backend or a second `ReachyMini`.
- **Only the ConversationLoop + ConnectorClient are rebuilt.** Backend, `ButtonState`, `StatusState`, `History`, and `MovementPlayer` are created once in `app.run()` and shared into every worker.
- **Curated live params only (Phase 2 scope):** `request_timeout_s`, `max_utterance_s`, `log_level`, `reachy_media_backend`. Server url/token editing + the multi-server picker/discovery are **Phase 3** — do not add `/servers*` routes or a server picker here.
- **`reachy_media_backend` is restart-only.** It cannot change live (the framework fixes the media backend when it constructs `ReachyMini` before `run()`); a change to it is persisted and applied only via `POST /restart-app` (daemon bounce). The other three apply live.
- **`index.html` is served by BOTH `settings_app` (:8042, has `/config`) and the stdlib `ButtonServer` (:8081, does NOT).** The Settings config form must **degrade gracefully** when `GET /config` fails (show a notice), never throw.
- **Tests live in `reachy_app/tests/test_smoke.py`** (custom runner, not pytest): add each test *function*, then register it in the `main()` runner tuple. `check(name, cond, extra)` is the assertion helper; a run exits non-zero if any check fails. Run with `python -m reachy_app.tests.test_smoke` from the repo root (robot venv `reachy_app/.venv`, Python 3.12). The `full loop turn` test prints `⏭ SKIPPED` without a connector on :8080 — that is not a failure.
- **Commits:** lowercase-prefixed subject (`feat:` / `refactor:`), and append the trailer `Claude-Session: https://claude.ai/code/session_01PQEE7LUw4A7KFPi2wMh2Dz`.
- Bulgarian STT/TTS and the Mac pipeline are untouched by this phase.

## File Structure

- **Create** `reachy_app/runtime_config.py` — `RuntimeConfig` (mutable, thread-safe, seeded from `settings`, persists a JSON overlay) + the `config_actions()` decision helper + the `restart_current_app()` daemon-bounce helper. One responsibility: "the live-editable config and what a change to it implies."
- **Create** `reachy_app/supervisor.py` — `Supervisor` (worker-thread lifecycle: build / rebuild / crash-restart / stop, sharing one backend). One responsibility: "run the loop in a swappable thread."
- **Modify** `reachy_app/app.py` — `run()` hosts the supervisor and registers `GET /config`, `POST /config`, `POST /restart-app`; the inline `loop.run_forever(...)` is replaced by supervisor lifecycle + a `stop_event` wait.
- **Modify** `reachy_app/static/index.html` — fill the Settings tab placeholder with the curated config form (graceful-degrading).
- **Modify** `reachy_app/tests/test_smoke.py` — new test functions for each task, registered in `main()`.
- **Modify** `reachy_app/README.md` — files map (two new modules) + a one-paragraph "live config" note.

---

### Task 1: `RuntimeConfig` — mutable live config + persistence

**Files:**
- Create: `reachy_app/runtime_config.py`
- Modify: `reachy_app/tests/test_smoke.py` (add 3 test functions, register in `main()`)

**Interfaces:**
- Consumes: `reachy_app.config.settings` (frozen defaults).
- Produces:
  - `RuntimeConfig(path: str | None = None)` with attributes `request_timeout_s: float`, `max_utterance_s: float`, `log_level: str`, `reachy_media_backend: str`, `connector_url: str`, `connector_token: str`.
  - `RuntimeConfig.apply_updates(updates: dict) -> set[str]` — validate + apply live fields atomically (all-or-nothing), persist on change, return the set of fields that actually changed. Raises `ValueError` (nothing applied) on unknown/invalid input.
  - `RuntimeConfig.public_dict() -> dict` — the 4 live fields (for `GET /config`).
  - `RuntimeConfig.worker_params() -> dict` — a locked snapshot `{connector_url, connector_token, request_timeout_s, max_utterance_s}` for the supervisor.
  - `config_actions(changed: set[str]) -> dict` — `{"set_log_level": bool, "rebuild": bool, "restart_required": bool}`.
  - `restart_current_app(post=None, logger=log) -> bool` — POST the daemon restart endpoint; returns success.
  - Module constants `LIVE_FIELDS`, `RUNTIME_PATH`.

- [ ] **Step 1: Write the failing tests**

Add to `reachy_app/tests/test_smoke.py`. Put an import near the top with the others (after the `from reachy_app.movement import (...)` block):

```python
from reachy_app.runtime_config import RuntimeConfig, config_actions, restart_current_app, LIVE_FIELDS
```

Add these three functions (place them after `test_entry_shim_scrapeable`):

```python
def _tmp_runtime_path() -> str:
    import tempfile, os
    d = tempfile.mkdtemp(prefix="reachy-rt-")
    return os.path.join(d, "runtime.json")


def test_runtime_config_persist_roundtrip() -> None:
    print("runtime config: edits persist and reload")
    import os
    path = _tmp_runtime_path()
    cfg = RuntimeConfig(path=path)
    changed = cfg.apply_updates({"max_utterance_s": 42, "log_level": "debug"})
    check("changed set reports both fields", changed == {"max_utterance_s", "log_level"}, str(changed))
    check("log_level upper-cased", cfg.log_level == "DEBUG", cfg.log_level)
    check("runtime.json written", os.path.exists(path), path)
    # a fresh instance on the same path reloads the overlay
    cfg2 = RuntimeConfig(path=path)
    check("max_utterance reloaded", cfg2.max_utterance_s == 42.0, str(cfg2.max_utterance_s))
    check("log_level reloaded", cfg2.log_level == "DEBUG", cfg2.log_level)
    # unchanged fields still come from settings defaults, not the overlay
    check("public_dict has exactly the 4 live fields",
          set(cfg2.public_dict()) == set(LIVE_FIELDS), str(cfg2.public_dict()))


def test_runtime_config_validation_atomic() -> None:
    print("runtime config: bad input is rejected all-or-nothing")
    path = _tmp_runtime_path()
    cfg = RuntimeConfig(path=path)
    before = cfg.max_utterance_s
    raised = False
    try:
        # valid max_utterance paired with an invalid log_level -> whole update rejected
        cfg.apply_updates({"max_utterance_s": 30, "log_level": "LOUD"})
    except ValueError:
        raised = True
    check("invalid update raises ValueError", raised)
    check("nothing applied on rejection (rollback)", cfg.max_utterance_s == before, str(cfg.max_utterance_s))
    raised2 = False
    try:
        cfg.apply_updates({"nonsense": 1})
    except ValueError:
        raised2 = True
    check("unknown field raises ValueError", raised2)
    raised3 = False
    try:
        cfg.apply_updates({"request_timeout_s": 0})  # below the 1..600 floor
    except ValueError:
        raised3 = True
    check("out-of-range value raises ValueError", raised3)
    # no-op update returns an empty changed set and does not raise
    same = cfg.apply_updates({"max_utterance_s": cfg.max_utterance_s})
    check("no-op update -> empty changed set", same == set(), str(same))


def test_config_actions_mapping() -> None:
    print("runtime config: change set maps to the right actions")
    a = config_actions({"log_level"})
    check("log_level -> set level only", a == {"set_log_level": True, "rebuild": False, "restart_required": False}, str(a))
    b = config_actions({"max_utterance_s"})
    check("max_utterance -> rebuild", b["rebuild"] and not b["set_log_level"] and not b["restart_required"], str(b))
    c = config_actions({"request_timeout_s"})
    check("request_timeout -> rebuild", c["rebuild"], str(c))
    d = config_actions({"reachy_media_backend"})
    check("media_backend -> restart only", d == {"set_log_level": False, "rebuild": False, "restart_required": True}, str(d))
    e = config_actions(set())
    check("no change -> no action", e == {"set_log_level": False, "rebuild": False, "restart_required": False}, str(e))


def test_restart_app_posts_daemon() -> None:
    print("runtime config: restart_current_app posts the daemon endpoint")
    calls = []
    class _Resp:
        def raise_for_status(self): pass
    def fake_post(url, timeout=0):
        calls.append((url, timeout))
        return _Resp()
    ok = restart_current_app(post=fake_post)
    check("returns True on success", ok is True)
    check("hits restart-current-app", calls and calls[0][0].endswith("/api/apps/restart-current-app"), str(calls))
    def boom_post(url, timeout=0):
        raise RuntimeError("no daemon")
    ok2 = restart_current_app(post=boom_post)
    check("returns False when the daemon is unreachable", ok2 is False)
```

Register all four in the `main()` runner tuple (add after `test_entry_shim_scrapeable,`):

```python
        test_button_auth, test_entry_shim_scrapeable,
        test_runtime_config_persist_roundtrip, test_runtime_config_validation_atomic,
        test_config_actions_mapping, test_restart_app_posts_daemon,
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source reachy_app/.venv/bin/activate && python -m reachy_app.tests.test_smoke 2>&1 | grep -Ei "runtime config|Error|Traceback" | head`
Expected: FAIL — an `ImportError`/`ModuleNotFoundError` for `reachy_app.runtime_config` (the module doesn't exist yet), aborting the run non-zero.

- [ ] **Step 3: Create `reachy_app/runtime_config.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A5 "runtime config:"`
Expected: every `runtime config:` check passes; the summary shows `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add reachy_app/runtime_config.py reachy_app/tests/test_smoke.py
git commit -m "feat: mutable RuntimeConfig with persistence and change-action mapping"
```

---

### Task 2: `Supervisor` — rebuildable worker thread

**Files:**
- Create: `reachy_app/supervisor.py`
- Modify: `reachy_app/tests/test_smoke.py` (add 3 test functions, register in `main()`)

**Interfaces:**
- Consumes: `RuntimeConfig.worker_params()`; `AudioBackend`; `ButtonState`, `StatusState`, `History`; `ConnectorClient`; `ConversationLoop`.
- Produces:
  - `Supervisor(*, backend, config, button, status, history, client_factory=ConnectorClient, crash_backoff=(1.0, 2.0, 5.0, 10.0))`.
  - `.start()` — spawn the worker thread (idempotent while running).
  - `.rebuild()` — stop → join the current worker, spawn a fresh one from current config.
  - `.stop()` — stop → join; no further workers.
  - `.current_loop: ConversationLoop | None` — the live loop (introspection / tests).

**Interface note (how the worker idles safely in tests):** `ConversationLoop.run_forever` checks `stop_event` between turns and, with `button` present but no press and `wake=None`, simply polls and sleeps — so a worker with a fresh `ButtonState` (never pressed) runs harmlessly until `stop_event` is set. Rebuild at idle is near-instant; a rebuild requested mid-turn waits for that turn to finish (the `join`).

- [ ] **Step 1: Write the failing tests**

Add to `reachy_app/tests/test_smoke.py`. Add an import near the top:

```python
from reachy_app.supervisor import Supervisor
from reachy_app.button_server import ButtonState, History  # StatusState already imported
```

Add these helpers + tests (after the runtime-config tests):

```python
class _RecordingClientFactory:
    """Captures ConnectorClient construction args; returns a harmless stub."""
    def __init__(self) -> None:
        self.calls: list = []
    def __call__(self, url, timeout_s=180.0, token=""):
        self.calls.append((url, timeout_s, token))
        return object()  # never used: no trigger fires in these tests


def _wait_until(pred, timeout=3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_supervisor_rebuild_swaps_params() -> None:
    print("supervisor: rebuild swaps loop params and rebuilds the client")
    path = _tmp_runtime_path()
    cfg = RuntimeConfig(path=path)
    factory = _RecordingClientFactory()
    fake = FakeBackend(pcm_to_wav(np.zeros(1600, dtype=np.float32), 16000))
    sup = Supervisor(backend=fake, config=cfg, button=ButtonState(),
                     status=StatusState(), history=History(), client_factory=factory)
    sup.start()
    try:
        check("worker builds a loop", _wait_until(lambda: sup.current_loop is not None))
        first = sup.current_loop
        check("loop uses seeded max_utterance",
              first.max_utterance_s == cfg.max_utterance_s, str(first.max_utterance_s))
        cfg.apply_updates({"max_utterance_s": 42, "request_timeout_s": 33})
        sup.rebuild()
        check("rebuild produced a NEW loop", _wait_until(lambda: sup.current_loop is not None and sup.current_loop is not first))
        check("new loop uses updated max_utterance", sup.current_loop.max_utterance_s == 42.0,
              str(sup.current_loop.max_utterance_s))
        check("client rebuilt with new timeout", factory.calls[-1][1] == 33.0, str(factory.calls[-1]))
    finally:
        sup.stop()
    check("worker thread stopped", _wait_until(lambda: sup.current_loop is not None) and not sup._thread_alive())


def test_supervisor_stop_is_clean() -> None:
    print("supervisor: stop joins the worker and blocks further rebuilds")
    cfg = RuntimeConfig(path=_tmp_runtime_path())
    fake = FakeBackend(pcm_to_wav(np.zeros(1600, dtype=np.float32), 16000))
    sup = Supervisor(backend=fake, config=cfg, button=ButtonState(),
                     status=StatusState(), history=History(),
                     client_factory=_RecordingClientFactory())
    sup.start()
    check("started", _wait_until(lambda: sup.current_loop is not None))
    sup.stop()
    check("thread not alive after stop", not sup._thread_alive())
    sup.rebuild()  # must be a no-op after shutdown, not raise
    check("rebuild after stop stays stopped", not sup._thread_alive())


def test_supervisor_crash_restarts_and_reports_error() -> None:
    print("supervisor: a worker crash sets error state and restarts (bounded)")
    cfg = RuntimeConfig(path=_tmp_runtime_path())

    class _CrashOnceBackend(FakeBackend):
        def __init__(self, wav):
            super().__init__(wav)
            self.enters = 0
        def enter_idle(self):
            self.enters += 1
            if self.enters == 1:
                raise RuntimeError("boom")  # crash the first worker run
            super().enter_idle()

    fake = _CrashOnceBackend(pcm_to_wav(np.zeros(1600, dtype=np.float32), 16000))
    status = StatusState()
    sup = Supervisor(backend=fake, config=cfg, button=ButtonState(),
                     status=status, history=History(),
                     client_factory=_RecordingClientFactory(),
                     crash_backoff=(0.02,))
    sup.start()
    try:
        check("error state published on crash", _wait_until(lambda: status.get() == "error"))
        check("worker restarts after backoff", _wait_until(lambda: fake.enters >= 2))
    finally:
        sup.stop()
    check("thread stopped after restart", not sup._thread_alive())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -Ei "supervisor:|Error|Traceback" | head`
Expected: FAIL — `ModuleNotFoundError: reachy_app.supervisor`, aborting the run non-zero.

- [ ] **Step 3: Create `reachy_app/supervisor.py`**

```python
# reachy_app/supervisor.py
"""Persistent supervisor + rebuildable worker thread.

The dashboard launches the app once; that process (the supervisor's host,
`app.run()`) owns the single ReachyMini handle and serves the Settings UI, and it
must stay up so the GUI never dies under the user. The conversation loop runs in a
*worker thread* that the supervisor can tear down and rebuild whenever the live
config changes — the new loop is built from the current RuntimeConfig and points at
the SAME shared backend, button/status/history, and (indirectly) movement.

Only the ConversationLoop + its ConnectorClient are rebuilt. The hardware-bound
backend and the shared state objects live for the whole app lifetime — the backend
is external-handle mode, so the loop's `backend.close()` on teardown is a no-op and
the handle survives every rebuild.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from .audio import AudioBackend
from .button_server import ButtonState, History, StatusState
from .connector_client import ConnectorClient
from .loop import ConversationLoop
from .runtime_config import RuntimeConfig

log = logging.getLogger("reachy.supervisor")


class Supervisor:
    def __init__(
        self,
        *,
        backend: AudioBackend,
        config: RuntimeConfig,
        button: ButtonState,
        status: StatusState,
        history: History,
        client_factory: Callable[..., ConnectorClient] = ConnectorClient,
        crash_backoff: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0),
    ) -> None:
        self._backend = backend
        self._config = config
        self._button = button
        self._status = status
        self._history = history
        self._client_factory = client_factory
        self._crash_backoff = crash_backoff
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()   # signals the CURRENT worker to exit
        self._shutdown = False
        self.current_loop: ConversationLoop | None = None

    # -- building the loop from current config --
    def _build_loop(self) -> ConversationLoop:
        p = self._config.worker_params()
        client = self._client_factory(
            p["connector_url"], timeout_s=p["request_timeout_s"], token=p["connector_token"],
        )
        return ConversationLoop(
            backend=self._backend,
            client=client,
            button=self._button,
            wake=None,  # push-to-talk only in the on-robot app
            on_state=self._status.set,
            on_turn=self._history.add,
            max_utterance_s=p["max_utterance_s"],
        )

    def _worker_main(self, stop: threading.Event) -> None:
        crashes = 0
        while not stop.is_set():
            loop = self._build_loop()
            self.current_loop = loop
            try:
                loop.run_forever(stop_event=stop)
                break  # clean return == stop was set
            except Exception:
                log.exception("worker crashed")
                self._status.set("error")
                if stop.is_set():
                    break
                delay = self._crash_backoff[min(crashes, len(self._crash_backoff) - 1)]
                crashes += 1
                log.warning("restarting worker in %.2fs", delay)
                stop.wait(delay)

    # -- lifecycle (all worker swaps go through the lock) --
    def start(self) -> None:
        with self._lock:
            if self._shutdown or self._thread is not None:
                return
            self._spawn_locked()

    def rebuild(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._stop_worker_locked()
            self._spawn_locked()

    def stop(self) -> None:
        with self._lock:
            self._shutdown = True
            self._stop_worker_locked()

    def _spawn_locked(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker_main, args=(self._stop,), name="reachy-worker", daemon=True,
        )
        self._thread.start()

    def _stop_worker_locked(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        # run_forever checks stop between turns; an in-flight turn finishes first.
        self._thread.join()
        self._thread = None

    def _thread_alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A6 "supervisor:"`
Expected: every `supervisor:` check passes; summary `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add reachy_app/supervisor.py reachy_app/tests/test_smoke.py
git commit -m "feat: Supervisor with a rebuildable, crash-restarting worker thread"
```

---

### Task 3: Route logic + wire the supervisor into `app.py`

**Files:**
- Modify: `reachy_app/supervisor.py` (add the framework-free `apply_config_request` helper)
- Modify: `reachy_app/tests/test_smoke.py` (add `test_apply_config_request_flow`, register in `main()`)
- Modify: `reachy_app/app.py` (imports; `run()` hosts the supervisor + registers routes; `__init__` sources the media backend)
- Modify: `reachy_app/README.md` (files map + live-config note)

**Interfaces:**
- Consumes: `RuntimeConfig`, `config_actions`, `restart_current_app` (from `runtime_config`); `Supervisor`.
- Produces:
  - `apply_config_request(config, supervisor, payload) -> tuple[int, dict]` in `supervisor.py` — the whole `POST /config` body (validate + persist → set log level / rebuild → response), **framework-free so it is unit-tested on the Mac without the robot or FastAPI**.
  - On `self.settings_app`: `GET /config`, `POST /config`, `POST /restart-app`. `run()` hosts the supervisor and blocks on `stop_event`.

**Automation boundary:** The route *decision logic* is fully covered by `test_apply_config_request_flow` (Step 1) — `apply_config_request` is what actually validates, sets the log level, and triggers a rebuild. What stays **VERIFY-ON-HARDWARE** is only the un-fakeable framework/daemon glue: the framework booting `run()` with a real `ReachyMini`, the FastAPI plumbing, the `/restart-app` daemon bounce, and the media-backend value taking effect after a restart. Those are marked in Steps 6–7 and the VERIFY section; everything landing before the robot is fully automated.

- [ ] **Step 1: Write the failing test for the route logic**

Add to `reachy_app/tests/test_smoke.py` (after the supervisor tests). It exercises the real `POST /config` body without FastAPI or the robot, using a fake supervisor that records `rebuild()`:

```python
def test_apply_config_request_flow() -> None:
    print("config route: apply_config_request validates, sets level, rebuilds, flags restart")
    import logging as _logging
    from reachy_app.supervisor import apply_config_request
    cfg = RuntimeConfig(path=_tmp_runtime_path())

    class _FakeSup:
        def __init__(self): self.rebuilds = 0
        def rebuild(self): self.rebuilds += 1

    sup = _FakeSup()
    root_before = _logging.getLogger().level
    try:
        # invalid value -> 400, nothing changed, no rebuild
        code, body = apply_config_request(cfg, sup, {"log_level": "LOUD"})
        check("invalid -> 400", code == 400 and body["ok"] is False, str(body))
        check("invalid -> no rebuild", sup.rebuilds == 0)
        # log_level -> 200, sets the root logger level, no rebuild
        code, body = apply_config_request(cfg, sup, {"log_level": "warning"})
        check("log_level -> 200", code == 200 and body["changed"] == ["log_level"], str(body))
        check("root logger level set", _logging.getLogger().level == _logging.WARNING)
        check("log_level -> no rebuild", sup.rebuilds == 0)
        # worker-affecting param -> rebuild once, response echoes the new value
        code, body = apply_config_request(cfg, sup, {"max_utterance_s": 25})
        check("worker param -> rebuild", sup.rebuilds == 1, str(sup.rebuilds))
        check("response echoes config", body["config"]["max_utterance_s"] == 25.0, str(body["config"]))
        check("worker param -> not restart_required", body["restart_required"] is False, str(body))
        # media backend -> restart_required, NOT a live rebuild
        code, body = apply_config_request(cfg, sup, {"reachy_media_backend": "local"})
        check("media backend -> restart_required", body["restart_required"] is True, str(body))
        check("media backend -> no extra rebuild", sup.rebuilds == 1, str(sup.rebuilds))
    finally:
        _logging.getLogger().setLevel(root_before)  # don't leak the level into later tests
```

Register it in the `main()` runner tuple right after `test_supervisor_crash_restarts_and_reports_error,` (added in Task 2):

```python
        test_apply_config_request_flow,
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -Ei "config route:|ImportError|AttributeError|Traceback" | head`
Expected: FAIL — `ImportError: cannot import name 'apply_config_request'` (the helper doesn't exist yet), aborting the run non-zero.

- [ ] **Step 3: Add `apply_config_request` to `reachy_app/supervisor.py`**

First extend the runtime-config import at the top of `supervisor.py`:

```python
from .runtime_config import RuntimeConfig
```

to:

```python
from .runtime_config import RuntimeConfig, config_actions
```

Then append this function at the END of `supervisor.py` (module level, after the `Supervisor` class):

```python
def apply_config_request(config: RuntimeConfig, supervisor: "Supervisor", payload: dict) -> tuple[int, dict]:
    """Shared body of `POST /config`: validate + persist, then act on what changed.

    Framework-free (no FastAPI, no robot) so it is unit-testable on the Mac.
    Returns (status_code, response_dict); `app.run()` wraps it in a JSONResponse.
    `supervisor` only needs a `.rebuild()` method (duck-typed for tests).
    """
    try:
        changed = config.apply_updates(payload)
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    actions = config_actions(changed)
    if actions["set_log_level"]:
        logging.getLogger().setLevel(config.log_level)
    if actions["rebuild"]:
        supervisor.rebuild()
    return 200, {
        "ok": True,
        "changed": sorted(changed),
        "config": config.public_dict(),
        "restart_required": actions["restart_required"],
    }
```

(`logging` is already imported at the top of `supervisor.py`.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A8 "config route:"`
Expected: every `config route:` check passes; summary `0 failed`.

- [ ] **Step 5: Commit the tested route logic**

```bash
git add reachy_app/supervisor.py reachy_app/tests/test_smoke.py
git commit -m "feat: framework-free apply_config_request for the POST /config body"
```

- [ ] **Step 6: Wire it into `reachy_app/app.py` (VERIFY-ON-HARDWARE glue)**

First `python -c "import ast; ast.parse(open('reachy_app/app.py').read()); print('parses OK')"` and read `reachy_app/app.py` to confirm the blocks below before replacing (line numbers may have drifted — match on content).

**(a) Imports** — replace this block (currently around lines 33-38):

```python
from .audio import ReachyMiniBackend
from .button_server import ButtonState, History, StatusState
from .config import settings
from .connector_client import ConnectorClient
from .loop import ConversationLoop
from .movement import MovementPlayer
```

with:

```python
import logging

from .audio import ReachyMiniBackend
from .button_server import ButtonState, History, StatusState
from .connector_client import ConnectorClient
from .movement import MovementPlayer
from .runtime_config import RuntimeConfig, restart_current_app
from .supervisor import Supervisor, apply_config_request
```

(`ConversationLoop` and `from .config import settings` are no longer used directly by `app.py` — the supervisor builds the loop and `RuntimeConfig` seeds from `settings` internally. Removing them is intended.)

**(b) Source the media backend from persisted config** — add an `__init__` to the class, immediately after the class attributes (after the `request_media_backend: str | None = None` line, around line 46):

```python
    def __init__(self) -> None:
        super().__init__()
        # The framework fixes the media backend when it builds ReachyMini, BEFORE run(),
        # from this attribute — so a persisted change only takes effect on the next app
        # start (that is why the UI calls POST /restart-app for it). Leave None ("framework
        # default") unless the user picked a non-default. VERIFY-ON-HARDWARE.
        mb = RuntimeConfig().reachy_media_backend
        if mb and mb != "default":
            self.request_media_backend = mb
```

**(c) Client construction + health log** — replace this block (around lines 49-59):

```python
        from fastapi.responses import Response

        client = ConnectorClient(
            settings.connector_url,
            timeout_s=settings.request_timeout_s,
            token=settings.connector_token,
        )
        try:
            self.logger.info("connector: %s", client.health())
        except Exception as e:  # keep going; per-turn errors surface in the UI
            self.logger.warning("connector not reachable at %s (%s)", settings.connector_url, e)
```

with:

```python
        from fastapi.responses import JSONResponse, Response

        config = RuntimeConfig()
        logging.getLogger().setLevel(config.log_level)

        # Best-effort readiness log; the worker owns the real per-turn client.
        try:
            probe = ConnectorClient(config.connector_url, token=config.connector_token)
            self.logger.info("connector: %s", probe.health())
        except Exception as e:  # keep going; per-turn errors surface in the UI
            self.logger.warning("connector not reachable at %s (%s)", config.connector_url, e)
```

**(d) Loop → supervisor + routes** — replace the tail of `run()` (from `backend = ReachyMiniBackend(mini=reachy_mini)` through `loop.run_forever(stop_event=stop_event)`, currently around lines 148-162):

```python
        backend = ReachyMiniBackend(mini=reachy_mini)
        loop = ConversationLoop(
            backend=backend,
            client=client,
            button=button,
            wake=None,  # wake word is standalone-only for now; button triggers here
            on_state=status.set,
            on_turn=history.add,
            vad_rms_threshold=settings.vad_rms_threshold,
            vad_silence_ms=settings.vad_silence_ms,
            vad_min_speech_ms=settings.vad_min_speech_ms,
            max_utterance_s=settings.max_utterance_s,
        )
        self.logger.info("Reachy Claude connector app running — open %s to talk.", self.custom_app_url)
        loop.run_forever(stop_event=stop_event)
```

with:

```python
        backend = ReachyMiniBackend(mini=reachy_mini)
        supervisor = Supervisor(
            backend=backend, config=config,
            button=button, status=status, history=history,
        )

        @app.get("/config")
        def _get_config() -> dict:
            return config.public_dict()

        @app.post("/config")
        def _post_config(payload: dict) -> Response:
            code, body = apply_config_request(config, supervisor, payload)
            return JSONResponse(body, status_code=code)

        @app.post("/restart-app")
        def _restart_app() -> dict:
            return {"ok": restart_current_app(logger=self.logger)}

        self.logger.info("Reachy Claude connector app running — open %s to talk.", self.custom_app_url)
        supervisor.start()
        try:
            while not stop_event.is_set():
                stop_event.wait(0.2)
        finally:
            supervisor.stop()
```

- [ ] **Step 7: Verify it compiles and the whole suite still passes**

Run: `python -m py_compile reachy_app/app.py && echo COMPILE_OK`
Expected: `COMPILE_OK`

Run: `python -m reachy_app.tests.test_smoke 2>&1 | tail -5`
Expected: summary `0 failed`. (`app.py` imports `reachy_mini`, so the suite never imports it — the runtime-config/supervisor tests still pass. `py_compile` is the automated guard for the wiring; its runtime behaviour under the framework is VERIFY-ON-HARDWARE.)

- [ ] **Step 8: Update `reachy_app/README.md`**

In the `## Files` list, add two lines (next to the other module entries):

```
- `runtime_config.py` — mutable, persisted live config (the Settings-tab knobs); seeds from `config.py`.
- `supervisor.py` — persistent supervisor + rebuildable worker thread; applies live config without restarting the app.
```

And add a short paragraph under the files list:

```
**Live config (packaged app):** the dashboard-launched process is a *supervisor* that
owns the ReachyMini handle and serves the UI; the conversation loop runs in a *worker*
thread it rebuilds when you change a setting on the Settings tab (`POST /config`). Reply
timeout, max utterance, and log level apply live; the audio pipeline (media backend) needs
`POST /restart-app` (a daemon bounce). The standalone `main.py` path is unchanged.
```

- [ ] **Step 9: Commit the app wiring**

```bash
git add reachy_app/app.py reachy_app/README.md
git commit -m "feat: host the supervisor in app.run and serve /config + /restart-app"
```

---

### Task 4: Settings-tab config panel (graceful-degrading)

**Files:**
- Modify: `reachy_app/static/index.html` (fill the Settings placeholder; add a config script block)
- Modify: `reachy_app/tests/test_smoke.py` (add `test_settings_panel`, register in `main()`)

**Interfaces:**
- Consumes: `GET /config` (returns the 4 live fields), `POST /config` (`{field: value}` → `{ok, changed, config, restart_required}`), `POST /restart-app`.
- Produces: a Settings form whose inputs carry `data-cfg="<field>"`; a `#cfg-note` status line; graceful degradation when `/config` is unavailable (the ButtonServer origin).

**Design:** The Talk tab and its script are untouched. Replace ONLY the Settings `<section>` placeholder, and ADD one `<script>` block after the existing one. On first switch to Settings (and on load), `GET /config`; populate inputs; `change` on an input `POST`s just that field; the Audio-pipeline row has a separate **Save & restart** button that `POST`s the media backend then calls `/restart-app`. Any fetch failure flips `#cfg-note` to a "Settings need the desktop app" notice and disables the inputs — never throws.

- [ ] **Step 1: Write the failing test**

Add to `reachy_app/tests/test_smoke.py` (after `test_shell_tabs`):

```python
def test_settings_panel() -> None:
    print("settings: page has the live-config form wired to /config")
    srv = ButtonServer("127.0.0.1", 8096)
    srv.start()
    time.sleep(0.2)
    try:
        page = urllib.request.urlopen("http://127.0.0.1:8096/", timeout=2).read().decode()
        check("form talks to /config", "/config" in page, "")
        check("has reply-timeout field", 'data-cfg="request_timeout_s"' in page, "")
        check("has max-utterance field", 'data-cfg="max_utterance_s"' in page, "")
        check("has log-level field", 'data-cfg="log_level"' in page, "")
        check("has media-backend field", 'data-cfg="reachy_media_backend"' in page, "")
        check("has restart-app action", "/restart-app" in page, "")
    finally:
        srv.stop()
```

Register it in `main()` right after `test_shell_tabs,`:

```python
        test_wav_roundtrip, test_endpointer, test_button_server, test_shell_tabs,
        test_settings_panel,
        test_button_auth, test_entry_shim_scrapeable,
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A6 "settings:"`
Expected: FAIL — `❌ has reply-timeout field` etc. (the Settings tab is still the Phase-1 placeholder).

- [ ] **Step 3: Replace the Settings placeholder section**

In `reachy_app/static/index.html`, replace this Phase-1 block:

```html
    <section id="tab-settings" class="tab hidden" data-tab="settings">
      <div class="placeholder">⚙️ Settings<br>Server connection & tuning arrive in the next update.</div>
    </section>
```

with:

```html
    <section id="tab-settings" class="tab hidden" data-tab="settings">
      <div id="cfg">
        <p id="cfg-note" class="cfg-note">Loading settings…</p>

        <div class="cfg-group">
          <label class="cfg-row">
            <span>Reply timeout <em>(seconds)</em></span>
            <input type="number" min="1" max="600" step="1" data-cfg="request_timeout_s" disabled />
          </label>
          <label class="cfg-row">
            <span>Max utterance <em>(seconds)</em></span>
            <input type="number" min="1" max="120" step="1" data-cfg="max_utterance_s" disabled />
          </label>
          <label class="cfg-row">
            <span>Log level</span>
            <select data-cfg="log_level" disabled>
              <option>DEBUG</option><option>INFO</option><option>WARNING</option>
              <option>ERROR</option><option>CRITICAL</option>
            </select>
          </label>
        </div>

        <div class="cfg-group">
          <div class="cfg-label">Advanced</div>
          <label class="cfg-row">
            <span>Audio pipeline <em>(restarts the app)</em></span>
            <select data-cfg="reachy_media_backend" disabled>
              <option>default</option><option>local</option><option>webrtc</option>
            </select>
          </label>
          <button id="cfg-restart" class="cfg-btn" disabled>Save &amp; restart app</button>
        </div>
      </div>
    </section>
```

Add these style rules inside the existing `<style>` block, right after the `.placeholder { … }` rule:

```css
  #cfg { padding: 1rem 1rem 2rem; overflow-y: auto; }
  .cfg-note { margin: 0 0 1rem; font-size: .85rem; opacity: .7; }
  .cfg-note.err { color: var(--red); opacity: 1; }
  .cfg-group { background: var(--card); border: 1px solid var(--line); border-radius: .8rem;
               padding: .3rem .9rem; margin-bottom: 1rem; }
  .cfg-label { font-size: .75rem; letter-spacing: .04em; text-transform: uppercase;
               opacity: .5; padding: .7rem 0 .2rem; }
  .cfg-row { display: flex; align-items: center; justify-content: space-between; gap: 1rem;
             padding: .7rem 0; border-bottom: 1px solid var(--line); font-size: .95rem; }
  .cfg-group .cfg-row:last-of-type { border-bottom: none; }
  .cfg-row em { opacity: .5; font-style: normal; font-size: .8rem; }
  .cfg-row input, .cfg-row select { background: var(--bg); color: var(--fg);
             border: 1px solid var(--line); border-radius: .5rem; padding: .4rem .5rem;
             font: inherit; min-width: 6.5rem; }
  .cfg-row input:disabled, .cfg-row select:disabled { opacity: .45; }
  .cfg-btn { width: 100%; padding: .7rem; margin: .3rem 0 .6rem; border: none; border-radius: .6rem;
             background: var(--amber); color: #1c1e22; font: inherit; font-weight: 600; cursor: pointer; }
  .cfg-btn:disabled { opacity: .45; cursor: default; }
```

- [ ] **Step 4: Add the config-panel script**

In `reachy_app/static/index.html`, immediately BEFORE the closing `</script>` of the existing script block (right after the `setInterval(pollStatus, 350); setInterval(pollHistory, 1000); pollStatus(); pollHistory();` lines), add:

```javascript
  // ---- Settings config panel (settings_app origin only; degrades on the phone page) ----
  const cfgNote = document.getElementById("cfg-note");
  const cfgInputs = Array.from(document.querySelectorAll("[data-cfg]"));
  const cfgRestart = document.getElementById("cfg-restart");
  let cfgLoaded = false;

  function cfgSet(enabled, note, isErr) {
    cfgInputs.forEach(el => { el.disabled = !enabled; });
    if (cfgRestart) cfgRestart.disabled = !enabled;
    if (note !== undefined) { cfgNote.textContent = note; cfgNote.classList.toggle("err", !!isErr); }
  }

  async function cfgLoad() {
    if (cfgLoaded) return;
    try {
      const data = await (await fetch("/config", { headers: AUTH, cache: "no-store" })).json();
      cfgInputs.forEach(el => { if (el.dataset.cfg in data) el.value = data[el.dataset.cfg]; });
      cfgSet(true, "Changes apply live. Audio pipeline needs a restart.", false);
      cfgLoaded = true;
    } catch (e) {
      cfgSet(false, "Settings are available inside the Reachy desktop app.", true);
    }
  }

  async function cfgPost(field, value) {
    cfgNote.classList.remove("err");
    try {
      const res = await fetch("/config", {
        method: "POST", headers: { "Content-Type": "application/json", ...AUTH },
        body: JSON.stringify({ [field]: value }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) { cfgNote.textContent = out.error || "Rejected"; cfgNote.classList.add("err"); return null; }
      cfgNote.textContent = "Saved.";
      return out;
    } catch (e) { cfgNote.textContent = "Save failed"; cfgNote.classList.add("err"); return null; }
  }

  cfgInputs.forEach(el => el.addEventListener("change", () => {
    if (el.dataset.cfg === "reachy_media_backend") return;  // restart-gated; handled below
    cfgPost(el.dataset.cfg, el.type === "number" ? Number(el.value) : el.value);
  }));

  if (cfgRestart) cfgRestart.addEventListener("click", async () => {
    const mb = cfgInputs.find(el => el.dataset.cfg === "reachy_media_backend");
    const out = await cfgPost("reachy_media_backend", mb ? mb.value : "default");
    if (!out) return;
    cfgNote.textContent = "Restarting the app…";
    try { await fetch("/restart-app", { method: "POST", headers: AUTH }); } catch (e) { /* app is going down */ }
  });

  // load config the first time Settings is opened
  document.querySelector('.seg-btn[data-tab="settings"]').addEventListener("click", cfgLoad);
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A7 "settings:"`
Expected: every `settings:` check passes; summary `0 failed`. The `shell:` checks still pass (Talk tab untouched).

(Optional eyeball, not required: `open reachy_app/static/index.html` — the Settings tab shows the form; with no server the note reads "Settings are available inside the Reachy desktop app." and inputs are disabled.)

- [ ] **Step 6: Commit**

```bash
git add reachy_app/static/index.html reachy_app/tests/test_smoke.py
git commit -m "feat: live-config panel on the Settings tab (graceful-degrading)"
```

---

## Self-Review

**Spec coverage (Phase 2 rows of the design):**
- "Persistent supervisor + restartable in-process worker … rebuilt on any config/server change" → Task 2 (`Supervisor.rebuild`), hosted in Task 3. ✓
- "replace the frozen import-time `Settings` with a mutable runtime config the worker reads at (re)build time" → Task 1 (`RuntimeConfig`, `worker_params()`), read in `Supervisor._build_loop`. ✓ (`settings` retained as the seed, per Global Constraints — the standalone path still needs it.)
- "Live tunables persist to the config dir" → Task 1 (`~/.config/reachy-mini-claude/runtime.json`, atomic write). ✓
- `GET /config` / `POST /config` (update → supervisor rebuilds) → Task 3, logic in `apply_config_request` (automated test) + the FastAPI adapter in `run()`. ✓
- `POST /restart-app` (media-backend change → daemon restart) → Task 1 helper (`restart_current_app`, automated test with injected `post`) + Task 3 route + `__init__` media-backend sourcing. ✓
- Config params table: `REQUEST_TIMEOUT_S`, `MAX_UTTERANCE_S`, `LOG_LEVEL` live; `reachy_media_backend` restart → Tasks 1/3/4. ✓
- "Worker crash → supervisor catches, marks error, auto-restarts with backoff; UI never dies" → Task 2 (`_worker_main` backoff + `status.set("error")`). ✓
- Curated config, push-to-talk only (`wake=None`, no VAD knobs) → `Supervisor._build_loop`. ✓
- **Deferred to Phase 3 (correctly absent):** `/servers*`, discovery/beacon, the picker + gate + park logic, `/whoami`, per-server tokens. Server url/token editing is deliberately out of Phase 2's `/config` (noted in Global Constraints).

**Placeholder scan:** No "TBD/TODO/handle appropriately". Every code step is complete.

**Automation coverage (the travelling constraint):** Tasks 1, 2, 4 are fully automated on the Mac (custom runner). Task 3's route *decision logic* is automated via `test_apply_config_request_flow` (`apply_config_request` is framework-free) and Steps 1–5 land tested-and-committed *before* the app wiring. The only thing that lands as compile-checked-but-not-runtime-tested is the FastAPI/framework glue in `app.py` (Steps 6–7), because `app.py` imports `reachy_mini`; its runtime behaviour is the VERIFY-ON-HARDWARE list.

**Type/name consistency:** `LIVE_FIELDS` = `("request_timeout_s","max_utterance_s","log_level","reachy_media_backend")` is identical in `runtime_config.py`, the `config_actions` sets, the UI `data-cfg` attributes, and the tests. `worker_params()` keys (`connector_url/connector_token/request_timeout_s/max_utterance_s`) match `Supervisor._build_loop`'s use. `Supervisor(*, backend, config, button, status, history, client_factory, crash_backoff)` signature matches every call site (tests + `app.run`). `apply_config_request(config, supervisor, payload) -> (int, dict)` matches the test's fake supervisor and `run()`'s adapter. `config_actions` returns exactly `{set_log_level, rebuild, restart_required}`, consumed in `apply_config_request`. `restart_current_app(post=None, logger=log)` matches the test's injected `post` and the route's `logger=self.logger`.

## VERIFY-ON-HARDWARE (needs the robot / desktop daemon)

1. `run()` on the real framework: the supervisor's worker thread runs the loop while `settings_app` serves `/config` concurrently, and `POST /config` from the settings_app thread cleanly rebuilds the worker (config applied on the next turn; near-instant at idle).
2. Editing **Reply timeout** / **Max utterance** / **Log level** in the embedded Settings tab applies without the app restarting (the GUI stays alive).
3. `POST /restart-app` → the daemon endpoint (`REACHY_DAEMON_URL` default `http://localhost:8000` + `/api/apps/restart-current-app`) actually bounces the app, and the dashboard re-embeds on readiness. Confirm the base URL/path on the robot.
4. The persisted **Audio pipeline** (media backend) value is picked up by `ReachyClaudeConnectorApp.__init__` → `request_media_backend` and actually changes the backend the framework constructs after the restart (confirm a non-`default` value like `local`/`webrtc` behaves as intended, or is a no-op the framework tolerates).
5. A worker crash (e.g. transient backend fault) is caught, surfaces `error` state on the Talk tab, and auto-restarts — the supervisor/UI never dies.

## Execution Handoff

Phase 3 (multi-server LAN discovery — Mac beacon + `/whoami`, robot listener + saved-servers store, the picker + gate + launch/park logic, `/servers*`) gets its own plan and reuses this phase's `Supervisor.rebuild()` to switch servers.
