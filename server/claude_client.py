# server/claude_client.py
"""Talks to Claude via the `claude -p` (headless) CLI.

Why the CLI and not an HTTP model call: this is Claude *Code*, so it runs the full
agent loop with tool access on this machine — the whole point of the project.

Key behaviours:
  * `--output-format json` gives us `.result` (the text to speak) and `.session_id`.
  * We capture the session id on the first turn and pass `--resume <id>` on every
    following turn, so Claude remembers the conversation. Session lookup is scoped
    to the working directory, so every call runs with the same cwd.
  * `--append-system-prompt` tunes Claude for *speech* (short, no markdown), while
    keeping its normal Claude Code behaviour.
  * v1 permission posture is read-only (see config / README). A denied tool does
    NOT crash the run under `dontAsk`; Claude just adapts its reply. So a non-zero
    exit is a real error (auth, timeout, max-turns), not a denied tool.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess

log = logging.getLogger("connector.claude")

# Web search makes Claude append citations / URLs, which sound terrible read aloud.
# Strip them so TTS never speaks a link. Belt-and-suspenders to the system prompt.
_SOURCES_RE = re.compile(r"\n+\s*(sources?|references?)\s*:.*$", re.IGNORECASE | re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)")  # [text](url) -> text
_BARE_URL_RE = re.compile(r"https?://\S+")


def clean_for_speech(text: str) -> str:
    text = _SOURCES_RE.sub("", text)          # drop a trailing "Sources:" block
    text = _MD_LINK_RE.sub(r"\1", text)        # keep link text, drop the URL
    text = _BARE_URL_RE.sub("", text)          # remove any stray bare URLs
    return text.strip()

# Spoken-output tuning. Appended to Claude Code's own system prompt.
VOICE_SYSTEM_PROMPT = (
    "You are Reachy, a small desktop robot with a physical body, antennas, and a "
    "voice. ALWAYS reply in Bulgarian (говори само на български) — every response must "
    "be in natural, conversational Bulgarian, no matter what language you are addressed "
    "in. Use Cyrillic script; never transliterate. "
    "Everything you say is spoken aloud through a speaker, so speak the way a "
    "person talks: usually one to three short sentences, plain and conversational. "
    "Never use markdown, headings, bullet points, code blocks, tables, or emoji — "
    "they sound like noise when read aloud. If a full answer would be long, give the "
    "short version and offer to go deeper. "
    "You have tools: you can search the web and read pages, and you can act on this "
    "computer. For anything about current events, weather, prices, or facts you're "
    "unsure of, quietly look it up with web search and answer naturally — do NOT say "
    "you lack internet access or real-time data; you have both. Just don't read out "
    "URLs or sources aloud. If a tool genuinely fails or an action is blocked, say so "
    "briefly."
)


class ClaudeError(RuntimeError):
    pass


class ClaudeClient:
    def __init__(
        self,
        *,
        working_dir: str,
        claude_bin: str = "claude",
        model: str = "",
        effort: str = "",
        permission_mode: str = "dontAsk",
        allowed_tools: str = "Read,Glob,Grep",
        disallowed_tools: str = "",
        max_turns: int = 6,
        timeout_s: int = 120,
        empty_reply: str = "Sorry, I didn't come up with anything to say.",
    ) -> None:
        self.empty_reply = empty_reply
        self.working_dir = working_dir
        self.claude_bin = claude_bin
        self.model = model
        self.effort = effort
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.max_turns = max_turns
        self.timeout_s = timeout_s
        self.session_id: str | None = None  # threaded across turns

    def _build_cmd(self, prompt: str) -> list[str]:
        cmd = [
            self.claude_bin,
            "-p", prompt,
            "--output-format", "json",
            "--permission-mode", self.permission_mode,
            "--append-system-prompt", VOICE_SYSTEM_PROMPT,
            "--max-turns", str(self.max_turns),
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if self.allowed_tools:
            cmd += ["--allowedTools", self.allowed_tools]
        if self.disallowed_tools:
            cmd += ["--disallowedTools", self.disallowed_tools]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        return cmd

    def ask(self, prompt: str) -> str:
        """Send one user turn, return Claude's spoken reply text."""
        cmd = self._build_cmd(prompt)
        log.info("claude ask (session=%s): %r", self.session_id, prompt)
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except FileNotFoundError as e:
            raise ClaudeError(
                f"Could not find the '{self.claude_bin}' binary. Is Claude Code "
                f"installed and on PATH?"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise ClaudeError(f"Claude timed out after {self.timeout_s}s") from e

        if proc.returncode != 0:
            raise ClaudeError(
                (proc.stderr or proc.stdout or "claude exited non-zero").strip()
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ClaudeError(f"Could not parse Claude JSON output: {proc.stdout[:400]}") from e

        # Thread the conversation.
        new_sid = data.get("session_id")
        if new_sid:
            self.session_id = new_sid

        reply = clean_for_speech(data.get("result") or "")
        if not reply:
            reply = self.empty_reply
        log.info("claude reply: %r", reply)
        return reply

    def reset(self) -> None:
        """Forget the current conversation; next ask() starts fresh."""
        log.info("resetting session (was %s)", self.session_id)
        self.session_id = None
