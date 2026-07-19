# reachy_claude_connector/main.py
"""Dashboard entry shim — the file the Reachy daemon scrapes.

The daemon discovers a packaged app by matching the entry-point NAME to a
site-packages folder and regex-scraping `custom_app_url` from that folder's
`main.py` (it never imports the file). Our real code lives in `reachy_app/`, so
this tiny package exists only to satisfy that convention:

  * it declares `custom_app_url` as a plain, text-scrapeable module constant, and
  * it re-exports the real app class so `python -m reachy_claude_connector.main`
    runs it.

`_get_instance_path()` resolves the *class's* module (`reachy_app.app`), so the
app's `static/` still resolves to `reachy_app/static/` — nothing about the UI or
the runtime moves. Keep this file import-light and the assignment below a plain
string literal so the daemon's regex keeps matching.
"""
from __future__ import annotations

from reachy_app.app import ReachyClaudeConnectorApp  # re-export for the entry point

# Scraped verbatim by the daemon from site_packages/reachy_claude_connector/main.py.
# Must match reachy_app.app.ReachyClaudeConnectorApp.custom_app_url.
custom_app_url = "http://0.0.0.0:8042"

if __name__ == "__main__":
    app = ReachyClaudeConnectorApp()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
