# reachy_app/audio.py
"""Audio + motion backends behind one small interface.

The rest of the app only knows `AudioBackend`: capture an utterance, play a reply,
and show state through gestures. Two implementations:

  * LocalAudioBackend  — Mac mic + speakers (sounddevice). No robot. Gestures are
    logged, not moved. This is what makes the whole loop testable on the Mac.
  * ReachyMiniBackend  — the real robot via the reachy_mini SDK. Speaking uses
    Piper audio + `enable_wobbling()` for a talking motion; antennas/head show state.

Both record 16 kHz-ish mono and hand back WAV bytes; the Mac server's whisper step
resamples internally, so the exact input rate only needs to be recorded correctly
in the WAV header.
"""
from __future__ import annotations

import io
import logging
import os
import tempfile
import time
import wave
from typing import Callable

import numpy as np

log = logging.getLogger("reachy.audio")

# should_stop is called after each captured block with the block's float32 samples;
# return True to end the utterance.
ShouldStop = Callable[[np.ndarray], bool]


# --------------------------- WAV helpers ---------------------------

def pcm_to_wav(samples: np.ndarray, samplerate: int) -> bytes:
    """Mono float32 [-1, 1] (or int16) -> WAV bytes (16-bit PCM)."""
    if samples.dtype != np.int16:
        clipped = np.clip(samples, -1.0, 1.0)
        samples = (clipped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def wav_to_pcm(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """WAV bytes -> (mono float32 [-1, 1], samplerate). Downmixes stereo."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        raw = w.readframes(n)
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def wav_duration_s(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return w.getnframes() / float(w.getframerate())


# --------------------------- base ---------------------------

class AudioBackend:
    """Interface. Gesture hooks default to no-ops (overridden by the robot)."""

    input_samplerate: int = 16000

    def record(self, should_stop: ShouldStop, max_seconds: float) -> bytes:
        raise NotImplementedError

    def play_wav(self, wav_bytes: bytes) -> None:
        raise NotImplementedError

    # state-feedback gestures
    def enter_idle(self) -> None: ...
    def enter_listening(self) -> None: ...
    def enter_thinking(self) -> None: ...
    def enter_speaking(self) -> None: ...

    def close(self) -> None: ...


# --------------------------- Mac (sounddevice) ---------------------------

class LocalAudioBackend(AudioBackend):
    """Mac mic + speakers via sounddevice. sounddevice is imported lazily so the
    package still imports (for tests / robot use) when PortAudio isn't installed."""

    def __init__(self, sample_rate: int = 16000, frame_ms: int = 30) -> None:
        import sounddevice as sd  # lazy: needs PortAudio (brew install portaudio)
        self._sd = sd
        self.input_samplerate = sample_rate
        self._frame = max(1, int(sample_rate * frame_ms / 1000))
        log.info("LocalAudioBackend ready (sr=%d, frame=%d samples)", sample_rate, self._frame)

    def record(self, should_stop: ShouldStop, max_seconds: float) -> bytes:
        blocks: list[np.ndarray] = []
        deadline = time.time() + max_seconds
        with self._sd.InputStream(
            samplerate=self.input_samplerate, channels=1, dtype="float32", blocksize=self._frame
        ) as stream:
            while True:
                block, _overflowed = stream.read(self._frame)
                mono = block[:, 0].copy()
                blocks.append(mono)
                if should_stop(mono) or time.time() >= deadline:
                    break
        audio = np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)
        return pcm_to_wav(audio, self.input_samplerate)

    def play_wav(self, wav_bytes: bytes) -> None:
        data, sr = wav_to_pcm(wav_bytes)
        self._sd.play(data, sr)
        self._sd.wait()

    def enter_idle(self) -> None:
        log.info("gesture: idle")

    def enter_listening(self) -> None:
        log.info("gesture: listening (antennas perk)")

    def enter_thinking(self) -> None:
        log.info("gesture: thinking")

    def enter_speaking(self) -> None:
        log.info("gesture: speaking (wobble)")


# --------------------------- Robot (reachy_mini SDK) ---------------------------

class ReachyMiniBackend(AudioBackend):
    """The real robot. Imports reachy_mini lazily (only present on the robot).

    Audio API (from the Pollen examples):
      record : media.start_recording() -> poll media.get_audio_sample() -> stop
      play   : media.play_sound(path) with enable_wobbling() for a talking motion
    Gestures move the antennas/head via goto_target + create_head_pose.
    """

    # antenna angles (radians): perked up for listening, neutral for idle.
    _ANT_NEUTRAL = (0.0, 0.0)
    _ANT_PERK = (0.5, 0.5)

    def __init__(self, media_backend: str = "default") -> None:
        from reachy_mini import ReachyMini  # lazy: robot-only dependency
        self._create_head_pose = self._import_head_pose()
        # Enter the SDK context manager manually; close() exits it.
        self._cm = ReachyMini(log_level="INFO", media_backend=media_backend)
        self.mini = self._cm.__enter__()
        self.input_samplerate = self.mini.media.get_input_audio_samplerate()
        log.info("ReachyMiniBackend ready (input sr=%d)", self.input_samplerate)

    @staticmethod
    def _import_head_pose():
        from reachy_mini.utils import create_head_pose
        return create_head_pose

    # -- audio --
    def record(self, should_stop: ShouldStop, max_seconds: float) -> bytes:
        m = self.mini.media
        m.start_recording()
        # wait briefly for the mic to warm up
        t0 = time.time()
        while m.get_audio_sample() is None and time.time() - t0 < 1.0:
            time.sleep(0.005)

        samples: list[np.ndarray] = []
        deadline = time.time() + max_seconds
        try:
            while time.time() < deadline:
                s = m.get_audio_sample()
                if s is None:
                    time.sleep(0.005)
                    continue
                s = np.asarray(s, dtype=np.float32).reshape(-1)
                samples.append(s)
                if should_stop(s):
                    break
        finally:
            m.stop_recording()
        audio = np.concatenate(samples) if samples else np.zeros(0, dtype=np.float32)
        return pcm_to_wav(audio, self.input_samplerate)

    def play_wav(self, wav_bytes: bytes) -> None:
        # play_sound wants a file; write a temp WAV, wobble while it plays.
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with open(path, "wb") as fh:
                fh.write(wav_bytes)
            self.mini.enable_wobbling()
            self.mini.media.play_sound(path)
            time.sleep(wav_duration_s(wav_bytes) + 0.3)  # play_sound is non-blocking
        finally:
            try:
                self.mini.disable_wobbling()
            except Exception:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- gestures --
    def _head(self, *, pitch=0.0, roll=0.0, yaw=0.0):
        return self._create_head_pose(roll=roll, pitch=pitch, yaw=yaw, degrees=True, mm=False)

    def enter_idle(self) -> None:
        self.mini.goto_target(self._head(), antennas=list(self._ANT_NEUTRAL), duration=0.6)

    def enter_listening(self) -> None:
        # perk antennas + a small attentive look-up
        self.mini.goto_target(self._head(pitch=-8), antennas=list(self._ANT_PERK), duration=0.4)

    def enter_thinking(self) -> None:
        # a pondering tilt while we wait on the Mac
        self.mini.goto_target(self._head(roll=10, pitch=6), antennas=list(self._ANT_NEUTRAL), duration=0.5)

    def enter_speaking(self) -> None:
        # face forward; wobbling in play_wav supplies the talking motion
        self.mini.goto_target(self._head(), antennas=list(self._ANT_NEUTRAL), duration=0.3)

    def close(self) -> None:
        try:
            self._cm.__exit__(None, None, None)
        except Exception:
            pass


# --------------------------- factory ---------------------------

def make_backend(kind: str, *, sample_rate: int, frame_ms: int, reachy_media_backend: str) -> AudioBackend:
    kind = (kind or "local").lower()
    if kind == "local":
        return LocalAudioBackend(sample_rate=sample_rate, frame_ms=frame_ms)
    if kind == "reachy":
        return ReachyMiniBackend(media_backend=reachy_media_backend)
    raise ValueError(f"unknown backend {kind!r} (use 'local' or 'reachy')")
