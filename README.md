# Reachy Mini ↔ Claude Code CLI Connector

Talk to **Claude Code** running on your Mac, out loud, through a
[Reachy Mini](https://www.pollen-robotics.com/reachy-mini/) robot. You speak → it
transcribes → Claude Code answers (with real tool access on your machine) → the robot
speaks the reply and moves while it does.

The robot is just ears, mouth, and expression. All the heavy lifting — speech-to-text,
Claude, text-to-speech — happens on the Mac. Reachy (a Raspberry Pi) captures and plays
audio over the LAN and runs a small turn-taking state machine.

> Currently configured for **Bulgarian** end-to-end (voice, recognition, and replies).
> One setting each swaps the language — see [Language](#language).

---

## How it works

```
  ┌─────────────── Reachy Mini (Pi)  ── or your Mac for testing ──┐
  │  trigger:  phone "hold to talk"   OR   "Hey Reachy" wake word  │
  │      │                                                         │
  │  mic ─┴─► capture ──► end-of-speech (button release | VAD)     │
  │              │  POST WAV over LAN                              │
  │              ▼                                                 │
  │      [ status: listening → thinking → speaking ]  + gestures   │
  │  speaker ◄── play WAV ◄── (audio reply over LAN)              │
  └──────────────│──────────────────────────▲───────────────────┘
                 │                           │
        ─────────┼─────────── LAN ───────────┼──────────
                 ▼                           │
  ┌────────────────────── Mac ───────────────┴──────────────────┐
  │  FastAPI connector (server/)                                 │
  │    POST /chat :  WAV → STT → Claude → TTS → WAV              │
  │    1. STT     faster-whisper  (local)                        │
  │    2. Claude  claude -p  (session-threaded, tools, web)      │
  │    3. TTS     Piper           (local)                        │
  └──────────────────────────────────────────────────────────────┘
```

Two parts, two folders:

| Folder | Runs on | Role |
|--------|---------|------|
| [`server/`](server/) | the Mac | The "brain". FastAPI app: STT → Claude Code CLI → TTS. |
| [`reachy_app/`](reachy_app/) | the Pi (or Mac for testing) | The "thin robot": capture, POST, play, gestures, phone UI. |

See [`DESIGN.md`](DESIGN.md) for the full rationale and locked decisions.

---

## Features

- 🎙️ **Full voice loop** — speak to the robot, hear Claude answer.
- 🧠 **Real Claude Code** — not a hosted chat model; it runs the agent loop with
  **tool access on your Mac** (read/search/edit files, run commands, **web search**).
- 🔀 **Swappable backend** — run it on the actual robot (`reachy_mini` SDK) or entirely
  on your Mac (laptop mic/speakers) for development, via one flag.
- 📱 **Phone control** — a LAN "hold to talk" page with a **live status indicator**
  (Ready / Listening / Thinking / Speaking) and a **conversation history**.
- 🗣️ **Wake word** — optional "Hey Reachy" via Picovoice Porcupine (off until you add a key).
- 🤖 **Embodied feedback** — antenna/head gestures per state; head "wobble" while speaking.
- 🌍 **Any language** — currently wired for Bulgarian; swap with a few settings.
- 🎛️ **Tunable** — model, reasoning effort, and permission posture via `.env` or `make`.

---

## Prerequisites

- **[Claude Code](https://docs.claude.com/en/docs/claude-code)** installed and logged
  in on the Mac (`claude` on your `PATH`; run it once interactively to authenticate).
  The connector deliberately does **not** use `--bare`, so it inherits your login,
  skills, MCP, and settings.
- **Python 3.12+**.
- **macOS local backend** (for testing without the robot) needs PortAudio:
  `brew install portaudio`.
- **On the robot**: the `reachy_mini` SDK installed on the Pi.
- A **Piper voice** (downloaded in setup — not committed to the repo).

---

## Quick start (test on your Mac, no robot)

```bash
git clone git@github.com:miraclebg/reachy-mini-claude-cli-connector.git
cd reachy-mini-claude-cli-connector

# 1. Create both virtualenvs + install deps
make install
brew install portaudio            # macOS, for the local-audio backend

# 2. Download a Piper voice (Bulgarian by default here)
source server/.venv/bin/activate
python -m piper.download_voices bg_BG-dimitar-medium --data-dir voices
deactivate
#   (English instead? use en_US-lessac-medium, and see "Language" below)

# 3. Configure
cp server/.env.example server/.env         # then set PIPER_MODEL (+ language, model…)
cp reachy_app/.env.example reachy_app/.env  # optional; defaults work on the Mac

# 4. Run everything (connector + robot app), prints the phone URL
make run
```

Open the printed URL (`http://<mac-ip>:8081/`) on a phone on the same Wi-Fi, **press
and hold** while you speak, release when done. With `--backend local` the phone is only
the button — the **mic and speakers are your Mac's**. (macOS will ask for microphone
permission the first time.)

---

## Running

`make` (or `make help`) lists everything:

| Command | What it does |
|---------|--------------|
| `make run` | Start connector + app (local backend). Ctrl-C stops both. |
| `make run-robot` | Same, on the real Reachy Mini (`--backend reachy`). |
| `make server` | Only the connector (the brain), for standalone testing. |
| `make test` | Run the `reachy_app` smoke tests (needs the connector running). |
| `make health` | Show the connector's live model / effort / permission / tools. |
| `make logs` | Tail what Reachy heard / replied. |
| `make stop` | Stop the connector + app. |
| `make install` | Create both venvs and install dependencies. |

Inline overrides: `make run EFFORT=low MODEL=sonnet PERMISSION=dontAsk`.

Under the hood `make run` calls [`run.sh`](run.sh), which starts the connector, waits
for it, prints the LAN URL, and runs the app in the foreground. `run.sh` must be
launched from the repo root.

---

## Configuration

Both components are configured by `.env` files (real env vars and `make` overrides take
precedence). Copy the `.env.example` templates and edit.

### `server/.env` — the connector

| Variable | Default | Notes |
|----------|---------|-------|
| `PIPER_MODEL` | — | **Required for audio.** Absolute path to a Piper `.onnx` voice. |
| `CLAUDE_MODEL` | *(CLI default)* | `opus` \| `sonnet` \| `haiku` \| `fable`, or a full id. |
| `CLAUDE_EFFORT` | *(CLI default)* | `low` \| `medium` \| `high` \| `xhigh` \| `max`. Lower = snappier replies. |
| `CLAUDE_PERMISSION_MODE` | `auto` | `auto` (tools auto-approved) \| `dontAsk` (read-only) \| `plan`. See [Permissions](#permissions--security). |
| `CLAUDE_ALLOWED_TOOLS` | `Read,Glob,Grep,WebSearch,WebFetch` | Explicit allow-list (matters under `dontAsk`). |
| `CLAUDE_DISALLOWED_TOOLS` | `Bash(rm *),Bash(sudo *),Bash(curl *),Bash(wget *),Bash(git push *)` | **Deny list — always wins, even under `auto`.** |
| `CLAUDE_MAX_TURNS` | `6` | Cap on agent-loop turns per utterance. |
| `CLAUDE_WORKING_DIR` | `./claude-workspace` | Where Claude runs (scopes `--resume` sessions). |
| `CLAUDE_TIMEOUT_S` | `120` | Per-turn timeout. |
| `WHISPER_MODEL` | `base.en` | `small`/`medium` are multilingual; `*.en` are English-only. |
| `WHISPER_LANGUAGE` | *(auto)* | Force a language, e.g. `bg`. Far more reliable than auto-detect. |
| `WHISPER_DEVICE` / `WHISPER_COMPUTE` | `cpu` / `int8` | |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | |

### `reachy_app/.env` — the robot side

| Variable | Default | Notes |
|----------|---------|-------|
| `CONNECTOR_URL` | `http://localhost:8080` | On the Pi, set to the Mac's LAN IP. |
| `REACHY_BACKEND` | `local` | `local` (Mac mic/speaker) \| `reachy` (robot SDK). |
| `BUTTON_PORT` | `8081` | The hold-to-talk page + status/history endpoints. |
| `WAKEWORD_ENABLED` | `true` | Inactive until a key + keyword are provided. |
| `PICOVOICE_ACCESS_KEY` | — | Free key from the Picovoice console (for the wake word). |
| `PORCUPINE_KEYWORD_PATH` | — | Path to a `Hey-Reachy_*.ppn` keyword file. |
| `VAD_SILENCE_MS` / `VAD_RMS_THRESHOLD` | `800` / `0.015` | End-of-speech tuning (wake-word path). |
| `MAX_UTTERANCE_S` | `15` | Hard cap on a single recording. |

Full lists are in the two `.env.example` files and `*/config.py`.

---

## Language

Everything below is set for **Bulgarian**. To use another language, change all three
together in `server/.env` (they must agree):

1. **Voice (TTS)** — download the voice and point `PIPER_MODEL` at it:
   `python -m piper.download_voices <voice> --data-dir voices`
   (browse voices in the [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) repo).
2. **Recognition (STT)** — `WHISPER_MODEL=small` (multilingual) and `WHISPER_LANGUAGE=<code>`
   (e.g. `bg`, `en`, `de`). Forcing the language avoids mis-detection.
3. **Replies** — the reply language is pinned in `VOICE_SYSTEM_PROMPT` in
   `server/claude_client.py`. Edit that line for a different language.

For English, e.g.: voice `en_US-lessac-medium`, `WHISPER_MODEL=base.en`,
`WHISPER_LANGUAGE=` (empty), and remove the Bulgarian instruction from the prompt.

---

## The phone page

Served at `http://<host>:8081/`. It's a small chat UI:

- **Status indicator** — reflects the real loop state (`Ready · Listening · Thinking ·
  Speaking · error/offline`), so it shows wake-word turns too, not just button presses.
- **Conversation history** — each turn as chat bubbles ("You" / "Reachy"), newest at
  the bottom.
- **Hold-to-talk button** — press-and-hold to record; release ends the utterance.

---

## HTTP API

### Connector (`server/`, port 8080)

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/health` | Readiness + live config (model, effort, permission, tools, session id). |
| `POST` | `/chat` | multipart WAV in → WAV out (the robot path). Transcript/reply in `X-Transcript`/`X-Reply` headers. |
| `POST` | `/chat/text` | `{"text": "..."}` → `{"reply", "session_id"}`. Test the Claude loop with no audio. |
| `POST` | `/reset` | Forget the conversation; next turn starts a fresh session. |

### Robot app (`reachy_app/`, port 8081)

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/` | The hold-to-talk page. |
| `GET`  | `/status` | `{"state":"idle"}` — current loop phase. |
| `GET`  | `/history` | `{"seq":N,"turns":[{"you","reply"}]}` — conversation log. |
| `POST` | `/press` / `/release` | Button hold / end-of-speech. |

---

## Permissions & security

The connector runs Claude with `--permission-mode auto`, which **auto-approves tool
calls** (there's no human to click "approve" in a voice loop). In practice Claude can
**read/edit files, run shell commands, and search the web** — bounded by the
`CLAUDE_DISALLOWED_TOOLS` **deny list**, which always wins even under `auto`.

> ⚠️ **Trust model:** with `auto`, anyone who can speak to the robot can run commands on
> the Mac (minus the deny list). That's fine on your own machine and LAN — just know
> what you're exposing. Harden the deny list, or switch to a read-only posture:
>
> ```bash
> make run PERMISSION=dontAsk        # deny anything not in CLAUDE_ALLOWED_TOOLS
> ```

Everything runs inside `claude-workspace/` (which scopes `--resume` session lookup), but
under `auto` shell commands can reach outside it — the deny list, not the directory, is
the real boundary.

Web-search replies are passed through `clean_for_speech()` so TTS never reads out URLs
or "Sources:" citations.

**Planned (v2):** move to the Python Agent SDK with a **spoken** tool-approval callback,
so Reachy can ask out loud before anything destructive instead of relying only on the
deny list.

---

## Testing

The `reachy_app` side has a smoke suite that runs without a robot or a live mic (it does
need the connector running):

```bash
make server           # in one terminal
make test             # in another
```

It covers the WAV helpers, the silence endpointer, the button/status/history endpoints,
and a full `ConversationLoop` turn through a `FakeBackend` against the real connector
(canned utterance → STT → Claude → TTS → playback, asserting the gesture + status order).

The connector itself is exercised with `server/smoke_test.py` (see `server/README.md`).

---

## Deploying to the actual Reachy Mini

1. On the **Mac**, run just the connector: `make server` (note the Mac's LAN IP).
2. On the **Pi**, install `reachy_app`'s deps + the `reachy_mini` SDK, set
   `CONNECTOR_URL=http://<mac-ip>:8080` in `reachy_app/.env`, and run:
   `python -m reachy_app.main --backend reachy`.
3. Optional: add a Picovoice key + a "Hey Reachy" `.ppn` to enable the wake word.

The robot backend is written against Pollen's documented SDK API; gesture magnitudes and
on-robot wake-word mic routing are the things to tune on first hardware run (see
`reachy_app/README.md` → *On-robot follow-ups*).

---

## Project layout

```
├── server/            # Mac connector (STT → Claude CLI → TTS)
│   ├── main.py           FastAPI app + endpoints
│   ├── claude_client.py  claude -p integration (sessions, voice prompt, speech cleanup)
│   ├── stt.py            faster-whisper wrapper
│   ├── tts.py            Piper wrapper
│   ├── config.py         env-driven settings
│   └── smoke_test.py     standalone tester
├── reachy_app/        # Thin robot side (Pi or Mac)
│   ├── main.py           CLI entry; wires backend + button + wake word + loop
│   ├── loop.py           turn-taking state machine
│   ├── audio.py          AudioBackend: LocalAudio (Mac) + ReachyMini (robot)
│   ├── connector_client.py  POST audio to the Mac
│   ├── button_server.py  hold-to-talk page + status/history endpoints
│   ├── static/index.html the phone UI
│   ├── wakeword.py       Porcupine (no-op until configured)
│   ├── vad.py            RMS end-of-speech
│   └── tests/            smoke suite
├── run.sh             # one-command launcher
├── Makefile           # make run / test / stop / …
├── DESIGN.md          # architecture + decisions
└── CLAUDE.md          # notes for Claude Code working in this repo
```

Not in the repo (gitignored, regenerated locally): the virtualenvs, the `voices/` model
files, your `.env`, and `claude-workspace/`.

---

## Roadmap

- **v2 — command execution with a spoken approval callback** (Agent SDK), replacing
  all-or-nothing `auto`.
- Acoustic echo cancellation (so the wake word can't hear Reachy speak).
- On-robot wake-word mic routing + gesture tuning.
- IMU gestures (pick-up-to-wake / tilt-to-cancel) on the Wireless.
