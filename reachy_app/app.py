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

# Bulgarian transcripts get logged; make sure the stream can encode them even under
# a C/POSIX locale (the app subprocess may not inherit a UTF-8 locale).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

from reachy_mini import ReachyMini, ReachyMiniApp

from .audio import ReachyMiniBackend
from .button_server import ButtonState, History, StatusState
from .config import settings
from .connector_client import ConnectorClient
from .loop import ConversationLoop


class ReachyClaudeConnectorApp(ReachyMiniApp):
    """Voice conversation with Claude Code on the Mac, spoken through the robot."""

    # Serves static/index.html here and gives us self.settings_app (a FastAPI).
    custom_app_url: str | None = "http://0.0.0.0:8042"
    request_media_backend: str | None = None  # -> "default" (robot mic + speaker)

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
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

        @app.get("/frame")
        def _frame() -> Response:
            jpeg = reachy_mini.media.get_frame_jpeg()
            if not jpeg:
                return Response(status_code=503, content=b"no frame")
            return Response(content=bytes(jpeg), media_type="image/jpeg")

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


if __name__ == "__main__":
    app = ReachyClaudeConnectorApp()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
