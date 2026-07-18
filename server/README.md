# Connector server (runs on the Mac)

Audio in → **STT** (faster-whisper) → **Claude Code CLI** (with tools) → **TTS** (Piper) → audio out.
This is the "brain" half of the project. The Reachy Mini app (the thin half) POSTs
audio here and plays back what it gets. See `../DESIGN.md` for the whole picture.

## Prerequisites

- **Claude Code** installed and logged in on this Mac (`claude` on your PATH; run
  `claude` once interactively to confirm you're authenticated). We deliberately do
  *not* run in `--bare` mode, so the connector inherits your existing login and setup.
- **Python 3.12+**.
- **ffmpeg** (optional, only for recording a test clip).

## Install

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Then get a Piper voice (needed only for spoken audio — the text endpoint works without it):

- `pip install piper-tts` already gave you the `piper` command (it's in requirements).
- Download a voice — two files, `<voice>.onnx` and `<voice>.onnx.json` — from the
  **`rhasspy/piper-voices`** repo on Hugging Face. A good default is
  `en_US-lessac-medium`. Put both files somewhere (e.g. `../voices/`) and point
  `PIPER_MODEL` at the `.onnx`.

## Configure

```bash
cp .env.example .env
# edit .env — at minimum set PIPER_MODEL to your voice's .onnx path
```

## Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Test it standalone (no robot)

```bash
# 1) Claude loop only — no audio, no Piper needed:
python smoke_test.py --text "hey, introduce yourself in one sentence"

# 2) Full audio loop. Record 3s from your mic, then run it through:
ffmpeg -f avfoundation -i ":0" -t 3 -ar 16000 -ac 1 sample.wav
python smoke_test.py --wav sample.wav      # prints transcript + reply, saves reply.wav

# 3) Multi-turn memory — ask a follow-up and Claude should remember:
python smoke_test.py --text "my name is Martin"
python smoke_test.py --text "what's my name?"
python smoke_test.py --reset --text "what's my name?"   # after reset it won't know

# Or just curl:
curl -s localhost:8080/health | jq
curl -s localhost:8080/chat/text -H 'content-type: application/json' \
     -d '{"text":"what time is it where you are?"}' | jq
```

## Permission posture (default = `auto`)

The connector runs Claude with `--permission-mode auto`, which **auto-approves tool
calls** (no human to click "approve" in a voice loop). In practice:

- ✅ Claude can **read/search/edit files, run shell commands, and search the web**
  (`WebSearch`,`WebFetch` are in the allow-list, so Reachy can answer live questions
  like weather). It just does the thing and answers.
- 🛑 The **deny list is the guardrail** — `Bash(rm *)`, `Bash(sudo *)`, `Bash(curl *)`,
  `Bash(wget *)`, `Bash(git push *)`. Deny rules **always win, even under `auto`**
  (verified: an `rm` request is blocked). Harden this list to taste in `.env`.
- ⚠️ **Trust model:** anyone who can speak to the robot can run commands on this Mac
  (minus the deny list). Fine on your own machine; know what you're exposing.

Switch postures without code changes:
- `CLAUDE_PERMISSION_MODE=dontAsk` (or `make run PERMISSION=dontAsk`) → read-only:
  denies anything not in `CLAUDE_ALLOWED_TOOLS`, without prompting. A denied action
  doesn't crash the turn; Claude just says it couldn't.
- `plan` → planning only, no side effects.

Everything runs inside `../claude-workspace/` (the session dir for `--resume`), but
note that under `auto` shell commands can still reach outside it — the deny list, not
the working dir, is the real boundary.

Replies are passed through `clean_for_speech()` (strips citations/URLs that web search
adds) so TTS never reads a link aloud.

**v2** will move to the Python Agent SDK and add a **spoken** tool-approval callback,
so Reachy can ask out loud before anything destructive instead of relying only on the
deny list.

## Files

- `main.py` — FastAPI app + endpoints
- `claude_client.py` — the `claude -p` integration (session threading, voice prompt, permissions)
- `stt.py` — faster-whisper wrapper
- `tts.py` — Piper wrapper (swap the one `synthesize()` if your piper flags differ)
- `config.py` — env-driven settings
- `smoke_test.py` — standalone tester
