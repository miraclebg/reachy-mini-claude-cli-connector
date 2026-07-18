# Reachy Vision — design

**Date:** 2026-07-18
**Status:** Approved (pending spec review)

## Summary

Let the user ask Reachy what it sees ("Рийчи, виж какво има пред теб") and have it
answer from a real camera frame. Vision is **opt-in per turn via a spoken trigger
word** — nothing is captured or sent unless the user explicitly asks to look.

Both technical unknowns are already validated:
- **Camera:** `reachy_mini` exposes `mini.media.get_frame_jpeg()` → JPEG bytes.
- **Claude sees images:** `claude -p` reads an image file via the `Read` tool (already
  in the allow-list). Verified: given a test image, Claude correctly described the
  shapes, colors, and text.

## Goals

- Ask-to-see: a spoken trigger makes Reachy look and answer about the current scene.
- Zero cost / zero capture on normal (non-vision) turns.
- Reuse the existing turn pipeline; vision is an additive branch.

## Non-goals

- Continuous / always-on vision, streaming, or video.
- Face recognition, tracking, or any vision that runs without an explicit ask.
- On-robot image analysis (all reasoning stays with Claude on the Mac).

## Flow

Only a turn whose transcript contains a trigger word does anything new:

```
You: "Рийчи, виж какво има пред теб"
   └─hold/release─► robot POSTs audio to Mac  POST /chat            (unchanged)
        Mac: STT → transcript
        trigger word present?
          no  → normal turn (unchanged)
          yes → Mac GET http://<robot-ip>:8042/frame  → JPEG
                Mac saves claude-workspace/camera_view.jpg
                Mac asks Claude: <utterance> + "photo at camera_view.jpg — look at it"
                Claude reads the image (Read tool) → answer
        → TTS → robot speaks                                        (unchanged)
```

The robot IP is **auto-discovered** from the incoming request (`request.client.host`);
no static robot address is configured. An optional `ROBOT_CAMERA_URL` env var overrides
it for edge cases.

## Components

### 1. Robot — `GET /frame` (in `reachy_app/app.py`)

Add one route to the app's existing settings web server (`custom_app_url`, `:8042`):

```
GET /frame → 200 image/jpeg   (reachy_mini.media.get_frame_jpeg())
           → 503              if the camera returned no frame yet
```

The `reachy_mini` instance is already available in `run()`; the route closes over it.

### 2. Mac connector — vision path

**New `server/vision.py`:**
- `transcript_wants_vision(text, triggers) -> bool` — case-insensitive substring match
  of the transcript against the trigger list.
- `fetch_frame(base_url, timeout_s) -> bytes | None` — `GET {base_url}/frame`, returns
  JPEG bytes or `None` on any failure (unreachable, 503, timeout, non-image).

**`server/main.py` `/chat`:** after STT, before calling Claude:
- If vision enabled AND `transcript_wants_vision(...)`:
  - Determine the robot base URL: `ROBOT_CAMERA_URL` if set, else
    `http://{request.client.host}:{CAMERA_PORT}`.
  - `frame = fetch_frame(...)`.
  - If `frame`: write `claude-workspace/camera_view.jpg`; set `image_path` to it.
  - If no frame: set a flag so Claude is told the camera was unavailable.
- Call `claude.ask(transcript, image_path=..., camera_failed=...)`.
- After the turn, delete `camera_view.jpg`.

`/chat/text` is unaffected (no robot to fetch from); vision is a `/chat`-only concern.

### 3. Claude — `claude_client.ask(prompt, image_path=None, camera_failed=False)`

- If `image_path`: append a line to the prompt, e.g.
  *"(A photo from your camera is saved at `camera_view.jpg` in your working directory.
  Use the Read tool to look at it, then answer the question about what you see.)"*
- If `camera_failed`: append *"(You tried to look but the camera returned no image; tell
  the user you couldn't see right now.)"*
- Otherwise unchanged. `Read` is already an allowed tool, and Claude runs in
  `claude-workspace/`, so a relative path resolves.

## Configuration (`server/.env`, English/Latin defaults documented in `.env.example`)

| Var | Default | Meaning |
|-----|---------|---------|
| `VISION_ENABLED` | `true` | Master switch for the vision branch. |
| `VISION_TRIGGERS` | `виж,погледни,виждаш,снимк,камер,look,see` | Substring triggers (comma-separated). |
| `CAMERA_PORT` | `8042` | Port of the robot's `/frame` (the app's web server). |
| `CAMERA_TIMEOUT_S` | `4` | Frame-fetch timeout. |
| `ROBOT_CAMERA_URL` | *(empty)* | Optional explicit base URL; else auto-discovered. |

## Error handling

- Trigger heard but frame fetch fails (robot unreachable / camera not ready / timeout)
  → Claude is told the camera was unavailable and says so gracefully, instead of
  answering about an image it never got.
- No trigger → the vision code never runs; the turn is byte-for-byte the old path.

## Privacy & security

- A frame is captured and transmitted **only** when a trigger word is heard; the saved
  JPEG is deleted after the turn.
- **Accepted tradeoff:** the robot's `/frame` on `:8042` is unauthenticated (matching the
  platform convention for the app's config page), so on-trigger any LAN device could also
  fetch a frame. Acceptable on a trusted home LAN; token-gating can be added later.

## Testing

- **Robot:** `curl http://<robot>:8042/frame` returns a valid JPEG; returns 503 before
  the camera is ready.
- **Connector unit tests (no robot):** `transcript_wants_vision` matches / doesn't match
  expected phrases; `fetch_frame` returns bytes from a fake HTTP source and `None` on
  failure; `claude_client.ask` appends the image note when `image_path` is set.
- **End-to-end:** with the robot running, say "виж…" → Reachy describes the real scene;
  say a normal phrase → no frame is fetched (verify via logs).

## Latency

A vision turn adds ~3–5 s (frame fetch + Claude reading the image). Normal turns are
unaffected.

## Files touched

- `reachy_app/app.py` — add `GET /frame`.
- `server/vision.py` — new: trigger match + frame fetch.
- `server/main.py` — wire the vision branch into `/chat`.
- `server/claude_client.py` — `ask()` accepts `image_path` / `camera_failed`.
- `server/config.py`, `server/.env.example` — new vision settings.
- Tests in `reachy_app/tests/` and/or a small connector test.
