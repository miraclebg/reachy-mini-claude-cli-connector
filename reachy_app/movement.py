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
