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
import urllib.error
import urllib.request

import numpy as np

from reachy_app.audio import AudioBackend, pcm_to_wav, wav_to_pcm, wav_duration_s
from reachy_app.button_server import ButtonServer, StatusState
from reachy_app.config import settings
from reachy_app.connector_client import ConnectorClient
from reachy_app.loop import ConversationLoop
from reachy_app.movement import (
    MovementPlayer, resolve, PRESETS, HEAD_LIMITS, BASE_LIMIT, MAX_KEYFRAMES, MAX_TOTAL_S, MIN_DUR,
)
from reachy_app.vad import SilenceEndpointer

SERVER = "http://localhost:8080"
FIXTURE = "/tmp/question.wav"


def _client() -> ConnectorClient:
    return ConnectorClient(SERVER, token=settings.connector_token)

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


def test_button_auth() -> None:
    print("button server auth (token required)")
    srv = ButtonServer("127.0.0.1", 8098, token="s3cret")
    srv.start()
    time.sleep(0.2)
    try:
        base = "http://127.0.0.1:8098"

        def get(path, headers=None):
            req = urllib.request.Request(base + path, headers=headers or {})
            try:
                return urllib.request.urlopen(req, timeout=2).getcode()
            except urllib.error.HTTPError as e:
                return e.code

        check("no token -> 401 on /status", get("/status") == 401)
        check("no token -> 401 on page", get("/") == 401)
        check("query token -> 200", get("/status?token=s3cret") == 200)
        check("header token -> 200", get("/status", {"X-Auth-Token": "s3cret"}) == 200)
        check("wrong token -> 401", get("/status?token=nope") == 401)
        check("/health open without token", get("/health") == 200)
    finally:
        srv.stop()


def test_entry_shim_scrapeable() -> None:
    print("embed: daemon can scrape custom_app_url from the entry shim")
    import os
    import re
    # The daemon reads site_packages/<entry-point-name>/main.py and regex-scrapes it
    # WITHOUT importing. Our entry-point name is `reachy_claude_connector`; mirror the
    # same file from the source tree (…/reachy_app/tests/test_smoke.py -> repo root).
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    shim = os.path.join(root, "reachy_claude_connector", "main.py")
    check("entry shim main.py exists", os.path.exists(shim), shim)
    text = open(shim, encoding="utf-8").read() if os.path.exists(shim) else ""
    # This pattern is copied verbatim from the daemon's _get_custom_app_url_from_file().
    m = re.search(r"""custom_app_url\s*(?::\s*[^=]+)?\s*=\s*["']([^"']+)["']""", text)
    check("custom_app_url is scrapeable", bool(m), "no regex match")
    check("scrapes to :8042", (m.group(1) if m else "") == "http://0.0.0.0:8042",
          m.group(1) if m else "<none>")


class FakeDriver:
    """Records driver calls instead of moving a robot."""
    def __init__(self) -> None:
        self.calls: list = []

    def goto(self, pose, antennas, duration) -> None:
        self.calls.append(("goto", dict(pose), antennas, duration))

    def rotate_base(self, degrees, duration) -> None:
        self.calls.append(("base", degrees, duration))


def _player():
    d = FakeDriver()
    return MovementPlayer(d, sleep=lambda _s: None), d


def test_movement_preset_look_left() -> None:
    print("movement: named preset resolves to a head move")
    p, d = _player()
    n = p.play("look_left")
    gotos = [c for c in d.calls if c[0] == "goto"]
    check("look_left runs one goto", n == 1 and len(gotos) == 1, str(d.calls))
    check("look_left sets +yaw (left)", gotos[0][1].get("yaw", 0) > 0, str(gotos[0]))
    check("duration respects min", gotos[0][3] >= 0.15, str(gotos[0][3]))


def test_movement_clamps_out_of_range() -> None:
    print("movement: out-of-range axis is clamped to the safe window")
    p, d = _player()
    p.play([{"yaw": 999, "dur": 1.0}])
    _, pose, _, _ = [c for c in d.calls if c[0] == "goto"][0]
    check("yaw clamped to max", pose["yaw"] == HEAD_LIMITS["yaw"][1], str(pose))


def test_movement_velocity_floor() -> None:
    print("movement: tiny duration on a big swing is raised by the velocity floor")
    p, d = _player()
    p.play([{"yaw": 40, "dur": 0.01}])
    dur = [c for c in d.calls if c[0] == "goto"][0][3]
    check("duration floored by velocity", dur >= 40.0 / 120.0 - 1e-6, str(dur))


def test_movement_unknown_preset_is_noop() -> None:
    print("movement: unknown preset name does nothing")
    p, d = _player()
    n = p.play("banana")
    check("no frames, no calls", n == 0 and d.calls == [], str(d.calls))


def test_movement_caps_sequence() -> None:
    print("movement: a runaway sequence is capped by count and total duration")
    p, d = _player()
    p.play([{"yaw": 1, "dur": 0.5}] * 100)
    gotos = [c for c in d.calls if c[0] == "goto"]
    total = sum(c[3] for c in gotos)
    check("keyframe count capped", len(gotos) <= MAX_KEYFRAMES, str(len(gotos)))
    check("total duration capped", total <= MAX_TOTAL_S + 1e-6, str(total))


def test_movement_base_keyframe() -> None:
    print("movement: rotate preset drives the base axis")
    p, d = _player()
    p.play("rotate_left")
    bases = [c for c in d.calls if c[0] == "base"]
    check("one base call", len(bases) == 1, str(d.calls))
    check("base +deg (left) within limit", 0 < bases[0][1] <= BASE_LIMIT[1], str(bases[0]))


def test_movement_velocity_floor_across_calls() -> None:
    print("movement: velocity floor accounts for the pose already held")
    p, d = _player()
    p.play("look_left")   # ends held at yaw=+35
    p.play("look_right")  # from +35 to -35 is a 70deg swing, not 35
    gotos = [c for c in d.calls if c[0] == "goto"]
    dur = gotos[-1][3]
    expected_floor = 70.0 / 120.0
    check("second goto duration floored for the true (70deg) swing",
          dur >= expected_floor - 1e-6, f"dur={dur} expected>={expected_floor}")


def test_movement_tolerates_bad_values() -> None:
    print("movement: non-numeric keyframe values are dropped, not raised")
    p, d = _player()
    n = p.play([{"yaw": "left", "pitch": 10, "dur": "soon"}])
    gotos = [c for c in d.calls if c[0] == "goto"]
    check("does not raise and runs one frame", n == 1 and len(gotos) == 1, str(d.calls))
    if gotos:
        _, pose, _, dur = gotos[0]
        check("bad yaw dropped", "yaw" not in pose, str(pose))
        check("good pitch kept and clamped", pose.get("pitch") == 10, str(pose))
        check("bad dur falls back to a sane floor", dur >= MIN_DUR - 1e-6, str(dur))


def server_up() -> bool:
    try:
        _client().health()
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

    client = _client()
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
    for t in (
        test_wav_roundtrip, test_endpointer, test_button_server, test_button_auth,
        test_entry_shim_scrapeable,
        test_movement_preset_look_left, test_movement_clamps_out_of_range,
        test_movement_velocity_floor, test_movement_unknown_preset_is_noop,
        test_movement_caps_sequence, test_movement_base_keyframe,
        test_movement_velocity_floor_across_calls, test_movement_tolerates_bad_values,
        test_full_turn,
    ):
        t()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
