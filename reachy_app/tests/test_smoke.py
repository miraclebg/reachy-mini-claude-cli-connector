#!/usr/bin/env python3
"""Smoke tests for the Mac-runnable parts of reachy_app.

Covers everything that doesn't need the robot or a live mic:
  * WAV helpers round-trip
  * SilenceEndpointer (speech -> silence -> end)
  * ButtonServer HTTP endpoints + ButtonState edges
  * A full ConversationLoop turn through a FakeBackend against the real Mac server

The last one needs the connector server running on :8080 (it's skipped with a clear
message if not). Run from the repo root:

    source reachy_app/.venv/bin/activate
    python -m reachy_app.tests.test_smoke
"""
from __future__ import annotations

import sys
import time
import urllib.request

import numpy as np

from reachy_app.audio import AudioBackend, pcm_to_wav, wav_to_pcm, wav_duration_s
from reachy_app.button_server import ButtonServer, StatusState
from reachy_app.connector_client import ConnectorClient
from reachy_app.loop import ConversationLoop
from reachy_app.vad import SilenceEndpointer

SERVER = "http://localhost:8080"
FIXTURE = "/tmp/question.wav"

_passed = 0
_failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}  {extra}")


# --------------------------- unit ---------------------------

def test_wav_roundtrip() -> None:
    print("wav helpers")
    sr = 16000
    tone = (0.3 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)).astype(np.float32)
    wav = pcm_to_wav(tone, sr)
    back, back_sr = wav_to_pcm(wav)
    check("samplerate preserved", back_sr == sr, f"{back_sr}")
    check("length preserved", abs(len(back) - len(tone)) <= 1, f"{len(back)} vs {len(tone)}")
    check("duration ~1s", abs(wav_duration_s(wav) - 1.0) < 0.01)
    check("amplitude preserved", abs(float(np.max(np.abs(back))) - 0.3) < 0.01)


def test_endpointer() -> None:
    print("silence endpointer")
    sr = 16000
    ep = SilenceEndpointer(sr, rms_threshold=0.02, silence_ms=300, min_speech_ms=200)
    blk = sr // 100  # 10 ms blocks
    speech = (0.2 * np.ones(blk)).astype(np.float32)
    silence = np.zeros(blk, dtype=np.float32)

    fired_during_speech = any(ep.feed(speech) for _ in range(30))  # 300 ms speech
    check("does not fire during speech", not fired_during_speech)

    fired = False
    for _ in range(40):  # up to 400 ms silence
        if ep.feed(silence):
            fired = True
            break
    check("fires after trailing silence", fired)

    ep2 = SilenceEndpointer(sr, rms_threshold=0.02, silence_ms=300, min_speech_ms=200)
    only_silence = any(ep2.feed(silence) for _ in range(100))
    check("never fires on pure silence (no speech yet)", not only_silence)


# --------------------------- button server ---------------------------

def test_button_server() -> None:
    print("button server")
    srv = ButtonServer("127.0.0.1", 8099)
    srv.start()
    time.sleep(0.2)
    try:
        base = "http://127.0.0.1:8099"
        page = urllib.request.urlopen(base + "/", timeout=2).read().decode()
        check("serves hold-to-talk page", "Hold" in page and "/press" in page)

        check("starts un-held", not srv.state.is_held())
        urllib.request.urlopen(urllib.request.Request(base + "/press", method="POST"), timeout=2).read()
        check("press -> held", srv.state.is_held())
        check("press edge consumed once", srv.state.take_press() and not srv.state.take_press())

        urllib.request.urlopen(urllib.request.Request(base + "/release", method="POST"), timeout=2).read()
        check("release -> not held", not srv.state.is_held())

        # status endpoint reflects StatusState
        import json as _json
        s0 = _json.loads(urllib.request.urlopen(base + "/status", timeout=2).read())
        check("status starts idle", s0.get("state") == "idle", str(s0))
        srv.status.set("speaking")
        s1 = _json.loads(urllib.request.urlopen(base + "/status", timeout=2).read())
        check("status reflects updates", s1.get("state") == "speaking", str(s1))

        # history endpoint (incl. Cyrillic round-trip through JSON)
        h0 = _json.loads(urllib.request.urlopen(base + "/history", timeout=2).read())
        check("history starts empty", h0.get("turns") == [], str(h0))
        srv.history.add("здравей", "привет")
        h1 = _json.loads(urllib.request.urlopen(base + "/history", timeout=2).read())
        ok = len(h1["turns"]) == 1 and h1["turns"][0]["you"] == "здравей" \
            and h1["turns"][0]["reply"] == "привет"
        check("history records a turn (Cyrillic ok)", ok, str(h1))
    finally:
        srv.stop()


# --------------------------- full turn (needs server) ---------------------------

class FakeBackend(AudioBackend):
    """Stands in for mic+speaker+robot: returns a canned utterance, captures playback."""

    def __init__(self, canned_wav: bytes) -> None:
        self.canned = canned_wav
        _, sr = wav_to_pcm(canned_wav)
        self.input_samplerate = sr
        self.played: bytes | None = None
        self.states: list[str] = []

    def record(self, should_stop, max_seconds):
        should_stop(np.zeros(160, dtype=np.float32))  # exercise the callback path
        return self.canned

    def play_wav(self, wav_bytes):
        self.played = wav_bytes

    def enter_idle(self):      self.states.append("idle")
    def enter_listening(self): self.states.append("listening")
    def enter_thinking(self):  self.states.append("thinking")
    def enter_speaking(self):  self.states.append("speaking")


def server_up() -> bool:
    try:
        ConnectorClient(SERVER).health()
        return True
    except Exception:
        return False


def test_full_turn() -> None:
    print("full loop turn (FakeBackend -> real server)")
    if not server_up():
        print("  ⏭  SKIPPED — connector server not running on :8080")
        return
    try:
        with open(FIXTURE, "rb") as fh:
            canned = fh.read()
    except OSError:
        print(f"  ⏭  SKIPPED — fixture {FIXTURE} missing")
        return

    client = ConnectorClient(SERVER)
    client.reset()
    fake = FakeBackend(canned)
    states: list[str] = []
    turns: list[tuple] = []
    loop = ConversationLoop(backend=fake, client=client, button=None, wake=None,
                            on_state=states.append, on_turn=lambda y, r: turns.append((y, r)))

    reply = loop.do_turn("wake")
    check("got a reply", reply is not None)
    if reply:
        check("transcribed the utterance", len(reply.transcript) > 0, repr(reply.transcript))
        check("claude answered", len(reply.reply_text) > 0, repr(reply.reply_text))
        check("reply audio played back", fake.played is not None and len(fake.played) > 1000)
        print(f"     heard : {reply.transcript!r}")
        print(f"     reply : {reply.reply_text!r}")
    order = fake.states
    check("gesture order listening->thinking->speaking->idle",
          order == ["listening", "thinking", "speaking", "idle"], str(order))
    check("published states listening->thinking->speaking->idle",
          states == ["listening", "thinking", "speaking", "idle"], str(states))
    check("published one turn to history", len(turns) == 1, str(turns))
    if turns and reply:
        check("turn carries transcript+reply",
              turns[0][0] == reply.transcript and turns[0][1] == reply.reply_text)


def main() -> int:
    for t in (test_wav_roundtrip, test_endpointer, test_button_server, test_full_turn):
        t()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
