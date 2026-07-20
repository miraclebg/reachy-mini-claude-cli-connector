# reachy_app/app.py
"""Reachy Mini **app** entry point — the installable, dashboard-managed form.

This wraps the same conversation loop as the standalone `main.py`, but as a
`ReachyMiniApp` so it can be installed / run / stopped / uninstalled from the
Reachy dashboard (Mac / iPhone app). The framework hands us an already-connected
`ReachyMini` and a `stop_event`, and serves our hold-to-talk page at
`custom_app_url`; we add the button/status/history routes to `self.settings_app`.

Config (connector URL + token) is read from `~/.config/reachy-mini-claude/config.env`
— see INSTALL.md — since the packaged code in site-packages isn't user-editable.

The manager launches this as `python -m reachy_app.app` (per the entry point in
pyproject.toml), which runs the `__main__` block below.
"""
from __future__ import annotations

import sys
import threading
import time

# Bulgarian transcripts get logged; make sure the stream can encode them even under
# a C/POSIX locale (the app subprocess may not inherit a UTF-8 locale).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

from reachy_mini import ReachyMini, ReachyMiniApp
from reachy_mini.utils import create_head_pose

import logging

from .audio import ReachyMiniBackend
from .button_server import ButtonState, History, StatusState
from .connector_client import ConnectorClient
from .movement import MovementPlayer
from .discovery import BeaconListener, verify_server
from .runtime_config import RuntimeConfig, restart_current_app
from .servers import ServerStore, add_server, select_server, servers_view
from .supervisor import Supervisor, apply_config_request


class ReachyClaudeConnectorApp(ReachyMiniApp):
    """Voice conversation with Claude Code on the Mac, spoken through the robot."""

    # Serves static/index.html here and gives us self.settings_app (a FastAPI).
    custom_app_url: str | None = "http://0.0.0.0:8042"
    request_media_backend: str | None = None  # -> "default" (robot mic + speaker)

    def __init__(self) -> None:
        super().__init__()
        # The framework fixes the media backend when it builds ReachyMini, BEFORE run(),
        # from this attribute — so a persisted change only takes effect on the next app
        # start (that is why the UI calls POST /restart-app for it). Leave None ("framework
        # default") unless the user picked a non-default. VERIFY-ON-HARDWARE.
        mb = RuntimeConfig().reachy_media_backend
        if mb and mb != "default":
            self.request_media_backend = mb

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        from fastapi.responses import JSONResponse, Response

        config = RuntimeConfig()
        logging.getLogger().setLevel(config.log_level)

        # Best-effort readiness log; the worker owns the real per-turn client.
        try:
            probe = ConnectorClient(config.connector_url, token=config.connector_token)
            self.logger.info("connector: %s", probe.health())
        except Exception as e:  # keep going; per-turn errors surface in the UI
            self.logger.warning("connector not reachable at %s (%s)", config.connector_url, e)

        button, status, history = ButtonState(), StatusState(), History()

        # Hold-to-talk page (served at "/" by the framework) drives these routes.
        app = self.settings_app
        assert app is not None

        @app.post("/press")
        def _press() -> dict:
            button.press()
            return {"ok": True, "state": "held"}

        @app.post("/release")
        def _release() -> dict:
            button.release()
            return {"ok": True, "state": "released"}

        @app.get("/status")
        def _status() -> dict:
            return {"state": status.get()}

        @app.get("/history")
        def _history() -> Response:
            return Response(content=history.as_json(), media_type="application/json")

        _logger = self.logger  # app logger, captured for the nested driver class

        class _ReachyDriver:
            """Adapts reachy_mini to the MovementPlayer driver protocol."""
            def goto(self, pose: dict, antennas, duration: float) -> None:
                head = create_head_pose(degrees=True, mm=False, **pose)
                kw = {} if antennas is None else {"antennas": list(antennas)}
                reachy_mini.goto_target(head, duration=duration, **kw)

            def rotate_base(self, degrees: float, duration: float) -> None:
                # VERIFY-ON-HARDWARE: confirm the Reachy Mini body-rotation API.
                # Best effort: try a dedicated body call, else approximate with head yaw
                # so the robot still turns (and nothing crashes) until the API is confirmed.
                try:
                    reachy_mini.set_body_rotation(degrees, duration=duration)  # type: ignore[attr-defined]
                except AttributeError:
                    _logger.warning("no body-rotation API yet; approximating with head yaw")
                    reachy_mini.goto_target(
                        create_head_pose(yaw=degrees, degrees=True, mm=False), duration=duration)

        player = MovementPlayer(_ReachyDriver())

        @app.post("/move")
        def _move(payload: dict) -> dict:
            spec = payload.get("spec")
            try:
                frames = player.play(spec)
            except Exception as e:  # never 500 — the connector treats non-200 as "no move"
                self.logger.warning("move failed: %s", e)
                return {"ok": False, "frames": 0}
            return {"ok": True, "frames": frames}

        @app.get("/frame")
        def _frame(hold: int = 0) -> Response:
            # Normally a frame is requested on a bare "look" — rise up to the photo pose
            # with an antenna flourish. But when a MOVE already aimed the head (e.g.
            # "погледни наляво и ми кажи какво виждаш"), hold=1 keeps that pose and just
            # settles + flushes, so the photo is of what the move pointed at.
            def look(antennas):
                return reachy_mini.goto_target(
                    create_head_pose(x=-0.027, y=-0.003, z=0.0, roll=1.5, pitch=-12.5, yaw=0.7),
                    antennas=antennas, duration=0.4)
            try:
                if not hold:
                    look([0.8, -0.8])   # cock the antennas
                    time.sleep(0.32)
                    look([0.0, 0.0])    # snap them straight
                # CRITICAL: settle the head AND let the camera pipeline's latency clear.
                # get_frame_jpeg() returns a buffered frame, so grabbing too soon (or
                # mid-motion) yields a LAGGED frame of the PREVIOUS scene. Then flush
                # several frames and keep the last → the current, stable view.
                time.sleep(1.5)
                jpeg = None
                for _ in range(10):
                    jpeg = reachy_mini.media.get_frame_jpeg()
                    time.sleep(0.08)
            except Exception as e:  # never 500 — the connector treats non-200 as "no frame"
                self.logger.warning("frame capture failed: %s", e)
                return Response(status_code=503, content=b"frame error")
            if not jpeg:
                return Response(status_code=503, content=b"no frame")
            return Response(content=bytes(jpeg), media_type="image/jpeg")

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
            real_id = (info or {}).get("id") if isinstance(info, dict) else None
            if ok and real_id == sel["id"]:
                self.logger.info("bound last-used server %s (%s)", sel.get("name"), sel["url"])
            elif ok:
                # Reachable and the token works, but it is a DIFFERENT connector than the
                # one we saved (e.g. two Macs share a token and swapped IPs). Never bind
                # silently — the same rule select_server enforces.
                self.logger.warning(
                    "last-used server at %s identifies as %s, not %s — parking",
                    sel["url"], real_id, sel["id"])
                # Deliberately not persisted: we park for this boot but keep the saved
                # selection so a later boot can retry it (unlike forget(), which persists).
                store.selected_id = None
            else:
                self.logger.warning("last-used server %s unreachable (%s) — parking",
                                    sel.get("url"), info)
                store.selected_id = None  # park; discovery will offer candidates
        else:
            self.logger.info("no server selected yet — parking until one is picked")

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

        self.logger.info("Reachy Claude connector app running — open %s to talk.", self.custom_app_url)
        supervisor.start()
        try:
            while not stop_event.is_set():
                stop_event.wait(0.2)
        finally:
            supervisor.stop()
            listener.stop()


if __name__ == "__main__":
    app = ReachyClaudeConnectorApp()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
