# reachy_app/wakeword.py
"""'Hey Reachy' wake word via Picovoice Porcupine.

Entirely optional. It stays a no-op (`enabled == False`) unless BOTH a Picovoice
access key and a keyword .ppn path are configured AND `pvporcupine` is installed —
so the app runs today on the phone-button path with zero external dependencies, and
the wake word lights up the moment you drop in the key + keyword file.

Runs a background thread that reads mic frames and sets a one-shot detection edge
the main loop consumes with `take_detection()`. `pause()`/`resume()` mute it during
SENDING/SPEAKING so Reachy's own voice can't trigger it (the echo gotcha).

On the robot you'd ideally feed the robot mic into Porcupine; v1 uses sounddevice,
which is fine for Mac testing. Marked as a follow-up for on-robot integration.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("reachy.wakeword")


class WakeWord:
    def __init__(
        self,
        *,
        access_key: str,
        keyword_path: str,
        sensitivity: float = 0.5,
        want_enabled: bool = True,
    ) -> None:
        self.enabled = False
        self._detected = threading.Event()
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._porcupine = None

        if not want_enabled:
            return
        if not access_key or not keyword_path:
            log.info("wake word off: set PICOVOICE_ACCESS_KEY + PORCUPINE_KEYWORD_PATH to enable.")
            return
        try:
            import pvporcupine
            self._porcupine = pvporcupine.create(
                access_key=access_key,
                keyword_paths=[keyword_path],
                sensitivities=[sensitivity],
            )
            self.enabled = True
            log.info("wake word ready (keyword=%s)", keyword_path)
        except ImportError:
            log.warning("wake word off: `pip install pvporcupine` to enable.")
        except Exception as e:  # bad key, bad .ppn, etc.
            log.warning("wake word off: Porcupine init failed: %s", e)

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import numpy as np
        import sounddevice as sd

        p = self._porcupine
        with sd.RawInputStream(
            samplerate=p.sample_rate, channels=1, dtype="int16", blocksize=p.frame_length
        ) as stream:
            log.info("listening for wake word…")
            while not self._stop.is_set():
                data, _ = stream.read(p.frame_length)
                if self._paused.is_set():
                    continue
                pcm = np.frombuffer(data, dtype=np.int16)
                if p.process(pcm) >= 0:
                    log.info("wake word detected")
                    self._detected.set()

    def take_detection(self) -> bool:
        """True once per detection (consumes the edge)."""
        if self._detected.is_set():
            self._detected.clear()
            return True
        return False

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._detected.clear()  # drop anything heard while paused
        self._paused.clear()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._porcupine:
            self._porcupine.delete()
