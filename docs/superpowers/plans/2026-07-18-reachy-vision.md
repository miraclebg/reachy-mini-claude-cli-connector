# Reachy Vision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user ask Reachy to look ("Рийчи, виж…") and answer from a real camera frame, opt-in per turn via a spoken trigger word.

**Architecture:** On a `/chat` turn, the Mac connector checks the STT transcript for a trigger word; if present it fetches a fresh JPEG from the robot's `GET /frame` (robot IP auto-discovered from the request), saves it into `claude-workspace/`, and tells Claude to Read it. Non-trigger turns are unchanged. Claude reads the image via the already-allowed `Read` tool.

**Tech Stack:** Python 3.12, FastAPI (connector), `requests`, the `reachy_mini` SDK (`media.get_frame_jpeg()`), pytest (new dev dep for connector unit tests).

## Global Constraints

- Vision runs **only** when `VISION_ENABLED` is true AND a trigger substring appears in the transcript. No trigger → the turn is byte-for-byte the existing path.
- Robot IP is **auto-discovered** from `request.client.host`; `ROBOT_CAMERA_URL` overrides it if set.
- The saved frame is `claude-workspace/camera_view.jpg`, referenced to Claude by the **relative** path `camera_view.jpg` (Claude's subprocess cwd is `claude_working_dir`), and **deleted after the turn**.
- Default triggers (verbatim): `виж,погледни,виждаш,снимк,камер,look,see`.
- Default config: `VISION_ENABLED=true`, `CAMERA_PORT=8042`, `CAMERA_TIMEOUT_S=4`, `ROBOT_CAMERA_URL=` (empty).
- Connector unit tests run from the `server/` directory (its modules import as top-level, e.g. `from config import settings`).
- `Read` is already in the allow-list; do not change tool permissions.

---

### Task 1: `server/vision.py` — trigger match + frame fetch

**Files:**
- Create: `server/vision.py`
- Create: `server/test_vision.py`
- Modify: `server/requirements.txt` (add `pytest`)

**Interfaces:**
- Produces:
  - `transcript_wants_vision(text: str, triggers: list[str]) -> bool`
  - `fetch_frame(base_url: str, timeout_s: float = 4.0) -> bytes | None`

- [ ] **Step 1: Add pytest to requirements and install**

Add this line to `server/requirements.txt` (under the existing deps):

```
pytest>=8.0            # connector unit tests
```

Run (from `server/`, venv active): `pip install pytest`
Expected: pytest installs successfully.

- [ ] **Step 2: Write the failing test**

Create `server/test_vision.py`:

```python
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vision import transcript_wants_vision, fetch_frame

TRIGGERS = ["виж", "погледни", "виждаш", "look", "see"]


def test_trigger_matches_plain():
    assert transcript_wants_vision("Рийчи, виж какво има", TRIGGERS)


def test_trigger_matches_inflected():
    # "виждаш" contains the stem, and is itself a trigger
    assert transcript_wants_vision("какво виждаш пред теб", TRIGGERS)


def test_trigger_matches_english_case_insensitive():
    assert transcript_wants_vision("Reachy, LOOK at this", TRIGGERS)


def test_no_trigger():
    assert not transcript_wants_vision("как си днес", TRIGGERS)


def test_empty_transcript():
    assert not transcript_wants_vision("", TRIGGERS)


def _serve(handler_cls):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_fetch_frame_ok():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(b"\xff\xd8JPEGDATA")
        def log_message(self, *a):
            pass
    httpd, base = _serve(H)
    try:
        assert fetch_frame(base) == b"\xff\xd8JPEGDATA"
    finally:
        httpd.shutdown()


def test_fetch_frame_503_returns_none():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.end_headers()
        def log_message(self, *a):
            pass
    httpd, base = _serve(H)
    try:
        assert fetch_frame(base) is None
    finally:
        httpd.shutdown()


def test_fetch_frame_unreachable_returns_none():
    assert fetch_frame("http://127.0.0.1:1", timeout_s=1) is None
```

- [ ] **Step 3: Run test to verify it fails**

Run (from `server/`): `pytest test_vision.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vision'`.

- [ ] **Step 4: Write minimal implementation**

Create `server/vision.py`:

```python
# server/vision.py
"""Vision helpers for the connector.

Two tiny, pure-ish functions: decide whether an utterance is asking to *see*, and
pull a single JPEG frame from the robot's /frame endpoint. Kept separate from main.py
so they're unit-testable without FastAPI or a robot.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("connector.vision")


def transcript_wants_vision(text: str, triggers: list[str]) -> bool:
    """True if any trigger substring appears in the transcript (case-insensitive)."""
    low = (text or "").lower()
    return any(t and t.lower() in low for t in triggers)


def fetch_frame(base_url: str, timeout_s: float = 4.0) -> bytes | None:
    """GET {base_url}/frame -> JPEG bytes, or None on any failure."""
    url = base_url.rstrip("/") + "/frame"
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

- [ ] **Step 5: Run test to verify it passes**

Run (from `server/`): `pytest test_vision.py -v`
Expected: PASS — all 8 tests.

- [ ] **Step 6: Commit**

```bash
git add server/vision.py server/test_vision.py server/requirements.txt
git commit -m "feat(vision): trigger-word match + robot frame fetch"
```

---

### Task 2: `claude_client.ask()` — optional image / camera-failed prompt

**Files:**
- Modify: `server/claude_client.py`
- Create: `server/test_claude_prompt.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `_augment_prompt(prompt: str, image_path: str | None, camera_failed: bool) -> str` (module-level)
  - `ClaudeClient.ask(self, prompt: str, image_path: str | None = None, camera_failed: bool = False) -> str`

- [ ] **Step 1: Write the failing test**

Create `server/test_claude_prompt.py`:

```python
from claude_client import _augment_prompt


def test_no_image_unchanged():
    assert _augment_prompt("как си?", None, False) == "как си?"


def test_image_path_appended():
    out = _augment_prompt("какво виждаш?", "camera_view.jpg", False)
    assert "camera_view.jpg" in out
    assert "Read tool" in out
    assert out.startswith("какво виждаш?")


def test_camera_failed_note():
    out = _augment_prompt("какво виждаш?", None, True)
    assert "no image" in out.lower()
    assert out.startswith("какво виждаш?")


def test_image_path_takes_precedence_over_failed():
    out = _augment_prompt("q", "camera_view.jpg", True)
    assert "camera_view.jpg" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `server/`): `pytest test_claude_prompt.py -v`
Expected: FAIL — `ImportError: cannot import name '_augment_prompt'`.

- [ ] **Step 3: Add `_augment_prompt` and thread it through `ask`**

In `server/claude_client.py`, add this module-level function just below `clean_for_speech`:

```python
def _augment_prompt(prompt: str, image_path: str | None, camera_failed: bool) -> str:
    """Add a camera note so Claude looks at (or acknowledges the absence of) a frame."""
    if image_path:
        return (
            prompt + f"\n\n(A photo from your camera is saved at {image_path} in your "
            "working directory. Use the Read tool to look at it, then answer the user's "
            "question about what you see.)"
        )
    if camera_failed:
        return (
            prompt + "\n\n(You tried to look but the camera returned no image; briefly "
            "tell the user you couldn't see right now.)"
        )
    return prompt
```

Then change `ask` (currently `def ask(self, prompt: str) -> str:`). Update its signature and the first line that builds the command:

```python
    def ask(self, prompt: str, image_path: str | None = None, camera_failed: bool = False) -> str:
        """Send one user turn, return Claude's spoken reply text."""
        full_prompt = _augment_prompt(prompt, image_path, camera_failed)
        cmd = self._build_cmd(full_prompt)
        log.info("claude ask (session=%s, image=%s): %r", self.session_id, bool(image_path), prompt)
```

(Leave the rest of `ask` unchanged — it already uses `cmd`.)

- [ ] **Step 4: Run test to verify it passes**

Run (from `server/`): `pytest test_claude_prompt.py -v`
Expected: PASS — all 4 tests.

- [ ] **Step 5: Commit**

```bash
git add server/claude_client.py server/test_claude_prompt.py
git commit -m "feat(vision): ask() accepts image_path / camera_failed"
```

---

### Task 3: Robot — `GET /frame` endpoint

**Files:**
- Modify: `reachy_app/app.py` (inside `run()`, where the other `self.settings_app` routes are defined)

**Interfaces:**
- Consumes: `reachy_mini.media.get_frame_jpeg()` → JPEG bytes (or falsy if not ready).
- Produces: `GET /frame` on the app's web server (`:8042`) → `image/jpeg` (200) or 503.

- [ ] **Step 1: Add the route**

In `reachy_app/app.py`, inside `run()`, find the existing route block that starts with `@app.post("/press")`. Add this route alongside them (after the `@app.get("/history")` route). Note `Response` is already imported at the top of `run()` (`from fastapi.responses import Response`):

```python
        @app.get("/frame")
        def _frame() -> Response:
            jpeg = reachy_mini.media.get_frame_jpeg()
            if not jpeg:
                return Response(status_code=503, content=b"no frame")
            return Response(content=bytes(jpeg), media_type="image/jpeg")
```

- [ ] **Step 2: Syntax check**

Run (from repo root): `python3 -m py_compile reachy_app/app.py`
Expected: no output (success).

- [ ] **Step 3: Commit**

```bash
git add reachy_app/app.py
git commit -m "feat(vision): robot GET /frame returns a camera JPEG"
```

- [ ] **Step 4: Deploy + verify on hardware**

Run:
```bash
git push
ssh pollen@10.10.9.29 'cd ~/reachy-mini-claude-cli-connector && git pull -q && \
  /venvs/apps_venv/bin/pip install -q --force-reinstall --no-deps . && \
  curl -s -X POST http://127.0.0.1:8000/api/apps/start-app/reachy_claude_connector >/dev/null && sleep 10 && \
  curl -s -o /tmp/frame.jpg -w "frame: HTTP %{http_code} %{size_download} bytes type=%{content_type}\n" http://127.0.0.1:8042/frame && \
  file /tmp/frame.jpg'
```
Expected: `HTTP 200`, a few KB, `content_type=image/jpeg`, and `file` reports `JPEG image data`.

---

### Task 4: Connector config + `/chat` wiring

**Files:**
- Modify: `server/config.py`
- Modify: `server/.env.example`
- Modify: `server/main.py`

**Interfaces:**
- Consumes: `transcript_wants_vision`, `fetch_frame` (Task 1); `ClaudeClient.ask(..., image_path, camera_failed)` (Task 2).
- Produces: `Settings.vision_enabled: bool`, `Settings.vision_triggers: str`, `Settings.camera_port: int`, `Settings.camera_timeout_s: float`, `Settings.robot_camera_url: str`.

- [ ] **Step 1: Add config fields**

In `server/config.py`, add to the `Settings` dataclass (after the `debug_audio_dir` field):

```python
    # --- vision (ask-to-see) ---
    vision_enabled: bool = _as_bool(os.environ.get("VISION_ENABLED", "true"))
    # Substring triggers (comma-separated). A frame is fetched only when the transcript
    # contains one of these.
    vision_triggers: str = os.environ.get(
        "VISION_TRIGGERS", "виж,погледни,виждаш,снимк,камер,look,see"
    )
    camera_port: int = int(os.environ.get("CAMERA_PORT", "8042"))
    camera_timeout_s: float = float(os.environ.get("CAMERA_TIMEOUT_S", "4"))
    # Explicit robot base URL for /frame; empty = auto-discover from the request.
    robot_camera_url: str = os.environ.get("ROBOT_CAMERA_URL", "")
```

- [ ] **Step 2: Document in `.env.example`**

Append to `server/.env.example`:

```
# --- Vision (ask-to-see) ---
# A camera frame is fetched from the robot ONLY when the transcript contains a trigger.
# VISION_ENABLED=true
# VISION_TRIGGERS=виж,погледни,виждаш,снимк,камер,look,see
# CAMERA_PORT=8042
# CAMERA_TIMEOUT_S=4
# ROBOT_CAMERA_URL=          # empty = auto-discover the robot's IP from the request
```

- [ ] **Step 3: Wire the vision branch into `/chat`**

In `server/main.py`:

(a) Add the import near the other connector imports (top of file):

```python
from vision import fetch_frame, transcript_wants_vision
```

(b) The `chat` endpoint currently is `async def chat(audio: UploadFile = File(...)):`. Change it to also take the request (so we can read the client IP):

```python
@app.post("/chat")
async def chat(request: Request, audio: UploadFile = File(...)):
```

(`Request` is already imported in main.py.)

(c) Replace the STT + Claude block. The current code is:

```python
        # 2) STT
        transcript = stt.transcribe(in_path)

        # 3) if we heard nothing, answer without bothering Claude
        if not transcript:
            reply = settings.msg_no_speech
        else:
            try:
                reply = claude.ask(transcript)
            except ClaudeError as e:
                log.error("Claude error: %s", e)
                reply = settings.msg_error
```

Replace it with:

```python
        # 2) STT
        transcript = stt.transcribe(in_path)

        # 2b) Vision: only if the user asked to look, fetch a frame from the robot.
        image_path = None
        camera_failed = False
        img_file = os.path.join(settings.claude_working_dir, "camera_view.jpg")
        triggers = [t.strip() for t in settings.vision_triggers.split(",") if t.strip()]
        if settings.vision_enabled and transcript and transcript_wants_vision(transcript, triggers):
            base = settings.robot_camera_url or f"http://{request.client.host}:{settings.camera_port}"
            log.info("vision trigger — fetching frame from %s", base)
            frame = fetch_frame(base, settings.camera_timeout_s)
            if frame:
                with open(img_file, "wb") as fh:
                    fh.write(frame)
                image_path = "camera_view.jpg"  # relative to Claude's cwd (claude_working_dir)
            else:
                camera_failed = True

        # 3) if we heard nothing, answer without bothering Claude
        if not transcript:
            reply = settings.msg_no_speech
        else:
            try:
                reply = claude.ask(transcript, image_path=image_path, camera_failed=camera_failed)
            except ClaudeError as e:
                log.error("Claude error: %s", e)
                reply = settings.msg_error
```

(d) Add image cleanup in the existing `finally` block of `chat` (which currently unlinks `in_path`). After the `os.unlink(in_path)` try/except, add:

```python
        try:
            if os.path.exists(img_file):
                os.unlink(img_file)
        except OSError:
            pass
```

- [ ] **Step 4: Syntax check + config smoke**

Run (from repo root): `python3 -m py_compile server/config.py server/main.py`
Expected: no output.

Run (from `server/`, venv active):
```bash
python -c "from config import settings; print(settings.vision_enabled, settings.camera_port, settings.vision_triggers.split(',')[0])"
```
Expected: `True 8042 виж`

- [ ] **Step 5: Commit**

```bash
git add server/config.py server/.env.example server/main.py
git commit -m "feat(vision): wire ask-to-see into /chat (auto-discover robot IP)"
```

---

### Task 5: End-to-end verification on hardware

**Files:** none (verification only).

- [ ] **Step 1: Restart the Mac connector**

Run (from repo root):
```bash
pkill -f "uvicorn main:app"; sleep 2
cd server && source .venv/bin/activate && \
  nohup uvicorn main:app --host 0.0.0.0 --port 8080 > /tmp/connector.log 2>&1 & cd ..
sleep 20 && curl -s localhost:8080/health >/dev/null && echo "connector up"
```
Expected: `connector up`.

- [ ] **Step 2: Confirm the robot app is running with the new /frame**

Run:
```bash
curl -s -o /dev/null -w "robot /frame: HTTP %{http_code}\n" http://10.10.9.29:8042/frame
```
Expected: `HTTP 200` (camera ready) — if 503, wait a few seconds and retry.

- [ ] **Step 3: Ask the human to speak a vision request**

Tell the user: open `http://10.10.9.29:8042/`, hold, say **"Рийчи, виж какво има пред теб"**, release. Then a normal one: hold, say **"как си днес?"**, release.

- [ ] **Step 4: Verify from the logs**

Run: `grep -E "vision trigger|transcript:" /tmp/connector.log | tail -6`
Expected: the "виж" turn logs `vision trigger — fetching frame from http://10.10.9.29:8042`; the "как си" turn does **not**. Reachy's spoken reply to the vision turn describes the real scene; the normal turn is unaffected.

- [ ] **Step 5: Confirm the frame was cleaned up**

Run: `ls /Users/mkovachev/sworkspace/reachi-min/claude-cli-connector/claude-workspace/camera_view.jpg 2>&1`
Expected: `No such file or directory` (deleted after the turn).

---

## Self-Review

**Spec coverage:**
- Robot `GET /frame` → Task 3. ✓
- Trigger match + frame fetch → Task 1. ✓
- Claude image note / camera-failed note → Task 2. ✓
- `/chat` wiring, auto-discover IP, save+delete frame → Task 4. ✓
- Config + `.env.example` → Task 4. ✓
- Error handling (fetch fail → camera_failed note) → Tasks 1/2/4. ✓
- End-to-end + non-trigger-unaffected verification → Task 5. ✓
- Privacy (fetch only on trigger, delete after) → Task 4 (Step 3c/3d). ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `transcript_wants_vision(text, triggers: list[str])`, `fetch_frame(base_url, timeout_s) -> bytes | None`, `ask(prompt, image_path=None, camera_failed=False)`, `_augment_prompt(prompt, image_path, camera_failed)` — names/signatures match across Tasks 1, 2, and 4. `image_path` is the relative string `"camera_view.jpg"` in all uses; the file is saved to `settings.claude_working_dir/camera_view.jpg`. Consistent.
