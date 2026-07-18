#!/usr/bin/env python3
# server/smoke_test.py
"""Quick standalone test of the server — no robot required.

Text loop (tests Claude only):
    python smoke_test.py --text "hey, what can you do?"

Audio loop (tests STT -> Claude -> TTS), saves the spoken reply to reply.wav:
    python smoke_test.py --wav /path/to/some_speech.wav

No sample WAV handy? Record ~3s from your Mac mic with ffmpeg:
    ffmpeg -f avfoundation -i ":0" -t 3 -ar 16000 -ac 1 sample.wav
then:  python smoke_test.py --wav sample.wav
"""
import argparse
import os
import sys
from urllib.parse import unquote

import requests

BASE = "http://localhost:8080"
# Match the server's CONNECTOR_TOKEN (if it has auth on).
TOKEN = os.environ.get("CONNECTOR_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="send text straight to Claude")
    ap.add_argument("--wav", help="send a WAV through the full audio loop")
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--reset", action="store_true", help="reset the conversation first")
    args = ap.parse_args()

    if args.reset:
        print("reset:", requests.post(f"{args.base}/reset", headers=HEADERS).json())

    if args.text:
        r = requests.post(f"{args.base}/chat/text", json={"text": args.text}, headers=HEADERS, timeout=180)
        r.raise_for_status()
        print("reply:", r.json()["reply"])
        return 0

    if args.wav:
        with open(args.wav, "rb") as fh:
            r = requests.post(f"{args.base}/chat", files={"audio": ("in.wav", fh, "audio/wav")},
                              headers=HEADERS, timeout=180)
        r.raise_for_status()
        print("heard :", unquote(r.headers.get("X-Transcript", "")))
        print("reply :", unquote(r.headers.get("X-Reply", "")))
        with open("reply.wav", "wb") as out:
            out.write(r.content)
        print("saved -> reply.wav")
        return 0

    print("health:", requests.get(f"{args.base}/health", headers=HEADERS).json())
    print("(pass --text or --wav to actually test the loop)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
