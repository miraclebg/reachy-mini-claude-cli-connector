# server/main.py
"""FastAPI connector: audio in -> STT -> Claude (with tools) -> TTS -> audio out.

Run it (from this folder):
    uvicorn main:app --host 0.0.0.0 --port 8080

Endpoints:
    GET  /health       -> readiness + current session id
    POST /chat/text    -> JSON {"text": ...}  (test the Claude loop with no audio)
    POST /chat         -> multipart WAV in, WAV out (the real robot path)
    POST /reset        -> forget the conversation, start fresh
"""
from __future__ import annotations

import logging
import os
import tempfile
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response

from config import settings
from claude_client import ClaudeClient, ClaudeError
from stt import STT
from tts import TTS, TTSError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("connector")

app = FastAPI(title="Reachy Mini <-> Claude Code connector")

# --- init the pipeline once ---
os.makedirs(settings.claude_working_dir, exist_ok=True)

stt = STT(settings.whisper_model, settings.whisper_device, settings.whisper_compute, settings.whisper_language)

claude = ClaudeClient(
    working_dir=settings.claude_working_dir,
    claude_bin=settings.claude_bin,
    model=settings.claude_model,
    effort=settings.claude_effort,
    permission_mode=settings.permission_mode,
    allowed_tools=settings.allowed_tools,
    disallowed_tools=settings.disallowed_tools,
    max_turns=settings.max_turns,
    timeout_s=settings.claude_timeout_s,
)

# TTS is optional at startup so you can test /chat/text before setting up a voice.
try:
    tts: TTS | None = TTS(settings.piper_model)
except TTSError as e:
    log.warning("TTS not ready: %s  (/chat/text still works; set PIPER_MODEL for audio)", e)
    tts = None


@app.get("/health")
def health():
    return {
        "ok": True,
        "session_id": claude.session_id,
        "stt_model": settings.whisper_model,
        "tts_ready": tts is not None,
        "model": settings.claude_model or "(cli default)",
        "effort": settings.claude_effort or "(cli default)",
        "permission_mode": settings.permission_mode,
        "allowed_tools": settings.allowed_tools,
    }


@app.post("/chat/text")
def chat_text(payload: dict):
    text = (payload or {}).get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Provide {'text': ...}")
    try:
        reply = claude.ask(text)
    except ClaudeError as e:
        raise HTTPException(status_code=502, detail=f"Claude error: {e}")
    return {"reply": reply, "session_id": claude.session_id}


@app.post("/chat")
async def chat(audio: UploadFile = File(...)):
    if tts is None:
        raise HTTPException(status_code=503, detail="TTS not configured (set PIPER_MODEL).")

    # 1) save the uploaded audio to a temp file for faster-whisper
    suffix = os.path.splitext(audio.filename or "")[1] or ".wav"
    fd, in_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        with open(in_path, "wb") as fh:
            fh.write(await audio.read())

        # 2) STT
        transcript = stt.transcribe(in_path)

        # 3) if we heard nothing, answer without bothering Claude
        if not transcript:
            reply = "Sorry, I didn't catch that."
        else:
            try:
                reply = claude.ask(transcript)
            except ClaudeError as e:
                log.error("Claude error: %s", e)
                reply = "Sorry, I had trouble thinking about that."

        # 4) TTS
        try:
            wav = tts.synthesize(reply)
        except TTSError as e:
            raise HTTPException(status_code=502, detail=f"TTS error: {e}")

        # Return audio, with transcript/reply in headers for easy debugging.
        return Response(
            content=wav,
            media_type="audio/wav",
            headers={
                "X-Transcript": quote(transcript),
                "X-Reply": quote(reply),
            },
        )
    finally:
        try:
            os.unlink(in_path)
        except OSError:
            pass


@app.post("/reset")
def reset():
    claude.reset()
    return {"ok": True, "session_id": claude.session_id}
