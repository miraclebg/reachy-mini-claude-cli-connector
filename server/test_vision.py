import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vision import transcript_wants_vision, fetch_frame

TRIGGERS = ["виж", "погледни", "виждаш", "look", "see"]


def test_trigger_matches_plain():
    assert transcript_wants_vision("Рийчи, виж какво има", TRIGGERS)


def test_trigger_matches_inflected():
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
