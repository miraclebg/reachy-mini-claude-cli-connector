# reachy_app/main.py
"""Entry point for the robot-side app.

    # On the Mac (test the whole loop with your laptop mic/speakers):
    python -m reachy_app.main --backend local

    # On the Reachy Mini:
    python -m reachy_app.main --backend reachy

Flags override the .env / environment config (see config.py). The phone
hold-to-talk page is served at http://<this-host>:<BUTTON_PORT>/.
"""
from __future__ import annotations

import argparse
import logging

from .audio import make_backend
from .button_server import ButtonServer
from .config import settings
from .connector_client import ConnectorClient
from .loop import ConversationLoop
from .wakeword import WakeWord


def main() -> int:
    ap = argparse.ArgumentParser(description="Reachy Mini <-> Claude connector, robot side.")
    ap.add_argument("--backend", choices=["local", "reachy"], default=settings.backend,
                    help="local = Mac mic/speaker; reachy = the robot SDK.")
    ap.add_argument("--connector-url", default=settings.connector_url,
                    help="Mac connector server base URL (POST /chat).")
    ap.add_argument("--no-button", action="store_true", help="disable the phone hold-to-talk page.")
    ap.add_argument("--no-wakeword", action="store_true", help="disable the wake word this run.")
    args = ap.parse_args()

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log = logging.getLogger("reachy.main")

    client = ConnectorClient(args.connector_url, timeout_s=settings.request_timeout_s)
    try:
        h = client.health()
        log.info("connector ok: %s", h)
    except Exception as e:
        log.warning("connector not reachable at %s (%s) — will try per-turn.", args.connector_url, e)

    backend = make_backend(
        args.backend,
        sample_rate=settings.sample_rate,
        frame_ms=settings.frame_ms,
        reachy_media_backend=settings.reachy_media_backend,
    )

    button_server = None
    button_state = None
    if settings.button_enabled and not args.no_button:
        button_server = ButtonServer(settings.button_host, settings.button_port)
        button_server.start()
        button_state = button_server.state

    wake = None
    if not args.no_wakeword:
        wake = WakeWord(
            access_key=settings.picovoice_access_key,
            keyword_path=settings.porcupine_keyword_path,
            sensitivity=settings.porcupine_sensitivity,
            want_enabled=settings.wakeword_enabled,
        )

    loop = ConversationLoop(
        backend=backend,
        client=client,
        button=button_state,
        wake=wake,
        on_state=(button_server.status.set if button_server is not None else None),
        on_turn=(button_server.history.add if button_server is not None else None),
        vad_rms_threshold=settings.vad_rms_threshold,
        vad_silence_ms=settings.vad_silence_ms,
        vad_min_speech_ms=settings.vad_min_speech_ms,
        max_utterance_s=settings.max_utterance_s,
    )
    try:
        loop.run_forever()
    finally:
        if button_server is not None:
            button_server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
