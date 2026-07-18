# CLAUDE.md

Voice conversation with **Claude Code** running on the local Mac, spoken through a
**Reachy Mini (Wireless)** robot. Audio in → speech-to-text → `claude -p` (with tool
access on the Mac) → text-to-speech → audio out.

Two design docs already carry the detail — read them before making design changes:
- `DESIGN.md` — architecture, activation state machine, decisions (all locked).
- `server/README.md` — install, run, test, and the permission posture.

## Architecture: thin robot, fat Mac

- **`server/`** — runs on the Mac. **Built and standalone-testable.** FastAPI app
  that owns the whole brain: STT → Claude → TTS. This is where nearly all work is.
- **`reachy_app/`** — runs on the Pi (or the Mac for testing). **Built; Mac-runnable
  parts tested, not yet run on hardware.** Kept light: activation state machine, audio
  capture/playback, POST to the Mac, state-feedback gestures. Has its own venv
  (`reachy_app/.venv`) and `README.md`.

The Pi does almost nothing; the Mac does STT (faster-whisper), Claude, and TTS (Piper).

## Running the whole thing

`make` (or `make help`) is the entry point. `make run` starts the connector + app
(local backend) and prints the LAN URL for the phone hold-to-talk page; `make
run-robot` targets the real robot; `make stop` kills both; `make test` runs the
reachy_app suite. Under the hood it's `run.sh`, which must be launched from the repo
root. Model/effort are inline knobs: `make run EFFORT=low MODEL=sonnet`.

**Claude model & effort** are connector settings (`CLAUDE_MODEL`, `CLAUDE_EFFORT` in
`server/.env`, or passed via `make`). Effort is `low|medium|high|xhigh|max` → `claude
--effort`; lower = snappier voice replies. Both surface in `GET /health`.

## Working in `server/`

Always use the project venv:

```bash
cd server
source .venv/bin/activate          # Python 3.12
pip install -r requirements.txt    # if deps changed

# run the server (0.0.0.0 so the Pi can reach it over LAN)
uvicorn main:app --host 0.0.0.0 --port 8080
```

Test standalone, no robot needed (server must be running in another shell):

```bash
python smoke_test.py --text "introduce yourself in one sentence"   # Claude loop only
python smoke_test.py --wav sample.wav                              # full audio loop -> reply.wav
python smoke_test.py --reset --text "what's my name?"             # verify session reset
```

There is no test suite yet for the server. `smoke_test.py` against a running server
is its verification path.

## Working in `reachy_app/`

Separate venv, separate deps (kept light for the Pi). Package is run as a module.

```bash
cd reachy_app
source .venv/bin/activate

# run the whole loop on the Mac (laptop mic/speakers, wake word off):
python -m reachy_app.main --backend local --no-wakeword    # run from repo root

# on the robot:  python -m reachy_app.main --backend reachy
```

Tests (need the server running on :8080), from the repo root with the reachy_app venv:

```bash
python -m reachy_app.tests.test_smoke
```

`reachy_app` has a real test suite (`tests/test_smoke.py`): WAV helpers, the silence
endpointer, the button server, and a full `ConversationLoop` turn through a
`FakeBackend` against the live Mac server. Backend-swap design (`audio.py`) is what
keeps it testable without the robot — `reachy_mini` and `sounddevice`/`pvporcupine`
are all imported lazily.

### Module map (`server/`)
- `main.py` — FastAPI app. Endpoints: `GET /health`, `POST /chat/text` (JSON, no
  audio), `POST /chat` (WAV in, WAV out), `POST /reset`.
- `claude_client.py` — the `claude -p` integration. Owns session threading and the
  voice-tuned system prompt.
- `stt.py` — faster-whisper wrapper (loads model once at startup).
- `tts.py` — Piper wrapper (loads voice once at startup).
- `config.py` — all settings, env-driven via `.env` (see `.env.example`).

## Key facts & gotchas

- **Git repo** with remote `origin`
  (`github.com:miraclebg/reachy-mini-claude-cli-connector`), default branch `main`.
  Venvs, `voices/` (Piper models), `.env`, and `claude-workspace/` are gitignored —
  regenerated locally, never committed.
- **Claude runs from `claude-workspace/`** (`CLAUDE_WORKING_DIR`). This is deliberate:
  `--resume` is scoped per-directory (so the session threads), and it scopes Claude's
  filesystem blast radius. Don't change this without understanding both effects.
- **Session threading:** `claude_client.py` captures `.session_id` from the first
  turn's JSON and passes `--resume <id>` on every following turn. `ClaudeClient`
  holds this in memory — the server is single-conversation and stateful.
- **Permission posture (now `auto` by default):** `--permission-mode auto`
  auto-approves tool calls — so command execution, file edits, and web search
  (`WebSearch`,`WebFetch`) are ON. Config: `CLAUDE_PERMISSION_MODE` (or `make run
  PERMISSION=...`); `dontAsk` restores read-only (deny anything not in `allowed_tools`;
  a denied tool doesn't crash the run, Claude just adapts).
- **Access control is the token, NOT the deny list.** `CLAUDE_DISALLOWED_TOOLS`
  (`rm`,`sudo`,`curl`,`wget`,`git push`) is bypassable defense-in-depth (`/bin/rm`,
  `find -delete`, `python -c os.remove`… slip past it) — a speed bump, not a boundary.
  The real control is **shared-token auth** (see below) + the permission mode.
- **Token auth (both services):** connector requires `CONNECTOR_TOKEN` on all endpoints
  except `/health` (`Authorization: Bearer`); the app requires `BUTTON_TOKEN` on the
  phone page/status/history/press (via `?token=` or `X-Auth-Token`, `/health` exempt).
  Empty token ⇒ that service runs open + logs a warning. Tokens live in the two `.env`
  files and must match on the connector side. Each `config.py` loads its **own** sibling
  `.env` explicitly (the app runs from repo root, so a bare `load_dotenv()` would miss it).
- **Speech cleanup:** `clean_for_speech()` in `claude_client.py` strips
  `Sources:`/citations, markdown links, and bare URLs from replies (web search adds
  them; they sound terrible via TTS).
- **Spoken output:** `VOICE_SYSTEM_PROMPT` in `claude_client.py` forces short,
  conversational, no-markdown replies. Editing Claude's spoken persona/format happens
  there.
- **TTS is optional at startup.** If `PIPER_MODEL` is unset/missing, the server still
  starts and `/chat/text` works; only `/chat` (audio) returns 503. This is intended.
- **Language: currently configured for Bulgarian, end-to-end.** Voice =
  `voices/bg_BG-dimitar-medium.onnx`; STT = `WHISPER_MODEL=small` (multilingual) +
  `WHISPER_LANGUAGE=bg` (forcing the language is far more reliable than auto-detect,
  which garbled the transcript); replies forced to Bulgarian via `VOICE_SYSTEM_PROMPT`.
  To change language: swap all three together (voice, `WHISPER_LANGUAGE`, the prompt).

## Setup — done in this workspace
- `.env` exists (`server/.env`), configured: haiku · low effort · `auto` perms · web
  tools · Bulgarian (voice + STT `small`/`bg` + Bulgarian replies).
- Piper voices downloaded in `voices/` (both `bg_BG-dimitar-medium` and
  `en_US-lessac-medium`). `PIPER_MODEL` points at the Bulgarian one.
- **Prereq:** Claude Code is installed and logged in (`claude` on PATH; confirmed
  present). We deliberately do NOT use `--bare`, so the connector inherits the
  existing login, skills, MCP, and CLAUDE.md setup.
