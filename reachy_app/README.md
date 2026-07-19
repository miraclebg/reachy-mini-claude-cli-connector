# reachy_app (runs on the Reachy Mini — or your Mac for testing)

The thin robot half. It captures speech, POSTs it to the Mac connector server
(`../server/`), and speaks the reply — driving state gestures along the way. All the
heavy lifting (STT, Claude, TTS) happens on the Mac; this side stays light.

```
  trigger (phone hold-to-talk  |  "Hey Reachy" wake word)
     └─► LISTENING ─► record utterance ─► SENDING ─► POST /chat to the Mac
                                                        └─► SPEAKING ─► play reply ─► IDLE
```

## Two backends, one interface

`--backend local` — Mac mic + speakers (sounddevice). Gestures are logged, not moved.
Lets you run the **entire** loop on your laptop, no robot needed.

`--backend reachy` — the real Reachy Mini via the `reachy_mini` SDK. Speaking uses
Piper audio + `enable_wobbling()` for a talking motion; the antennas/head show state.

## Install

```bash
cd reachy_app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# macOS local backend needs PortAudio for sounddevice:  brew install portaudio
```

On the **robot**, also install the SDK there: `pip install reachy_mini` (or per Pollen's
setup). The robot backend imports it lazily, so the Mac install stays light.

## Configure

```bash
cp .env.example .env
# On the Pi, set CONNECTOR_URL to the Mac's LAN IP, e.g. http://192.168.1.20:8080
```

## Run

```bash
# On the Mac — full loop with your laptop mic/speakers:
python -m reachy_app.main --backend local --no-wakeword

# On the Reachy Mini:
python -m reachy_app.main --backend reachy
```

Then open the **hold-to-talk page** from your phone (same LAN):
`http://<this-host>:8081/` — press and hold while you speak, release when done.
If `BUTTON_TOKEN` is set, open it as `http://<this-host>:8081/?token=<BUTTON_TOKEN>`
(the launcher prints the full URL). Likewise set `CONNECTOR_TOKEN` to match the
connector, or calls to it are rejected with 401.
The page is a small chat UI:
- a **live status indicator** — `Ready · Listening · Thinking · Speaking · error/offline`
  — driven by the real loop state (so it reflects wake-word turns too, not just button
  presses). Also at `GET /status` (`{"state":"idle"}`).
- a **conversation history** — each turn's transcript ("You") and reply ("Reachy") as
  chat bubbles, newest at the bottom. Also at `GET /history`
  (`{"seq":N,"turns":[{"you":...,"reply":...}]}`). In-memory; resets on restart.

Flags: `--connector-url URL`, `--no-button`, `--no-wakeword`, `--backend {local,reachy}`.

## Activation

- **Phone button** — always available, no dependencies. Release IS the end-of-speech
  signal, so no VAD needed on this path. This is the reliable-in-noise default.
- **Wake word ("Hey Reachy")** — optional, off until you provide a Picovoice access
  key + a `.ppn` keyword file (generate "Hey Reachy" in the Picovoice console). Set
  `PICOVOICE_ACCESS_KEY` and `PORCUPINE_KEYWORD_PATH`, then `pip install pvporcupine`.
  Its end-of-speech uses the RMS `SilenceEndpointer` (dependency-free; silero is a
  drop-in upgrade later). It's muted while Reachy sends/speaks so it can't hear itself.

## Test (no robot, no mic)

The server must be running (`../server`, on :8080). Then:

```bash
source .venv/bin/activate
python -m reachy_app.tests.test_smoke
```

Covers WAV helpers, the silence endpointer, the button server endpoints, and a full
`ConversationLoop` turn through a `FakeBackend` against the real Mac server (canned
utterance → STT → Claude → TTS → playback, asserting the gesture order).

## Files

- `main.py` — CLI entry; wires backend + button + wake word + loop.
- `loop.py` — the turn-taking state machine.
- `audio.py` — `AudioBackend` + `LocalAudioBackend` (Mac) + `ReachyMiniBackend` (robot).
- `connector_client.py` — POST WAV to the Mac `/chat`, get reply WAV.
- `button_server.py` + `static/index.html` — the LAN hold-to-talk page.
- `wakeword.py` — Porcupine wrapper (no-op until configured).
- `vad.py` — RMS trailing-silence end-of-speech detector.
- `config.py` — env-driven settings (`.env.example`).
- `../reachy_claude_connector/main.py` — dashboard entry shim (carries the scrapeable
  `custom_app_url`; re-exports `app.py`'s `ReachyClaudeConnectorApp`).

## On-robot follow-ups (not yet hardware-tested)

The robot backend is written against Pollen's documented SDK API but hasn't run on
hardware from here. Worth checking on the first real run:
- antenna/head pose magnitudes in `ReachyMiniBackend` gestures (tune to taste);
- feeding the **robot** mic into Porcupine instead of a separate sounddevice stream
  (v1 opens its own stream — fine on the Mac, revisit on the Pi);
- echo handling: wake word is muted during playback, but acoustic echo cancellation
  is a later improvement.
