# Voice-Directed Robot Movement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator direct the robot's body by voice — named commands (look/rotate/tilt/nod/flap) plus Claude-improvised movement ("потанцувай") — composed into the existing see-and-speak pipeline.

**Architecture:** Claude emits movement markers in its reply (reusing the `[LOOK]` mechanism). One generic, safety-clamped keyframe **player on the robot** executes every movement; named routines are just stored keyframe presets fed to that same player. The **server** parses the markers and orchestrates the ordered sequence move → frame → re-ask → TTS.

**Tech Stack:** Python 3.12, FastAPI (both services), `reachy_mini` SDK (robot-only, imported lazily), `requests`, pytest (server), custom test runner (`reachy_app`).

## Global Constraints

- **Two venvs, two dep sets:** server code runs in `server/.venv`; robot code in `reachy_app/.venv`. Never import `reachy_mini` at module top in testable code — import it lazily (mirror `reachy_app/audio.py`).
- **Silent by default:** a movement command produces **no** spoken text unless the operator asked for information back. Do exactly what's asked, nothing extra.
- **Bulgarian, Cyrillic** for anything spoken (existing `VOICE_SYSTEM_PROMPT` rule — do not weaken it).
- **Marker formats (exact):** named routine `[MOVE <name>]`; improvised keyframes `[MOVE]<json-array>[/MOVE]`. Markers must never reach TTS.
- **Safety limits live in ONE place** — the robot-side player. Values below are `VERIFY-ON-HARDWARE` placeholders; keep them as named constants so they are tuned in one edit:
  - Head (degrees): `yaw ±40`, `pitch ±30`, `roll ±25`. Translation (metres): `x,y ±0.03`, `z ±0.02`. Base (degrees, absolute): `±90`. Antennas: `±1.0`.
  - Velocity floor: `120 deg/s`, `0.08 m/s`; per-keyframe min duration `0.15 s`.
  - Sequence caps: `MAX_KEYFRAMES = 24`, `MAX_TOTAL_S = 8.0`.
- **Sign conventions (`VERIFY-ON-HARDWARE`):** negative `pitch` = look **up** (matches vision code `pitch=-12.5` gazing up and `enter_listening` `pitch=-8`); positive `yaw` = look **left**; positive `base` = rotate **left**. Fix signs on the hardware pass if reversed.
- **Hold vs. return** is encoded in the preset itself: orientation presets end at their offset (held); gesture presets end at neutral (returned).

## File Structure

**New:**
- `reachy_app/movement.py` — safety-clamped keyframe player + named presets. Pure logic; the SDK is injected via a tiny driver, so this file is fully unit-testable on the Mac.
- `server/movement.py` — marker parsing (`parse_move`, `wants_move`) + `post_move` HTTP call. Pure/`requests` only, mirrors `server/vision.py`.
- `server/test_movement.py` — pytest for the marker parser.

**Modified:**
- `reachy_app/app.py` — build a real driver from `reachy_mini`, add `POST /move`, add `?hold=1` to `/frame` (capture from the held pose after a move).
- `server/main.py` — orchestrate move → frame → re-ask; strip markers before TTS; new config.
- `server/vision.py` — `fetch_frame(..., hold=False)` → `GET /frame?hold=1`.
- `server/claude_client.py` — motor palette + decision rules appended to `VOICE_SYSTEM_PROMPT`.
- `server/config.py` — `movement_enabled`, `move_timeout_s`.
- `reachy_app/tests/test_smoke.py` — player tests + register in `main()`.
- `server/test_claude_prompt.py` — guard test for the movement prompt block.
- `README.md` — the user guide (command table, decision rules, safety, config).

---

## Task 1: Robot-side keyframe player (`reachy_app/movement.py`)

The heart of the feature: presets, clamping, velocity floor, sequence caps, and a player that drives an injected `driver`. No robot needed to test — a `FakeDriver` records calls.

**Files:**
- Create: `reachy_app/movement.py`
- Test: `reachy_app/tests/test_smoke.py` (add `FakeDriver` + tests, register in `main()`)

**Interfaces:**
- Produces:
  - `PRESETS: dict[str, list[dict]]` — named routine → keyframe list.
  - `resolve(spec: str | list) -> list[dict]` — clamped, capped keyframes (`[]` for unknown/invalid).
  - `class MovementPlayer` with `__init__(self, driver, sleep=time.sleep)` and `play(self, spec) -> int` (returns frames executed).
  - Driver protocol the robot must satisfy: `goto(pose: dict, antennas: list | None, duration: float)` and `rotate_base(degrees: float, duration: float)`.
  - Constants: `HEAD_LIMITS`, `TRANS_LIMITS`, `BASE_LIMIT`, `ANT_LIMIT`, `MAX_KEYFRAMES`, `MAX_TOTAL_S`, `POSE_AXES`.

- [ ] **Step 1: Write the failing tests**

Add to `reachy_app/tests/test_smoke.py` (near the other test functions, after `FakeBackend`):

```python
from reachy_app.movement import (
    MovementPlayer, resolve, PRESETS, HEAD_LIMITS, BASE_LIMIT, MAX_KEYFRAMES, MAX_TOTAL_S,
)


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
```

- [ ] **Step 2: Register the new tests and run to verify they fail**

Edit the `main()` runner tuple in `reachy_app/tests/test_smoke.py`:

```python
def main() -> int:
    for t in (
        test_wav_roundtrip, test_endpointer, test_button_server, test_button_auth,
        test_movement_preset_look_left, test_movement_clamps_out_of_range,
        test_movement_velocity_floor, test_movement_unknown_preset_is_noop,
        test_movement_caps_sequence, test_movement_base_keyframe,
        test_full_turn,
    ):
        t()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0
```

Run (from repo root, reachy_app venv):
```bash
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector
reachy_app/.venv/bin/python -m reachy_app.tests.test_smoke
```
Expected: FAIL — `ModuleNotFoundError: No module named 'reachy_app.movement'`.

- [ ] **Step 3: Implement `reachy_app/movement.py`**

```python
# reachy_app/movement.py
"""Generic, safety-clamped movement player for the Reachy Mini.

Every voice-directed movement — named routine or Claude-improvised — is a list of
keyframes run through ONE player. A keyframe is a dict with any subset of:
    x, y, z            head translation (metres)
    roll, pitch, yaw   head orientation (degrees)
    base               body/turntable rotation (degrees, absolute)
    ant                antennas [left, right]
    dur                seconds for this keyframe (floored by the velocity guard)

All safety lives here (range clamp, velocity floor, sequence caps): Claude may ask
for anything; the player caps it. The reachy_mini SDK is injected via a `driver`
(see ReachyDriver in app.py), so this module stays importable and unit-testable on
the Mac with no robot.
"""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("reachy.movement")

# --- safety limits (VERIFY-ON-HARDWARE: tune on the real robot) ---
HEAD_LIMITS = {"yaw": (-40.0, 40.0), "pitch": (-30.0, 30.0), "roll": (-25.0, 25.0)}
TRANS_LIMITS = {"x": (-0.03, 0.03), "y": (-0.03, 0.03), "z": (-0.02, 0.02)}
BASE_LIMIT = (-90.0, 90.0)     # absolute; guards cable wind-up on the wireless unit
ANT_LIMIT = (-1.0, 1.0)
MAX_SPEED_DEG = 120.0          # deg/s -> velocity floor for rotational axes
MAX_SPEED_M = 0.08             # m/s   -> velocity floor for translation
MIN_DUR = 0.15                 # s, per-keyframe floor
MAX_KEYFRAMES = 24
MAX_TOTAL_S = 8.0

POSE_AXES = ("x", "y", "z", "roll", "pitch", "yaw")
_NEUTRAL = {k: 0.0 for k in (*POSE_AXES, "base")}

# --- named routines (signs are VERIFY-ON-HARDWARE; see plan Global Constraints) ---
# Orientation presets end at an offset (held); gesture presets end at neutral (return).
PRESETS: dict[str, list[dict]] = {
    "look_left":    [{"yaw": 35, "dur": 0.5}],
    "look_right":   [{"yaw": -35, "dur": 0.5}],
    "look_up":      [{"pitch": -25, "dur": 0.5}],
    "look_down":    [{"pitch": 25, "dur": 0.5}],
    "tilt_left":    [{"roll": 20, "dur": 0.5}],
    "tilt_right":   [{"roll": -20, "dur": 0.5}],
    "rotate_left":  [{"base": 60, "dur": 1.0}],
    "rotate_right": [{"base": -60, "dur": 1.0}],
    "nod":   [{"pitch": 15, "dur": 0.35}, {"pitch": -10, "dur": 0.35},
              {"pitch": 15, "dur": 0.35}, {"pitch": 0, "dur": 0.35}],
    "shake": [{"yaw": 25, "dur": 0.3}, {"yaw": -25, "dur": 0.3},
              {"yaw": 25, "dur": 0.3}, {"yaw": 0, "dur": 0.35}],
    "flap_left":  [{"ant": [1, 0], "dur": 0.25}, {"ant": [0, 0], "dur": 0.25},
                   {"ant": [1, 0], "dur": 0.25}, {"ant": [0, 0], "dur": 0.25}],
    "flap_right": [{"ant": [0, 1], "dur": 0.25}, {"ant": [0, 0], "dur": 0.25},
                   {"ant": [0, 1], "dur": 0.25}, {"ant": [0, 0], "dur": 0.25}],
    "flap_both":  [{"ant": [1, 1], "dur": 0.25}, {"ant": [0, 0], "dur": 0.25},
                   {"ant": [1, 1], "dur": 0.25}, {"ant": [0, 0], "dur": 0.25}],
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _velocity_floor(prev: dict, new: dict) -> float:
    deg = max((abs(new.get(a, 0.0) - prev.get(a, 0.0)) for a in ("yaw", "pitch", "roll", "base")), default=0.0)
    m = max((abs(new.get(a, 0.0) - prev.get(a, 0.0)) for a in ("x", "y", "z")), default=0.0)
    return max(deg / MAX_SPEED_DEG, m / MAX_SPEED_M)


def _clamp_keyframe(f: dict, prev: dict) -> tuple[dict, dict]:
    """Clamp one keyframe against the safe limits; return (clamped, new_pose_state)."""
    cf: dict = {}
    new_prev = dict(prev)
    for ax, (lo, hi) in HEAD_LIMITS.items():
        if ax in f:
            cf[ax] = new_prev[ax] = _clamp(float(f[ax]), lo, hi)
    for ax, (lo, hi) in TRANS_LIMITS.items():
        if ax in f:
            cf[ax] = new_prev[ax] = _clamp(float(f[ax]), lo, hi)
    if "base" in f:
        cf["base"] = new_prev["base"] = _clamp(float(f["base"]), *BASE_LIMIT)
    if "ant" in f and isinstance(f["ant"], (list, tuple)) and len(f["ant"]) == 2:
        cf["ant"] = [_clamp(float(f["ant"][0]), *ANT_LIMIT), _clamp(float(f["ant"][1]), *ANT_LIMIT)]
    requested = float(f.get("dur", MIN_DUR))
    cf["dur"] = max(requested, _velocity_floor(prev, new_prev), MIN_DUR)
    return cf, new_prev


def resolve(spec) -> list[dict]:
    """A preset name or a keyframe list -> clamped, capped keyframes ([] if invalid)."""
    if isinstance(spec, str):
        frames = [dict(f) for f in PRESETS.get(spec.strip(), [])]
    elif isinstance(spec, list):
        frames = [dict(f) for f in spec if isinstance(f, dict)]
    else:
        frames = []
    frames = frames[:MAX_KEYFRAMES]
    out: list[dict] = []
    prev = dict(_NEUTRAL)
    total = 0.0
    for f in frames:
        cf, prev = _clamp_keyframe(f, prev)
        if total + cf["dur"] > MAX_TOTAL_S:
            break
        total += cf["dur"]
        out.append(cf)
    return out


class MovementPlayer:
    """Runs a keyframe sequence via an injected driver.

    driver must provide:
        goto(pose: dict, antennas: list | None, duration: float)
        rotate_base(degrees: float, duration: float)
    `sleep` is injectable so tests run instantly.
    """

    def __init__(self, driver, sleep=time.sleep) -> None:
        self.driver = driver
        self._sleep = sleep

    def play(self, spec) -> int:
        frames = resolve(spec)
        for kf in frames:
            if "base" in kf:
                self.driver.rotate_base(kf["base"], kf["dur"])
            pose = {k: kf[k] for k in POSE_AXES if k in kf}
            ant = kf.get("ant")
            if pose or ant is not None:
                self.driver.goto(pose, ant, kf["dur"])
            self._sleep(kf["dur"])
        log.info("played %d keyframe(s) for spec=%r", len(frames), spec if isinstance(spec, str) else "<keyframes>")
        return len(frames)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector
reachy_app/.venv/bin/python -m reachy_app.tests.test_smoke
```
Expected: all movement tests PASS; `test_full_turn` still passes if the server is up (it's fine if that one is skipped/failed only due to no server — the movement tests must pass).

- [ ] **Step 5: Commit**

```bash
git add reachy_app/movement.py reachy_app/tests/test_smoke.py
git commit -m "movement: robot-side keyframe player with presets, clamping, caps"
```

---

## Task 2: Robot `/move` endpoint + `/frame?hold` (`reachy_app/app.py`)

Thin robot-only glue: build a real driver from `reachy_mini`, expose `POST /move`, and let `/frame` skip the fixed photo pose when a move already positioned the head. This file imports `reachy_mini` at module top, so it is **not** Mac-unit-testable; the logic it delegates to (Task 1) is. Verification is a syntax/import-shape check now and a hardware pass later.

**Files:**
- Modify: `reachy_app/app.py` (imports; add `/move`; extend `/frame`)

**Interfaces:**
- Consumes: `MovementPlayer` from Task 1 (`goto`/`rotate_base` driver protocol).
- Produces: `POST /move` accepting `{"spec": <name|keyframe-list>}` → `{"ok": bool, "frames": int}`; `GET /frame?hold=1` capturing from the current held pose.

- [ ] **Step 1: Add the movement import**

At the top of `reachy_app/app.py`, alongside the existing `from .audio import ReachyMiniBackend`:

```python
from .movement import MovementPlayer
```

- [ ] **Step 2: Build the driver + player and add `POST /move`**

Inside `run(...)`, after the `@app.get("/history")` route and before `@app.get("/frame")`, add:

```python
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
                    self.logger.warning("no body-rotation API yet; approximating with head yaw")
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
```

- [ ] **Step 3: Add `?hold` to `/frame`**

Change the `/frame` handler signature and guard the repositioning block so a preceding move's pose is kept:

```python
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
```

- [ ] **Step 4: Verify the file parses and the movement import resolves**

`reachy_mini` isn't importable on the Mac, so check syntax and that the new module import is valid, without importing `app.py`:

```bash
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector
reachy_app/.venv/bin/python -c "import ast; ast.parse(open('reachy_app/app.py').read()); print('app.py parses')"
reachy_app/.venv/bin/python -c "from reachy_app.movement import MovementPlayer; print('player importable')"
```
Expected: both print success. (Full `/move` behaviour is a hardware-pass check — see Task 8.)

- [ ] **Step 5: Commit**

```bash
git add reachy_app/app.py
git commit -m "movement: robot /move endpoint + /frame hold-pose capture"
```

---

## Task 3: Server-side marker parsing (`server/movement.py`)

Pure functions + one `requests` call, mirroring `server/vision.py`. Tested with pytest, no robot.

**Files:**
- Create: `server/movement.py`
- Test: `server/test_movement.py`

**Interfaces:**
- Produces:
  - `parse_move(text: str) -> tuple[object | None, str]` — returns `(spec, cleaned_text)`. `spec` is a preset name (`str`), a keyframe `list`, or `None`. `cleaned_text` has all `[MOVE …]` / `[MOVE]…[/MOVE]` markers removed (a co-occurring `[LOOK]` is left intact).
  - `wants_move(text: str) -> bool`
  - `post_move(base_url: str, spec, timeout_s: float = 8.0) -> bool` — `POST {base_url}/move` with `{"spec": spec}`.

- [ ] **Step 1: Write the failing tests**

Create `server/test_movement.py`:

```python
from movement import parse_move, wants_move


def test_parse_named_routine():
    spec, cleaned = parse_move("[MOVE look_left]")
    assert spec == "look_left"
    assert cleaned == ""


def test_parse_keyframes_json():
    spec, cleaned = parse_move('[MOVE][{"yaw": 20, "dur": 0.3}, {"yaw": 0, "dur": 0.4}][/MOVE]')
    assert isinstance(spec, list) and spec[0]["yaw"] == 20
    assert cleaned == ""


def test_parse_none_when_no_marker():
    spec, cleaned = parse_move("здрасти, как си?")
    assert spec is None
    assert cleaned == "здрасти, как си?"


def test_parse_leaves_look_marker():
    spec, cleaned = parse_move("[MOVE look_left][LOOK]")
    assert spec == "look_left"
    assert cleaned == "[LOOK]"


def test_parse_strips_marker_keeps_speech():
    spec, cleaned = parse_move("Хайде! [MOVE nod]")
    assert spec == "nod"
    assert cleaned == "Хайде!"


def test_parse_bad_json_returns_none_spec():
    spec, cleaned = parse_move("[MOVE][not json[/MOVE]")
    assert spec is None
    assert cleaned == ""


def test_wants_move():
    assert wants_move("[MOVE nod]")
    assert wants_move('[MOVE][{"yaw":1}][/MOVE]')
    assert not wants_move("no markers here")
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector/server
.venv/bin/python -m pytest test_movement.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'movement'`.

- [ ] **Step 3: Implement `server/movement.py`**

```python
# server/movement.py
"""Movement helpers for the connector.

Parse the movement markers Claude emits and forward the resolved spec to the robot's
/move endpoint. Kept separate from main.py so the parser is unit-testable without
FastAPI or a robot (mirrors vision.py).

Markers:
  * named routine   -> [MOVE look_left]
  * improvised move -> [MOVE][{"yaw":20,"dur":0.3}, ...][/MOVE]
A co-occurring [LOOK] is left untouched (main.py handles vision).
"""
from __future__ import annotations

import json
import logging
import re

import requests

log = logging.getLogger("connector.movement")

# Keyframe block first (its inner JSON contains ']'), then the bareword named form.
_MOVE_BLOCK_RE = re.compile(r"\[move\](.*?)\[/move\]", re.IGNORECASE | re.DOTALL)
_MOVE_NAME_RE = re.compile(r"\[move\s+([a-z_]+)\]", re.IGNORECASE)


def parse_move(text: str) -> tuple[object | None, str]:
    """Return (spec, cleaned_text). spec is a preset name, a keyframe list, or None."""
    t = text or ""
    block = _MOVE_BLOCK_RE.search(t)
    if block:
        cleaned = _MOVE_BLOCK_RE.sub("", t).strip()
        raw = block.group(1).strip()
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("bad [MOVE] keyframe json: %r", raw)
            return None, cleaned
        return (spec if isinstance(spec, list) else None), cleaned
    named = _MOVE_NAME_RE.search(t)
    if named:
        cleaned = _MOVE_NAME_RE.sub("", t).strip()
        return named.group(1).lower(), cleaned
    return None, t


def wants_move(text: str) -> bool:
    t = text or ""
    return bool(_MOVE_BLOCK_RE.search(t) or _MOVE_NAME_RE.search(t))


def post_move(base_url: str, spec, timeout_s: float = 8.0) -> bool:
    """POST {base_url}/move with {"spec": spec}. True on HTTP 200, else False."""
    url = base_url.rstrip("/") + "/move"
    try:
        r = requests.post(url, json={"spec": spec}, timeout=timeout_s)
    except requests.RequestException as e:
        log.warning("move POST failed (%s): %s", url, e)
        return False
    if r.status_code != 200:
        log.warning("move POST %s -> %s", url, r.status_code)
        return False
    return True
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector/server
.venv/bin/python -m pytest test_movement.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add server/movement.py server/test_movement.py
git commit -m "movement: server-side marker parser + /move client"
```

---

## Task 4: Teach Claude the motor palette (`server/claude_client.py`)

Append a movement block to `VOICE_SYSTEM_PROMPT` so Claude knows the named routines, the keyframe format, and the decision rules (silent by default; speak only when asked for info; combine with `[LOOK]`).

**Files:**
- Modify: `server/claude_client.py:57-80` (extend `VOICE_SYSTEM_PROMPT`)
- Test: `server/test_claude_prompt.py` (add a guard test)

**Interfaces:**
- Consumes: nothing new. Produces: an enriched `VOICE_SYSTEM_PROMPT` string.

- [ ] **Step 1: Write the failing guard test**

Add to `server/test_claude_prompt.py`:

```python
from claude_client import VOICE_SYSTEM_PROMPT


def test_prompt_documents_movement():
    p = VOICE_SYSTEM_PROMPT
    assert "[MOVE" in p              # marker taught
    assert "look_left" in p          # a named routine listed
    assert "[/MOVE]" in p            # keyframe block form taught
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector/server
.venv/bin/python -m pytest test_claude_prompt.py::test_prompt_documents_movement -v
```
Expected: FAIL — `[MOVE` not in prompt.

- [ ] **Step 3: Extend `VOICE_SYSTEM_PROMPT`**

In `server/claude_client.py`, append this string to the existing `VOICE_SYSTEM_PROMPT` (add it as a final concatenated segment before the closing `)`):

```python
    " You also have a BODY you can move on request. To move, put a marker in your reply. "
    "Named moves (use the exact token): [MOVE look_left] [MOVE look_right] [MOVE look_up] "
    "[MOVE look_down] [MOVE tilt_left] [MOVE tilt_right] [MOVE rotate_left] "
    "[MOVE rotate_right] [MOVE nod] [MOVE shake] [MOVE flap_left] [MOVE flap_right] "
    "[MOVE flap_both]. 'Look' turns the head; 'rotate' turns the whole body; 'tilt' leans "
    "the head sideways. For anything without a named move — 'потанцувай', 'изненадай ме', "
    "a playful reaction — improvise your own motion with a keyframe block: "
    "[MOVE][{\"yaw\":20,\"ant\":[1,-1],\"dur\":0.3},{\"yaw\":-20,\"dur\":0.3},{\"yaw\":0,\"dur\":0.4}][/MOVE]. "
    "Each keyframe may set any of yaw/pitch/roll (head degrees, negative pitch = up), "
    "base (body-turn degrees), ant ([left,right]) and dur (seconds); omit what you don't move. "
    "RULES: By default a movement command is SILENT — emit ONLY the marker, no spoken text. "
    "Speak a reply ONLY when the user also asked for information back. If the user asks you "
    "to move AND to tell them what you see (e.g. 'погледни наляво и ми кажи какво виждаш'), "
    "emit the move marker AND [LOOK] together — the move happens first, then a photo is "
    "taken from that pose and you answer. Do exactly what was asked and nothing extra. "
    "Never say the words MOVE or LOOK to the user, and never read a marker aloud."
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector/server
.venv/bin/python -m pytest test_claude_prompt.py -v
```
Expected: all pass (existing 4 + the new one).

- [ ] **Step 5: Commit**

```bash
git add server/claude_client.py server/test_claude_prompt.py
git commit -m "movement: teach Claude the motor palette + decision rules"
```

---

## Task 5: Wire orchestration into `/chat` (`server/main.py` + `server/config.py` + `server/vision.py`)

Parse the move marker after Claude's reply, POST it to the robot, then (if `[LOOK]` co-occurs) capture from the held pose and re-ask. Strip markers before TTS. This lands inside the big `/chat` handler which needs STT/TTS/Claude, so its end-to-end check is `smoke_test.py` against a running server (matching how vision is verified); the pure decision logic it calls is already unit-tested in Task 3.

**Files:**
- Modify: `server/config.py` (add settings)
- Modify: `server/vision.py` (`fetch_frame` gains `hold`)
- Modify: `server/main.py` (imports; `grab_frame(hold=…)`; orchestration block)

**Interfaces:**
- Consumes: `parse_move`, `wants_move`, `post_move` (Task 3); `wants_look` (existing); `fetch_frame(..., hold=…)`.
- Produces: no new exports — behaviour change only.

- [ ] **Step 1: Add config settings**

In `server/config.py`, right after the vision settings block (after `robot_camera_url`):

```python
    # --- movement (voice-directed body motion) ---
    movement_enabled: bool = _as_bool(os.environ.get("MOVEMENT_ENABLED", "true"))
    move_timeout_s: float = float(os.environ.get("MOVE_TIMEOUT_S", "12"))  # > player MAX_TOTAL_S
```

- [ ] **Step 2: Add `hold` to `fetch_frame`**

In `server/vision.py`, replace the `fetch_frame` signature and URL build:

```python
def fetch_frame(base_url: str, timeout_s: float = 4.0, hold: bool = False) -> bytes | None:
    """GET {base_url}/frame -> JPEG bytes, or None on any failure.

    hold=True adds ?hold=1 so the robot captures from its CURRENT (already-moved) pose
    instead of rising to the default photo pose.
    """
    url = base_url.rstrip("/") + "/frame" + ("?hold=1" if hold else "")
    try:
        r = requests.get(url, timeout=timeout_s)
    except requests.RequestException as e:
        log.warning("frame fetch failed (%s): %s", url, e)
        return None
    if r.status_code != 200 or not r.content:
        log.warning("frame fetch %s -> %s (%d bytes)", url, r.status_code, len(r.content or b""))
        return None
    return r.content
```

- [ ] **Step 3: Import the movement helpers in `main.py`**

In `server/main.py`, next to `from vision import fetch_frame, transcript_wants_vision`:

```python
from movement import parse_move, wants_move, post_move
```

- [ ] **Step 4: Give `grab_frame` a `hold` parameter**

In `server/main.py`, change the `grab_frame` definition (around line 168) to thread `hold` into `fetch_frame`:

```python
        def grab_frame(hold=False):
            """Fetch a frame from the robot, save it for Claude; return the rel path or None."""
            frame = fetch_frame(robot_base, settings.camera_timeout_s, hold=hold) if robot_base else None
            if not frame:
                return None
            try:
                with open(img_file, "wb") as fh:
                    fh.write(frame)
            except OSError as e:
                log.warning("could not save camera frame: %s", e)
                return None
            if settings.debug_vision_dir:  # keep a timestamped copy for diagnosis
                try:
                    os.makedirs(settings.debug_vision_dir, exist_ok=True)
                    import time as _t
                    with open(os.path.join(settings.debug_vision_dir, _t.strftime("frame-%H%M%S.jpg")), "wb") as dbg:
                        dbg.write(frame)
                except OSError:
                    pass
            return "camera_view.jpg"  # relative to Claude's cwd (claude_working_dir)
```

- [ ] **Step 5: Add the orchestration block**

In `server/main.py`, replace the `else:` branch that asks Claude (currently lines ~194-212) with the version below. It adds movement parsing before the existing look handling and passes `hold` into the frame grab:

```python
        # 3) Ask Claude (empty transcript → skip it).
        if not transcript:
            reply = settings.msg_no_speech
        else:
            try:
                reply = claude.ask(transcript, image_path=image_path, camera_failed=camera_failed)

                # 3a) Movement: Claude may direct a body move (named preset or keyframes).
                #     Do it FIRST so a co-requested photo is taken from the moved pose.
                moved = False
                if settings.movement_enabled:
                    move_spec, reply = parse_move(reply)
                    if move_spec is not None and robot_base:
                        moved = post_move(robot_base, move_spec, settings.move_timeout_s)

                # 3b) Claude-decided vision (possibly after a move): grab a frame and re-ask.
                if settings.vision_enabled and image_path is None and not camera_failed and wants_look(reply):
                    log.info("Claude requested a look — fetching frame (hold=%s) and re-asking", moved)
                    image_path = grab_frame(hold=moved)
                    if image_path:
                        reply = claude.ask("Снимката, която поиска, е готова.", image_path=image_path)
                    else:
                        reply = claude.ask("Камерата не върна изображение.", camera_failed=True)
                    # A move requested in the describe turn is rare but strip+run it too.
                    if settings.movement_enabled:
                        move_spec2, reply = parse_move(reply)
                        if move_spec2 is not None and robot_base:
                            post_move(robot_base, move_spec2, settings.move_timeout_s)
            except ClaudeError as e:
                log.error("Claude error: %s", e)
                reply = settings.msg_error
            # never speak the internal markers if they slip through
            reply = re.sub(r"\[look\]", "", reply, flags=re.IGNORECASE).strip() or settings.msg_error
```

- [ ] **Step 6: Sanity-check imports and syntax**

```bash
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector/server
.venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('main.py parses')"
.venv/bin/python -m pytest test_movement.py test_vision.py test_claude_prompt.py -v
```
Expected: `main.py parses`; all unit tests pass.

- [ ] **Step 7: End-to-end smoke (server running, no robot needed)**

Start the server in one shell, then exercise the text loop. Movement markers with no robot connected degrade gracefully (parsed + stripped; `post_move` returns False; reply still spoken/silent correctly).

```bash
# shell A:
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector/server
source .venv/bin/activate && uvicorn main:app --host 0.0.0.0 --port 8080
# shell B:
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector/server
python smoke_test.py --reset --text "погледни наляво"        # expect near-empty/short spoken reply
python smoke_test.py --text "потанцувай"                      # expect no marker text leaks into reply
python smoke_test.py --text "как се казваш?"                  # unaffected normal reply
```
Expected: replies never contain `[MOVE` / `[/MOVE]` / `[LOOK]`; a bare movement command yields little or no speech; a normal question is unchanged.

- [ ] **Step 8: Commit**

```bash
git add server/config.py server/vision.py server/main.py
git commit -m "movement: orchestrate move -> frame -> re-ask in /chat"
```

---

## Task 6: User guide in the README

The explicit deliverable: document commands, the decide-to-move-vs-speak rules, safety, and config as a user guide.

**Files:**
- Modify: `README.md` (add a "Voice-directed movement" section; note `/move` in the HTTP API and the feature in Features)

**Interfaces:** none (documentation).

- [ ] **Step 1: Add the feature to the Features list**

Under `## Features` in `README.md`, add a bullet:

```markdown
- **Voice-directed movement** — tell Reachy to look, rotate, tilt, nod, or flap its
  antennas, or ask for something open-ended ("потанцувай") and Claude improvises the
  motion. Movements compose with vision: "погледни наляво и ми кажи какво виждаш" turns,
  photographs the new view, and describes it.
```

- [ ] **Step 2: Add the user-guide section**

Add this section (place it after `## The phone page`, before `## HTTP API`):

```markdown
## Voice-directed movement

Reachy can move its body on command. Two kinds of request, one mechanism: **named
routines** for the common moves, and **Claude-improvised** motion for anything else.
All movement is safety-clamped on the robot.

### Commands

| Say (BG) | What it does |
|---|---|
| погледни наляво / надясно | turn the head to look left / right (and hold) |
| погледни нагоре / надолу | tip the head up / down (and hold) |
| завърти се наляво / надясно | rotate the whole body left / right (and hold) |
| наклони глава наляво / надясно | lean the head ear-to-shoulder (and hold) |
| кимни | nod "yes" and return |
| поклати глава | shake "no" and return |
| размахай антени (лявата / дясната / двете) | flap antennas and return |
| потанцувай / изненадай ме / … | Claude improvises a short motion |

### How Reachy decides to move, speak, or both

- A bare movement command is **silent** — Reachy just moves, no chit-chat.
- It **speaks** only when you also asked for information back. "Погледни наляво **и ми
  кажи какво виждаш**" turns left, takes a photo *from that pose*, and describes it.
- For requests with no named routine, Claude composes its own keyframes on the fly, so
  "dance" is genuine improvisation rather than one canned routine.
- Orientation moves (look/rotate/tilt) **hold** their pose; gesture moves (nod/shake/
  flap/dance) **return** to neutral. Reachy re-centers on the next command or turn.

### Safety

Every movement runs through one clamped player on the robot: each axis is capped to a
tested range, a velocity floor prevents jerky high-speed swings, and a sequence can't
exceed ~8 seconds. Claude can *ask* for anything; the robot caps it.

### Settings (`server/.env`)

| Var | Default | Meaning |
|---|---|---|
| `MOVEMENT_ENABLED` | `true` | Master switch for voice-directed movement. |
| `MOVE_TIMEOUT_S` | `12` | How long the connector waits for a movement to finish. |

Movement reuses the robot app's address (same host/port as the camera), so no extra URL
is needed. With no robot connected, movement markers are parsed and stripped and the
reply still behaves correctly — nothing moves.
```

- [ ] **Step 3: Note `/move` in the HTTP API section**

Under `### Robot app (`reachy_app/`, port 8081)` (the robot-app routes list) in `README.md`, add:

```markdown
- `POST /move` — run a movement. Body `{"spec": "look_left"}` (named routine) or
  `{"spec": [{"yaw": 20, "dur": 0.3}, …]}` (keyframes). Safety-clamped on the robot.
- `GET /frame?hold=1` — capture from the current (already-moved) pose instead of rising
  to the default photo pose.
```

- [ ] **Step 4: Verify the tables render (no broken markdown)**

```bash
cd /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector
grep -n "Voice-directed movement" README.md
```
Expected: matches in both the Features bullet and the section heading.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: voice-directed movement user guide"
```

---

## Task 7: Hardware verification pass (Reachy connected)

Not code — the checklist to run once Reachy is back online, resolving the `VERIFY-ON-HARDWARE` items. Keep the fixes to the single constants/driver seams noted below.

**Files:** likely `reachy_app/movement.py` (limits/signs), `reachy_app/app.py` (`rotate_base`).

- [ ] **Step 1: Deploy and smoke each named routine**

Run each command via the phone page and confirm the physical motion matches the label:
`погледни наляво/надясно/нагоре/надолу`, `наклони глава…`, `завърти се…`, `кимни`,
`поклати глава`, `размахай антени…`.

- [ ] **Step 2: Fix sign conventions if reversed**

If left/right, up/down, or rotate direction is inverted, flip the sign in the relevant
`PRESETS` entry in `reachy_app/movement.py` (yaw/pitch/base). No other change needed.

- [ ] **Step 3: Confirm the base-rotation API**

Check whether `завърти се` turned the **body**. If it only turned the head, replace the
`set_body_rotation` call in `_ReachyDriver.rotate_base` (`reachy_app/app.py`) with the
real SDK body-rotation method (inspect `dir(reachy_mini)` on the robot), keeping the
head-yaw fallback.

- [ ] **Step 4: Tune ranges/speed for comfort**

If any move strains a motor or looks too fast/slow, adjust `HEAD_LIMITS` / `TRANS_LIMITS`
/ `BASE_LIMIT` / `MAX_SPEED_DEG` / `MAX_SPEED_M` in `reachy_app/movement.py`. If
`goto_target` turns out to **block** for its full `duration`, drop the `self._sleep(...)`
in `MovementPlayer.play` (it would otherwise double each keyframe's time).

- [ ] **Step 5: Verify the composed case**

Say "погледни наляво и ми кажи какво виждаш" and confirm: head turns left → photo is of
the left view (not forward) → Reachy describes it → nothing else is said.

- [ ] **Step 6: Commit any tuning**

```bash
git add reachy_app/movement.py reachy_app/app.py
git commit -m "movement: hardware-tuned limits, signs, and base-rotation call"
```

---

## Self-Review

**Spec coverage:**
- Hybrid control model (named + keyframes, one player) → Task 1 (`PRESETS` + `resolve` + `MovementPlayer`).
- Silent-by-default / speak-only-when-info-requested / do-nothing-extra → Task 4 (prompt rules) + Task 5 (markers stripped, no synthetic speech added).
- Compose move → capture-from-held-pose → describe → report → Task 2 (`/frame?hold`) + Task 5 (orchestration order + `grab_frame(hold=moved)`).
- Hold vs. return per-move → encoded in `PRESETS` (Task 1), documented Task 6.
- Motor axis catalog (yaw=look, roll=tilt, base=rotate) → `PRESETS` (Task 1), prompt (Task 4), README table (Task 6).
- Marker protocol → Task 3 parser + Task 4 prompt.
- Safety (range clamp, velocity floor, sequence caps, base guard) → Task 1 constants + `_clamp_keyframe`/`resolve`.
- Server orchestration + config + graceful no-robot degrade → Task 5.
- README user guide → Task 6.
- Verify-on-hardware (base API, per-axis ranges/signs) → Task 7.

**Placeholder scan:** No "TBD/TODO/implement later" in code steps. The two `VERIFY-ON-HARDWARE` items (base-rotation SDK method, tuned limits/signs) are concrete, working code with an honest fallback (head-yaw approximation) and an explicit Task 7 to confirm — sanctioned by the approved spec, not open-ended.

**Type consistency:** `MovementPlayer(driver, sleep=…)`, driver `goto(pose, antennas, duration)` / `rotate_base(degrees, duration)`, `resolve(spec) -> list[dict]`, `play(spec) -> int`, `parse_move(text) -> (spec, cleaned)`, `wants_move`, `post_move(base_url, spec, timeout_s)`, `fetch_frame(base_url, timeout_s, hold)` — names/signatures match across Tasks 1, 2, 3, 5. Config `movement_enabled` / `move_timeout_s` used exactly as defined.
