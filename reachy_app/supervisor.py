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
from .runtime_config import RuntimeConfig, config_actions

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
        server_provider: Callable[[], dict | None] | None = None,
    ) -> None:
        self._backend = backend
        self._config = config
        self._button = button
        self._status = status
        self._history = history
        self._client_factory = client_factory
        self._crash_backoff = crash_backoff
        # Returns {"url", "token"} for the bound server, or None -> park (no worker).
        # None provider = Phase-2 behaviour: always run, using config's url/token.
        #
        # CONTRACT: this callable is invoked while the supervisor's (non-reentrant)
        # lock is held, so it MUST NOT call back into this Supervisor (rebuild/start/
        # stop) or block — doing so deadlocks the app permanently. Keep it a cheap,
        # side-effect-free read (today: `store.selected()`).
        self._server_provider = server_provider
        self._parked = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()   # signals the CURRENT worker to exit
        self._shutdown = False
        self.current_loop: ConversationLoop | None = None

    # -- building the loop from current config --
    def _current_server(self) -> dict | None:
        """The bound server, or None when parked. Falls back to config (Phase 2)."""
        if self._server_provider is None:
            p = self._config.worker_params()
            return {"url": p["connector_url"], "token": p["connector_token"]}
        return self._server_provider()

    def _build_loop(self) -> ConversationLoop:
        p = self._config.worker_params()
        srv = self._current_server()
        if srv is None:
            # A provider that returns None means "unbound". Never silently substitute
            # the config's server — the design forbids auto-switching. _worker_main
            # checks for this before building, so reaching here is a programming error.
            raise RuntimeError("no server bound; refusing to fall back to the config server")
        client = self._client_factory(
            srv["url"], timeout_s=p["request_timeout_s"], token=srv.get("token", ""),
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
            # Re-check on every iteration (including crash-retries): if the server was
            # unbound while we were backing off, park rather than retrying against
            # some other server.
            if self._server_provider is not None and self._current_server() is None:
                self._parked = True
                self.current_loop = None
                self._status.set("parked")
                log.info("server unbound while running — parking")
                break
            try:
                loop = self._build_loop()
                self.current_loop = loop
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

    def is_parked(self) -> bool:
        return self._parked


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
