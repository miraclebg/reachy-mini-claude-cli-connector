# Voice-directed robot movement — design

**Date:** 2026-07-18
**Status:** Approved (design). Hardware not connected at write time — two items flagged
`VERIFY-ON-HARDWARE` below.
**Related:** builds directly on the vision feature (`2026-07-18-reachy-vision-design.md`)
and reuses its Claude-emits-a-marker mechanism.

## Goal

Let the operator direct the robot's body by voice. Two classes of request, one
mechanism:

- **Known commands** — "погледни наляво", "завърти се надясно", "кимни", "размахай
  антени" — map to hand-tuned **named routines**.
- **Anything else** — "потанцувай", "изненадай ме", "престори се на срамежлив" — Claude
  **improvises** a movement. It already knows what the motors can do; we don't want to
  enumerate every possible movement.

## Core decision: hybrid control model, one player

The pivotal fork was *what Claude emits*. Decision: **named routines for the requested
commands, Claude-authored keyframes for everything else** — and both go through a
**single generic keyframe player on the robot**. A named routine is just a stored
keyframe preset; an improvised dance is a keyframe sequence Claude wrote. Same executor,
same safety clamps, one code path. New named move = add a preset, no new plumbing.

Rejected alternatives:
- *Named library only* — exactly the "list every movement" approach we wanted to avoid;
  "dance" could only ever be a canned routine.
- *Raw keyframes only* — loses the reliability/safety of blessed, hand-tuned presets for
  the common commands.

## Behaviour rules (locked)

1. **Silent by default.** A movement command just moves — no spoken filler, no "готово".
2. **Speak only when information was requested.** "Погледни наляво **и ми кажи какво
   виждаш**" speaks because the operator asked to be told something.
3. **Turns can chain, in order.** "look left and describe" = move left → capture a frame
   **from the looking-left pose** → analyze → report. The move must finish and the camera
   must shoot from the moved position before Claude describes.
4. **Do exactly what's asked, nothing more.**
5. **Hold vs. return is per-move:**
   - *Orientation moves hold* — "погледни наляво" turns and **stays** left; it only
     re-centers on the next command / next turn.
   - *Gesture moves return* — nod / shake / flap / dance run and settle back to neutral;
     the gesture *is* the round trip.

## Motor axes & named-routine catalog

The head is a 6-DOF platform (`create_head_pose(x, y, z, roll, pitch, yaw)`), plus 2
antennas, plus a **base rotation** motor (the whole body turns — a new axis the current
code does not touch).

| Command (BG) | Physical motion | Axis | Hold/return |
|---|---|---|---|
| погледни наляво / надясно | head gazes L/R | head **yaw** | hold |
| погледни нагоре / надолу | head tips up/down | head **pitch** | hold |
| завърти се наляво / надясно | **whole body** turns | **base rotation** | hold |
| наклони глава наляво / надясно | head leans ear-to-shoulder | head **roll** | hold |
| кимни | nod "yes" | pitch oscillation | return |
| поклати глава | shake "no" | yaw oscillation | return |
| размахай антени (ляво / дясно / двете) | antenna flap | antennas | return |
| (creative / unknown) | Claude improvises | any | return |

Note the disambiguation: **yaw = "look left/right"**, **roll = "tilt/наклони"**, **base
rotation = "завърти се"** — three distinct commands that must not collide.

## Marker protocol

Reuses the `[LOOK]` mechanism. Claude's reply may contain movement markers plus optional
spoken text:

- Named routine: `[MOVE look_left]`
- Improvised keyframes: `[MOVE [{"yaw":-20,"ant":[1,-1],"dur":0.3}, {"yaw":0,"dur":0.4}]]`
- Combined with vision: `[MOVE look_left][LOOK]`
- Any non-marker text is the spoken reply. **No text ⇒ silent.**

A keyframe is a partial pose — any subset of `{x, y, z, roll, pitch, yaw, base, ant:[L,R],
dur}`; unspecified axes stay where they are. The server strips every marker before TTS
(as it already does for `[look]`), so a marker never gets spoken.

## How Claude decides (system-prompt rules)

- Known command → emit the matching **named routine** marker.
- Unknown / creative request → **author keyframes**.
- Request asks for information back ("…и ми кажи какво виждаш") → `[MOVE …][LOOK]`, then
  speak the resulting description.
- Otherwise → move **silently**, add nothing.

Trigger is **Claude-driven** (no keyword fast-path): deciding rotate-vs-not, composing a
dance, and detecting "move *and* describe" all require understanding the utterance.

## End-to-end flow

```
Robot records "погледни наляво и ми кажи какво виждаш" → POST /chat
Mac:  STT → Claude
      reply = [MOVE look_left][LOOK]           (no spoken text)
      server parses markers, orchestrates IN ORDER:
        1. POST robot /move {look_left}   → robot turns, settles, HOLDS the pose
        2. GET  robot /frame              → captures FROM the held looking-left pose
        3. re-ask Claude with the image   → "виждам бюро с два монитора…"
        4. TTS that description
   → return WAV (+ transcript/reply headers)
Robot plays the WAV

Bare "потанцувай"     → reply [MOVE [keyframes…]] → POST /move, dance, silent
Bare "погледни наляво" → reply [MOVE look_left]    → POST /move, turn & hold, silent
```

## Safety (single choke-point: the robot-side player)

- **Range clamp** — every axis clamped to a tested safe window. Claude may *ask* for
  anything; the player caps it.
- **Velocity floor** — each keyframe `dur` forced ≥ (distance ÷ max-speed) so a large
  swing can't be requested in a tiny duration and jerk the motors.
- **Sequence limits** — max keyframe count and max total duration (~8 s) so a "dance"
  can't run away.
- **Base-rotation guard** — cap absolute/cumulative base angle to avoid winding up
  (cable twist on the wireless unit).
- **Always settles safe** — gesture moves end at neutral; orientation moves hold their
  (already-clamped) pose.

All limits live in the robot-side player (last line of defence, where the SDK is); the
server may pre-validate but the robot is authoritative.

## Components touched

- `reachy_app/movement.py` *(new)* — the generic clamped keyframe player + named presets.
  The one place motors are driven for this feature. Imports `reachy_mini` lazily like
  `audio.py` so it stays Mac-testable.
- `reachy_app/app.py` — add `POST /move` (runs a keyframe sequence via the player); let
  `/frame` capture from the **held** pose when a move preceded it in the same turn
  (via a flag/param), instead of always resetting to the fixed photo pose — so
  "look left → photo" shoots left, not forward.
- `server/main.py` — parse `[MOVE]`, orchestrate move → frame → re-ask → TTS. Robot
  `/move` base URL derived like the camera URL (`robot_move_url` or base-from-request).
- `server/claude_client.py` — motor palette + decision rules added to the voice system
  prompt; a `wants_move()` / marker-parse helper alongside `wants_look()`.
- `server/config.py` — `movement_enabled`, robot `/move` URL setting.
- `reachy_app/tests/test_smoke.py` — player tests (preset resolution, range clamp,
  velocity floor, length cap) against a fake mini; server marker-parsing tests.
- **README (user guide)** — see below; an explicit deliverable.

## Documentation — README user guide (deliverable)

The README must document the feature as a **user guide**, not just internals:

- The full command table (BG phrasing → what the robot does).
- Worked examples, including the composed "look left **and** describe" case.
- **How Claude decides**: move vs. speak vs. both; when it improvises; the silent-by-
  default rule.
- Safety notes (clamping, limits) and the `movement_enabled` / URL settings.

## Testing

Player unit tests against a fake mini: presets resolve to expected keyframes;
out-of-range values clamp; velocity floor enforced; sequence length/duration capped.
Server tests: `[MOVE look_left][LOOK]` yields move-then-frame ordering; markers never
reach TTS; silent move produces no spoken text. No robot required — matches the existing
fake-backend test style. Hardware pass is the final manual check.

## Verify-on-hardware (Reachy currently offline)

1. `VERIFY-ON-HARDWARE` — the SDK's **base-rotation** call (not exposed by
   `create_head_pose`); how to drive the body turntable and its angle limits.
2. `VERIFY-ON-HARDWARE` — the actual **safe range per axis** (yaw/pitch/roll/translation/
   base) and a comfortable max-speed for the velocity floor. Design uses placeholders
   tuned during the hardware pass.
