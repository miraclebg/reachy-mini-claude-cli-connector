# reachy_app/loop.py
"""The turn-taking state machine.

    IDLE ──(wake word | phone press)──► LISTENING
    LISTENING ──(VAD end-of-speech | phone release)──► SENDING
    SENDING ──(reply audio arrives)──► SPEAKING
    SPEAKING ──(playback done)──► IDLE

Two triggers, one loop. The phone button uses release as the end-of-speech signal
(no VAD needed); the wake word uses the RMS endpointer. The wake word is muted
during SENDING/SPEAKING so Reachy's own voice can't re-trigger it.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from .audio import AudioBackend
from .button_server import ButtonState
from .connector_client import ChatReply, ConnectorClient, ConnectorError
from .vad import SilenceEndpointer
from .wakeword import WakeWord

log = logging.getLogger("reachy.loop")


class ConversationLoop:
    def __init__(
        self,
        *,
        backend: AudioBackend,
        client: ConnectorClient,
        button: ButtonState | None = None,
        wake: WakeWord | None = None,
        on_state: Callable[[str], None] | None = None,
        on_turn: Callable[[str, str], None] | None = None,
        vad_rms_threshold: float = 0.015,
        vad_silence_ms: int = 800,
        vad_min_speech_ms: int = 300,
        max_utterance_s: float = 15.0,
    ) -> None:
        self.backend = backend
        self.client = client
        self.button = button
        self.wake = wake
        self.on_state = on_state
        self.on_turn = on_turn
        self.max_utterance_s = max_utterance_s
        self._vad_cfg = dict(
            rms_threshold=vad_rms_threshold,
            silence_ms=vad_silence_ms,
            min_speech_ms=vad_min_speech_ms,
        )
        self._running = False

    # --- trigger polling ---
    def _poll_trigger(self) -> str | None:
        if self.button is not None and self.button.take_press():
            return "button"
        if self.wake is not None and self.wake.take_detection():
            return "wake"
        return None

    def _set_state(self, state: str) -> None:
        """Publish the current phase (drives the phone status indicator)."""
        if self.on_state is not None:
            self.on_state(state)

    def _make_should_stop(self, mode: str):
        if mode == "button":
            # release IS end-of-speech
            assert self.button is not None
            return lambda _block: not self.button.is_held()
        # wake path: trailing-silence endpointer
        endpointer = SilenceEndpointer(self.backend.input_samplerate, **self._vad_cfg)
        return endpointer.feed

    # --- one full turn (also the unit-test seam) ---
    def do_turn(self, mode: str) -> ChatReply | None:
        log.info("── turn start (%s) ──", mode)
        self._set_state("listening")
        self.backend.enter_listening()
        should_stop = self._make_should_stop(mode)
        wav = self.backend.record(should_stop, self.max_utterance_s)

        self._set_state("thinking")
        self.backend.enter_thinking()
        if self.wake is not None:
            self.wake.pause()  # don't hear ourselves think/speak

        reply: ChatReply | None = None
        try:
            reply = self.client.chat(wav)
        except ConnectorError as e:
            log.error("connector error: %s", e)
        finally:
            if self.wake is not None:
                self.wake.resume()

        if reply is not None:
            if self.on_turn is not None and (reply.transcript or reply.reply_text):
                self.on_turn(reply.transcript, reply.reply_text)
            self._set_state("speaking")
            self.backend.enter_speaking()
            self.backend.play_wav(reply.audio_wav)
            self._set_state("idle")
        else:
            # leave the indicator on "error" until the next interaction
            self._set_state("error")
        self.backend.enter_idle()
        return reply

    # --- run until interrupted ---
    def run_forever(self) -> None:
        if self.button is None and (self.wake is None or not self.wake.enabled):
            log.warning("no active triggers (button disabled and wake word off) — nothing to do.")
        if self.wake is not None:
            self.wake.start()
        self._running = True
        self._set_state("idle")
        self.backend.enter_idle()
        log.info("ready. waiting for a trigger…")
        try:
            while self._running:
                mode = self._poll_trigger()
                if mode is None:
                    time.sleep(0.02)
                    continue
                self.do_turn(mode)
                log.info("ready. waiting for a trigger…")
        except KeyboardInterrupt:
            log.info("interrupted, shutting down.")
        finally:
            self.stop()

    def stop(self) -> None:
        self._running = False
        if self.wake is not None:
            self.wake.stop()
        self.backend.close()
