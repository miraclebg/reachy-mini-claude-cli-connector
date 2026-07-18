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

import hmac
import logging
import os
import tempfile
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from config import settings
from claude_client import ClaudeClient, ClaudeError
from stt import STT
from tts import TTS, TTSError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("connector")

app = FastAPI(title="Reachy Mini <-> Claude Code connector")


def _request_token(request: Request) -> str:
    """Read the bearer/`X-Auth-Token` credential off a request."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-auth-token", "")


# Auth is important here because Claude runs with command-execution permissions and
# the server may listen on 0.0.0.0. /health stays open for readiness probes (it
# exposes only config metadata, no conversation content or command surface).
_OPEN_PATHS = {"/health"}


@app.middleware("http")
async def require_token(request: Request, call_next):
    if settings.connector_token and request.url.path not in _OPEN_PATHS:
        if not hmac.compare_digest(_request_token(request), settings.connector_token):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)

# --- init the pipeline once ---
os.makedirs(settings.claude_working_dir, exist_ok=True)

stt = STT(settings.whisper_model, settings.whisper_device, settings.whisper_compute,
          settings.whisper_language, settings.whisper_vad_filter, settings.whisper_vad_threshold)

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
    empty_reply=settings.msg_error,
)

# TTS is optional at startup so you can test /chat/text before setting up a voice.
try:
    tts: TTS | None = TTS(settings.piper_model)
except TTSError as e:
    log.warning("TTS not ready: %s  (/chat/text still works; set PIPER_MODEL for audio)", e)
    tts = None

if settings.connector_token:
    log.info("auth: token required on all endpoints except /health")
else:
    log.warning(
        "auth: CONNECTOR_TOKEN not set — endpoints are OPEN. Anyone who can reach "
        "%s:%s can drive Claude (command execution under '%s'). Set CONNECTOR_TOKEN.",
        settings.host, settings.port, settings.permission_mode,
    )


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
        data = await audio.read()
        with open(in_path, "wb") as fh:
            fh.write(data)

        # (debug) keep the raw utterance for inspection if enabled
        if settings.debug_audio_dir:
            try:
                os.makedirs(settings.debug_audio_dir, exist_ok=True)
                import time as _t
                name = _t.strftime("utt-%H%M%S") + f"-{len(data)}.wav"
                with open(os.path.join(settings.debug_audio_dir, name), "wb") as dbg:
                    dbg.write(data)
                log.info("saved debug audio: %s", name)
            except OSError as e:
                log.warning("could not save debug audio: %s", e)

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
