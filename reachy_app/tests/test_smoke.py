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
from reachy_app.button_server import ButtonServer, ButtonState, History, StatusState
from reachy_app.config import settings
from reachy_app.connector_client import ConnectorClient
from reachy_app.discovery import BeaconListener, DISCOVERY_PORT, parse_beacon, verify_server
from reachy_app.loop import ConversationLoop
from reachy_app.movement import (
    MovementPlayer, resolve, PRESETS, HEAD_LIMITS, BASE_LIMIT, MAX_KEYFRAMES, MAX_TOTAL_S, MIN_DUR,
)
from reachy_app.runtime_config import RuntimeConfig, config_actions, restart_current_app, LIVE_FIELDS
from reachy_app.servers import ServerStore, public_server
from reachy_app.supervisor import Supervisor
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


def test_shell_tabs() -> None:
    print("shell: page has Talk|Settings nav, keeps hold-to-talk, reads theme param")
    srv = ButtonServer("127.0.0.1", 8097)
    srv.start()
    time.sleep(0.2)
    try:
        page = urllib.request.urlopen("http://127.0.0.1:8097/", timeout=2).read().decode()
        check("hold-to-talk preserved", "Hold" in page and "/press" in page)
        check("has Talk tab panel", 'data-tab="talk"' in page, "")
        check("has Settings tab panel", 'data-tab="settings"' in page, "")
        check("reads the dashboard theme param", '"theme"' in page or "'theme'" in page, "")
    finally:
        srv.stop()


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


# --------------------------- runtime config ---------------------------

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


def test_runtime_config_robust_load_and_types() -> None:
    print("runtime config: non-dict overlay and non-numeric input degrade safely")
    import json as _json
    path = _tmp_runtime_path()
    # a non-dict runtime.json must NOT crash construction — it is ignored
    with open(path, "w", encoding="utf-8") as fh:
        _json.dump(42, fh)
    cfg = RuntimeConfig(path=path)  # must not raise
    check("non-dict overlay ignored, construction succeeds", isinstance(cfg.public_dict(), dict))
    # non-numeric input to a numeric field raises ValueError (not TypeError)
    raised_value = False
    try:
        cfg.apply_updates({"request_timeout_s": None})
    except ValueError:
        raised_value = True
    except TypeError:
        raised_value = False
    check("non-numeric request_timeout_s -> ValueError", raised_value)
    raised_value2 = False
    try:
        cfg.apply_updates({"max_utterance_s": [1]})
    except ValueError:
        raised_value2 = True
    except TypeError:
        raised_value2 = False
    check("non-numeric max_utterance_s -> ValueError", raised_value2)


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


def test_restart_app_teardown_counts_as_success() -> None:
    print("runtime config: a mid-request teardown counts as an accepted restart")
    # On the robot the daemon stops THIS process while our POST is in flight, so the
    # request never completes. That is the restart working, not failing.
    # (VERIFIED-ON-HARDWARE 2026-07-20: external caller got HTTP 200 in ~2s; the
    # in-process caller timed out and used to report a bogus failure.)
    class _Timeout(Exception):
        pass
    _Timeout.__name__ = "ReadTimeout"

    def timeout_post(url, timeout=0):
        raise _Timeout("HTTPConnectionPool: Read timed out.")
    check("timeout -> accepted (True)", restart_current_app(post=timeout_post) is True)

    def reset_post(url, timeout=0):
        raise OSError("Connection aborted, connection reset by peer")
    check("connection reset -> accepted (True)", restart_current_app(post=reset_post) is True)

    # Genuine failures must still report False.
    def refused_post(url, timeout=0):
        raise OSError("[Errno 61] Connection refused")
    check("connection refused -> failure (False)", restart_current_app(post=refused_post) is False)

    class _HTTPError(Exception):
        pass
    _HTTPError.__name__ = "HTTPError"

    class _BadResp:
        def raise_for_status(self): raise _HTTPError("404 Client Error: Not Found")
    def notfound_post(url, timeout=0):
        return _BadResp()
    check("HTTP error status -> failure (False)", restart_current_app(post=notfound_post) is False)


# --------------------------- supervisor ---------------------------

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


def test_supervisor_restarts_on_build_failure() -> None:
    print("supervisor: a loop-build failure sets error and restarts, not a silent thread death")
    cfg = RuntimeConfig(path=_tmp_runtime_path())
    status = StatusState()

    class _FailOnceFactory:
        def __init__(self): self.calls = 0
        def __call__(self, url, timeout_s=180.0, token=""):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("bad config")  # build failure on the first spawn
            return object()

    factory = _FailOnceFactory()
    fake = FakeBackend(pcm_to_wav(np.zeros(1600, dtype=np.float32), 16000))
    sup = Supervisor(backend=fake, config=cfg, button=ButtonState(),
                     status=status, history=History(), client_factory=factory,
                     crash_backoff=(0.02,))
    sup.start()
    try:
        check("error state on build failure", _wait_until(lambda: status.get() == "error"))
        check("rebuilds after a build failure", _wait_until(lambda: factory.calls >= 2))
        check("worker recovers a live loop", _wait_until(lambda: sup.current_loop is not None))
    finally:
        sup.stop()
    check("thread stopped after recovery", not sup._thread_alive())


def test_supervisor_parks_without_a_server() -> None:
    print("supervisor: no bound server -> parked, no worker, status 'parked'")
    cfg = RuntimeConfig(path=_tmp_runtime_path())
    status = StatusState()
    bound = {"v": None}  # nothing bound yet
    fake = FakeBackend(pcm_to_wav(np.zeros(1600, dtype=np.float32), 16000))
    sup = Supervisor(backend=fake, config=cfg, button=ButtonState(),
                     status=status, history=History(),
                     client_factory=_RecordingClientFactory(),
                     server_provider=lambda: bound["v"])
    sup.start()
    try:
        check("parked", sup.is_parked() is True)
        check("no worker thread", not sup._thread_alive())
        check("status published as parked", _wait_until(lambda: status.get() == "parked"), status.get())
        check("no loop built", sup.current_loop is None, str(sup.current_loop))
        # bind a server and rebuild -> worker starts
        bound["v"] = {"url": "http://1.1.1.1:8080", "token": "t"}
        sup.rebuild()
        check("worker runs once bound", _wait_until(lambda: sup.current_loop is not None))
        check("not parked anymore", sup.is_parked() is False)
        # unbind -> parks again, worker torn down
        bound["v"] = None
        sup.rebuild()
        check("parks again on unbind", _wait_until(lambda: not sup._thread_alive()))
        check("is_parked true again", sup.is_parked() is True)
    finally:
        sup.stop()


def test_supervisor_binds_the_provided_server() -> None:
    print("supervisor: the bound server's url+token reach the rebuilt client")
    cfg = RuntimeConfig(path=_tmp_runtime_path())
    factory = _RecordingClientFactory()
    bound = {"v": {"url": "http://5.5.5.5:8080", "token": "tok-5"}}
    fake = FakeBackend(pcm_to_wav(np.zeros(1600, dtype=np.float32), 16000))
    sup = Supervisor(backend=fake, config=cfg, button=ButtonState(),
                     status=StatusState(), history=History(),
                     client_factory=factory, server_provider=lambda: bound["v"])
    sup.start()
    try:
        check("worker built", _wait_until(lambda: sup.current_loop is not None))
        check("client got the bound url", factory.calls[-1][0] == "http://5.5.5.5:8080", str(factory.calls[-1]))
        check("client got the bound token", factory.calls[-1][2] == "tok-5", str(factory.calls[-1]))
        bound["v"] = {"url": "http://6.6.6.6:8080", "token": "tok-6"}
        sup.rebuild()
        check("switch rebinds url", _wait_until(lambda: factory.calls[-1][0] == "http://6.6.6.6:8080"),
              str(factory.calls[-1]))
        check("switch rebinds token", factory.calls[-1][2] == "tok-6", str(factory.calls[-1]))
    finally:
        sup.stop()


def _send_beacon(port, obj) -> None:
    import json as _json, socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        s.sendto(_json.dumps(obj).encode(), ("127.0.0.1", port))
    finally:
        s.close()


def test_parse_beacon_accepts_and_rejects() -> None:
    print("discovery: beacon parsing accepts the contract, rejects junk")
    import json as _json
    good = _json.dumps({"reachy_connector": 1, "id": "i1", "name": "mac",
                        "url": "http://10.0.0.5:8080"}).encode()
    got = parse_beacon(good)
    check("accepts a valid beacon", got is not None and got["id"] == "i1", str(got))
    check("keeps name+url", got and got["name"] == "mac" and got["url"] == "http://10.0.0.5:8080", str(got))
    check("rejects non-JSON", parse_beacon(b"not json") is None)
    check("rejects wrong magic", parse_beacon(_json.dumps({"id": "x", "name": "n", "url": "u"}).encode()) is None)
    check("rejects missing url", parse_beacon(_json.dumps(
        {"reachy_connector": 1, "id": "x", "name": "n"}).encode()) is None)
    check("rejects non-dict", parse_beacon(_json.dumps([1, 2]).encode()) is None)


def test_beacon_listener_collects_and_dedupes() -> None:
    print("discovery: listener collects beacons, dedupes by id, honours clear()")
    port = 48997
    lis = BeaconListener(port=port)
    lis.start()
    try:
        time.sleep(0.3)
        _send_beacon(port, {"reachy_connector": 1, "id": "a", "name": "mac-a", "url": "http://1.1.1.1:8080"})
        _send_beacon(port, {"reachy_connector": 1, "id": "b", "name": "mac-b", "url": "http://2.2.2.2:8080"})
        _send_beacon(port, {"reachy_connector": 1, "id": "a", "name": "mac-a2", "url": "http://1.1.1.9:8080"})
        _send_beacon(port, {"nope": 1})
        check("both servers discovered", _wait_until(lambda: len(lis.discovered()) == 2), str(lis.discovered()))
        by_id = {d["id"]: d for d in lis.discovered()}
        check("dedupes by id (latest wins)", by_id.get("a", {}).get("url") == "http://1.1.1.9:8080", str(by_id))
        check("junk ignored", set(by_id) == {"a", "b"}, str(by_id))
        lis.clear()
        check("clear() empties the list", lis.discovered() == [], str(lis.discovered()))
    finally:
        lis.stop()
    check("listener stops cleanly", not lis.is_alive())


def test_beacon_listener_expires_stale() -> None:
    print("discovery: entries older than the TTL disappear")
    port = 48996
    lis = BeaconListener(port=port, ttl_s=0.6)
    lis.start()
    try:
        time.sleep(0.3)
        _send_beacon(port, {"reachy_connector": 1, "id": "z", "name": "m", "url": "http://3.3.3.3:8080"})
        check("appears", _wait_until(lambda: len(lis.discovered()) == 1), str(lis.discovered()))
        check("expires after ttl", _wait_until(lambda: lis.discovered() == [], timeout=3.0), str(lis.discovered()))
    finally:
        lis.stop()


def test_beacon_listener_survives_busy_port_and_recovers() -> None:
    print("discovery: a busy port degrades, and the SAME listener can retry once it frees")
    import socket as _socket
    port = 48995
    # Hog the port so the listener's bind() fails.
    hog = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    hog.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    hog.bind(("", port))
    lis = BeaconListener(port=port, ttl_s=5.0)
    hog_closed = False
    try:
        lis.start()                       # bind fails; must NOT raise
        check("busy port does not crash", True)
        check("listener thread exits", _wait_until(lambda: not lis.is_alive()), "still alive")
        check("no phantom discoveries", lis.discovered() == [], str(lis.discovered()))

        # Free the port and RETRY THE SAME INSTANCE. This is the actual regression
        # guard: the old start() had recorded a dead thread and would no-op forever.
        hog.close()
        hog_closed = True
        lis.start()
        check("same listener recovers on retry", _wait_until(lambda: lis.is_alive()), "did not restart")
        time.sleep(0.3)
        _send_beacon(port, {"reachy_connector": 1, "id": "r1", "name": "m",
                            "url": "http://4.4.4.4:8080"})
        check("receives after recovery", _wait_until(lambda: len(lis.discovered()) == 1),
              str(lis.discovered()))
    finally:
        lis.stop()
        if not hog_closed:
            hog.close()


def test_verify_server_token_outcomes() -> None:
    print("discovery: /whoami verifies reachability AND the token")
    class _R:
        def __init__(self, code, payload=None): self.status_code = code; self._p = payload or {}
        def json(self): return self._p

    calls = []
    def ok_get(url, headers=None, timeout=0):
        calls.append((url, headers))
        return _R(200, {"id": "i1", "name": "mac", "version": "1"})
    ok, info = verify_server("http://1.1.1.1:8080", "tok", get=ok_get)
    check("200 -> verified", ok is True and info["id"] == "i1", str(info))
    check("hits /whoami", calls and calls[0][0].endswith("/whoami"), str(calls))
    check("sends bearer token", calls and "tok" in str(calls[0][1]), str(calls))

    ok2, err2 = verify_server("http://1.1.1.1:8080", "bad", get=lambda *a, **k: _R(401))
    check("401 -> unauthorized", ok2 is False and err2 == "unauthorized", str(err2))

    ok3, err3 = verify_server("http://1.1.1.1:8080", "t", get=lambda *a, **k: _R(500))
    check("500 -> not verified", ok3 is False, str(err3))

    def boom(*a, **k): raise OSError("no route to host")
    ok4, err4 = verify_server("http://1.1.1.1:8080", "t", get=boom)
    check("unreachable -> not verified", ok4 is False and "no route" in err4.lower(), str(err4))


def _tmp_servers_path() -> str:
    import os, tempfile
    return os.path.join(tempfile.mkdtemp(prefix="reachy-srv-"), "servers.json")


def test_server_store_roundtrip_and_select() -> None:
    print("servers: store persists, selects, and reloads")
    p = _tmp_servers_path()
    s = ServerStore(path=p)
    check("starts empty", s.list_saved() == [] and s.selected() is None)
    s.upsert("id-a", "studio", "http://1.1.1.1:8080", "tok-a")
    s.upsert("id-b", "office", "http://2.2.2.2:8080", "tok-b")
    check("two saved", len(s.list_saved()) == 2, str(s.list_saved()))
    check("select unknown -> False", s.select("nope") is False)
    check("select known -> True", s.select("id-b") is True)
    check("selected is id-b", s.selected() and s.selected()["id"] == "id-b", str(s.selected()))
    s2 = ServerStore(path=p)  # reload from disk
    check("selection persisted", s2.selected_id == "id-b", str(s2.selected_id))
    check("token persisted (on disk, not over HTTP)", s2.get("id-b")["token"] == "tok-b")
    check("upsert updates in place", (s2.upsert("id-a", "studio2", "http://9.9.9.9:8080", "tok-a2"),
                                      len(s2.list_saved()))[1] == 2, str(s2.list_saved()))
    check("updated fields stuck", s2.get("id-a")["url"] == "http://9.9.9.9:8080", str(s2.get("id-a")))
    check("forget removes", s2.forget("id-a") is True and s2.get("id-a") is None)


def test_server_store_file_is_not_world_readable() -> None:
    print("servers: the token-bearing store is written 0600, not world-readable")
    import os as _os
    import stat as _stat
    p = _tmp_servers_path()
    s = ServerStore(path=p)
    s.upsert("id-a", "studio", "http://1.1.1.1:8080", "sup3rs3cret")
    mode = _stat.S_IMODE(_os.stat(p).st_mode)
    check("store file exists", _os.path.exists(p), p)
    check("mode is 0600", mode == 0o600, oct(mode))
    check("not group-readable", not (mode & _stat.S_IRGRP), oct(mode))
    check("not world-readable", not (mode & _stat.S_IROTH), oct(mode))
    # a rewrite (select -> save) must not loosen it again
    s.select("id-a")
    mode2 = _stat.S_IMODE(_os.stat(p).st_mode)
    check("still 0600 after a rewrite", mode2 == 0o600, oct(mode2))


def test_server_store_never_leaks_token() -> None:
    print("servers: the public projection hides the token")
    p = _tmp_servers_path()
    s = ServerStore(path=p)
    s.upsert("id-a", "studio", "http://1.1.1.1:8080", "sup3rs3cret")
    pub = public_server(s.get("id-a"))
    check("has_token flag instead of the token", pub.get("has_token") is True, str(pub))
    check("token value absent", "token" not in pub, str(pub))
    check("secret string nowhere in the projection", "sup3rs3cret" not in str(pub), str(pub))
    check("keeps id/name/url", pub["id"] == "id-a" and pub["name"] == "studio"
          and pub["url"] == "http://1.1.1.1:8080", str(pub))
    s.upsert("id-c", "no-token", "http://3.3.3.3:8080", "")
    check("empty token -> has_token False", public_server(s.get("id-c"))["has_token"] is False)


def test_server_store_survives_corrupt_file() -> None:
    print("servers: a corrupt or non-dict store degrades to empty, not a crash")
    p = _tmp_servers_path()
    import os
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    s = ServerStore(path=p)  # must not raise
    check("corrupt -> empty store", s.list_saved() == [] and s.selected() is None)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("[1,2,3]")
    s2 = ServerStore(path=p)  # non-dict JSON must not raise either
    check("non-dict -> empty store", s2.list_saved() == [])


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
        test_wav_roundtrip, test_endpointer, test_button_server, test_shell_tabs,
        test_settings_panel,
        test_button_auth, test_entry_shim_scrapeable,
        test_runtime_config_persist_roundtrip, test_runtime_config_validation_atomic,
        test_runtime_config_robust_load_and_types,
        test_config_actions_mapping, test_restart_app_posts_daemon,
        test_restart_app_teardown_counts_as_success,
        test_supervisor_rebuild_swaps_params, test_supervisor_stop_is_clean,
        test_supervisor_crash_restarts_and_reports_error,
        test_supervisor_restarts_on_build_failure,
        test_supervisor_parks_without_a_server, test_supervisor_binds_the_provided_server,
        test_parse_beacon_accepts_and_rejects, test_beacon_listener_collects_and_dedupes,
        test_beacon_listener_expires_stale, test_beacon_listener_survives_busy_port_and_recovers,
        test_verify_server_token_outcomes,
        test_server_store_roundtrip_and_select, test_server_store_never_leaks_token,
        test_server_store_file_is_not_world_readable,
        test_server_store_survives_corrupt_file,
        test_apply_config_request_flow,
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
