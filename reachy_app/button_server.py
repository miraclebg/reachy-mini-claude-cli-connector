# reachy_app/button_server.py
"""Phone 'hold to talk' page, served on the LAN by the robot.

Stdlib http.server only — no FastAPI/uvicorn — to keep the Pi light. It runs in a
background thread and mutates a shared ButtonState:

    press  (pointerdown) -> hold  : start capturing
    release (pointerup)  -> end   : stop capturing (button release IS end-of-speech)

The main loop reads `take_press()` (a fresh press edge) to start a turn and
`is_held()` to know when to stop recording.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("reachy.button")

_STATIC = os.path.join(os.path.dirname(__file__), "static", "index.html")


class ButtonState:
    def __init__(self) -> None:
        self._held = threading.Event()
        self._press_edge = threading.Event()

    # -- called by the HTTP handler --
    def press(self) -> None:
        self._held.set()
        self._press_edge.set()

    def release(self) -> None:
        self._held.clear()

    # -- read by the main loop --
    def is_held(self) -> bool:
        return self._held.is_set()

    def take_press(self) -> bool:
        """True exactly once per fresh press (consumes the edge)."""
        if self._press_edge.is_set():
            self._press_edge.clear()
            return True
        return False


class StatusState:
    """Current phase of the conversation loop, shown live on the phone page.
    States: idle | listening | thinking | speaking | error."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "idle"

    def set(self, state: str) -> None:
        with self._lock:
            self._state = state

    def get(self) -> str:
        with self._lock:
            return self._state


class History:
    """Rolling record of conversation turns (what Reachy heard / said), shown as a
    chat history on the phone page. In-memory only; resets when the app restarts."""

    def __init__(self, maxlen: int = 100) -> None:
        self._lock = threading.Lock()
        self._turns: list[dict] = []
        self._maxlen = maxlen
        self._seq = 0  # increments each add, so the page can cheaply detect changes

    def add(self, you: str, reply: str) -> None:
        with self._lock:
            self._seq += 1
            self._turns.append({"n": self._seq, "you": you, "reply": reply})
            if len(self._turns) > self._maxlen:
                self._turns = self._turns[-self._maxlen:]

    def as_json(self, limit: int = 40) -> bytes:
        with self._lock:
            payload = {"seq": self._seq, "turns": self._turns[-limit:]}
        # ensure_ascii keeps Cyrillic as \uXXXX — valid JSON, JS decodes it fine.
        return json.dumps(payload).encode()


def _make_handler(state: ButtonState, status: StatusState, history: History):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # silence default stderr spam
            pass

        def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                try:
                    with open(_STATIC, "rb") as fh:
                        self._send(200, fh.read(), "text/html; charset=utf-8")
                except OSError:
                    self._send(200, _FALLBACK_PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/status":
                body = ('{"state":"%s"}' % status.get()).encode()
                self._send(200, body, "application/json")
            elif self.path == "/history":
                self._send(200, history.as_json(), "application/json")
            elif self.path == "/health":
                self._send(200, b'{"ok":true}', "application/json")
            else:
                self._send(404, b"not found")

        def do_POST(self) -> None:
            if self.path == "/press":
                state.press()
                self._send(200, b'{"ok":true,"state":"held"}', "application/json")
            elif self.path == "/release":
                state.release()
                self._send(200, b'{"ok":true,"state":"released"}', "application/json")
            else:
                self._send(404, b"not found")

    return Handler


class ButtonServer:
    def __init__(self, host: str, port: int) -> None:
        self.state = ButtonState()
        self.status = StatusState()
        self.history = History()
        self._httpd = ThreadingHTTPServer((host, port), _make_handler(self.state, self.status, self.history))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.host, self.port = host, port

    def start(self) -> None:
        self._thread.start()
        log.info("hold-to-talk page on http://%s:%d/", self.host, self.port)

    def stop(self) -> None:
        self._httpd.shutdown()


# Minimal page used if static/index.html is missing.
_FALLBACK_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Reachy</title><style>html,body{height:100%;margin:0}body{display:flex;align-items:center;
justify-content:center;background:#111;font-family:sans-serif}#b{width:70vw;height:70vw;max-width:340px;
max-height:340px;border-radius:50%;border:none;font-size:1.4rem;color:#fff;background:#c60;touch-action:none}
#b.on{background:#0a0}</style><button id=b>Hold to talk</button><script>
const b=document.getElementById('b');const P=p=>fetch(p,{method:'POST',keepalive:true});
const dn=e=>{e.preventDefault();b.classList.add('on');b.textContent='Listening…';P('/press')};
const up=e=>{e.preventDefault();b.classList.remove('on');b.textContent='Hold to talk';P('/release')};
b.addEventListener('pointerdown',dn);b.addEventListener('pointerup',up);
b.addEventListener('pointerleave',up);b.addEventListener('pointercancel',up);</script>"""
