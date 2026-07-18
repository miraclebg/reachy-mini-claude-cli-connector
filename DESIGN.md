# Reachy Mini ↔ Claude Code CLI Connector

Voice conversation with Claude Code running on the local Mac, spoken through a
Reachy Mini (Wireless) robot. Claude has tool access on the Mac (read-only in v1;
full command execution planned for v2).

## Architecture: thin robot, fat Mac

Reachy is ears, mouth, and expression. All the heavy lifting (speech-to-text,
Claude, text-to-speech) happens on the Mac. The Pi captures/plays audio, runs a
small activation state machine, and moves.

```
  ┌─────────────── Reachy Mini (Wireless / Raspberry Pi) ────┐
  │  activation:  "Hey Reachy" (Porcupine)  OR  phone button  │
  │       │                                                   │
  │       ▼                                                   │
  │  mic ──► capture ──► end-of-speech (VAD, or button release)│
  │              │  (POST WAV over LAN)                       │
  │              ▼                                            │
  │        [ thinking gesture while it waits ]                │
  │              ▲                                            │
  │  speaker ◄── play WAV  ◄── (audio reply over LAN)         │
  │  antennas show state: listening / thinking / idle         │
  └──────────────│───────────────────────▲──────────────────┘
                 │                        │
        ─────────┼──── LAN ───────────────┼─────────
                 ▼                        │
  ┌──────────────────────── Mac ──────────┴──────────────────┐
  │  FastAPI server  (server/, BUILT)                         │
  │    POST /chat  : WAV -> STT -> Claude -> TTS -> WAV        │
  │    1. STT   faster-whisper (local)                        │
  │    2. Claude  claude -p  (dontAsk, read-only, session-threaded)
  │    3. TTS   Piper (local)                                 │
  └───────────────────────────────────────────────────────────┘
```

## Activation (turn-taking) — BOTH modes

Two triggers into one state machine. Complementary: the phone button is reliable
in noise and never false-triggers; the wake word is hands-free; the button is also
the fallback when the wake word mishears.

```
  IDLE ──(wake word "Hey Reachy" | phone press)──► LISTENING
  LISTENING ──(VAD end-of-speech | phone release)──► SENDING
  SENDING ──(audio reply arrives)──► SPEAKING
  SPEAKING ──(playback done)──► IDLE
```

- **Wake word** — Porcupine, always-on ON THE PI (the one exception to "Pi does
  nothing", but light). "Hey Reachy" is a custom keyword generated in the Picovoice
  console; needs a free Picovoice access key.
- **Phone button** — the Pi app serves a "hold to talk" page on the LAN. Press =
  start capture, release = end. No VAD needed (release is the end-of-speech signal).
- **VAD (silero)** covers the wake-word path only (marks the END of the utterance).
- **Echo gotcha:** always-on mic + speaker → mic hears Reachy and can false-trigger
  the wake word. v1: mute detection during playback. Later: acoustic echo cancel.
- **Embodied feedback:** antennas signal state — perk on LISTENING, thinking gesture
  on SENDING, settle on IDLE.
- **Bonus (reserved):** Wireless has an IMU → pick-up-to-wake / tilt-to-cancel later.

## Components

### `server/` — runs on the Mac ✅ BUILT
- FastAPI. `POST /chat` (WAV in, WAV out), `POST /chat/text` (test w/o audio),
  `POST /reset`, `GET /health`.
- Owns the Claude session id so turns thread.
- Runs Claude from a fixed working directory (`../claude-workspace/`) — `--resume`
  is per-directory, and it scopes Claude's filesystem blast radius.
- Standalone-testable with a WAV file; no robot needed (`smoke_test.py`).

### `reachy_app/` — runs on the Pi (kept light) ✅ BUILT
- Swappable `AudioBackend`: `LocalAudioBackend` (Mac mic/speaker, for testing) and
  `ReachyMiniBackend` (real SDK: `play_sound` + `enable_wobbling`, antenna/head gestures).
- Stdlib-http LAN hold-to-talk page (phone button) + Porcupine wake word (no-op until a
  Picovoice key + `.ppn` are set) + RMS silence endpointer for the wake-word path.
- `ConversationLoop` runs the state machine; POSTs to the Mac `/chat`, plays the reply.
- Testable on the Mac with no robot (`python -m reachy_app.tests.test_smoke`).
- Not yet run on real hardware — gesture magnitudes + on-robot wake-word mic are the
  follow-ups (see reachy_app/README.md).

## Claude integration (in server/claude_client.py)

```bash
claude -p "<utterance>" \
  --output-format json \
  --permission-mode auto \
  --allowedTools "Read,Glob,Grep,WebSearch,WebFetch" \
  --disallowedTools "Bash(rm *),Bash(sudo *),Bash(curl *),Bash(wget *),Bash(git push *)" \
  --append-system-prompt "You are Reachy... spoken aloud, short, no markdown..." \
  [--resume <session_id>]
```
Not `--bare`: we want the full local setup (existing login, skills, MCP, CLAUDE.md).
Server parses `.result` (speak it), stores `.session_id` (reuse it).

## Decisions

- [x] **Thin Pi** — STT/TTS/Claude on the Mac.
- [x] **Hardware** — Reachy Mini **Wireless** (IMU reserved for later).
- [x] **Activation** — BOTH "Hey Reachy" (Porcupine) AND phone-button push-to-talk.
- [x] **Push-to-talk input** — phone (LAN hold-to-talk page).
- [x] **STT** — faster-whisper (local).
- [x] **TTS** — Piper (local).
- [x] **Transport** — one-shot POST per utterance.
- [x] **Voice-tuned system prompt** — short, spoken, no markdown, via `--append-system-prompt`.
- [x] **Wake-word engine** — **Porcupine**.
- [x] **Permissions** — default **`auto`**: auto-approves tools (command execution,
      edits, and web search ON), bounded by the **deny list** (`rm`,`sudo`,`curl`,
      `wget`,`git push`), which wins even under `auto`. Configurable via
      `CLAUDE_PERMISSION_MODE` / `make PERMISSION=`; `dontAsk` restores the read-only
      posture. Trade-off: voice = command execution on the Mac. **v2** adds a spoken
      tool-approval callback (Agent SDK) so Reachy asks out loud before destructive acts.

## Status
- ✅ Design locked.
- ✅ `server/` built + verified end-to-end on the Mac (STT→Claude→TTS, sessions, tools).
- ✅ `reachy_app/` built — phone hold-to-talk + wake-word wiring + swappable backend;
  Mac-runnable parts tested (see reachy_app/README.md).
- ⏳ Next: run on the actual Reachy Mini (`--backend reachy`), tune gestures, and add
  the Picovoice key to light up "Hey Reachy". v2: command execution via Agent SDK.
