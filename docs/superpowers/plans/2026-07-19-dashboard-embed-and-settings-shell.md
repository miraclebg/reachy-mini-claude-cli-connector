# Dashboard Embed + Settings Shell (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the connector app auto-embed in the Reachy **desktop** app (like other apps) and give its web UI a `Talk | Settings` shell — the foundation the live config (Phase 2) and server discovery (Phase 3) build on.

**Architecture:** The daemon discovers a packaged app by matching the entry-point **name** to a site-packages folder and text-scraping `custom_app_url` from that folder's `main.py` (it never imports it). Our code lives in `reachy_app/` but the entry-point name is `reachy_claude_connector`, so the scrape finds nothing and the desktop app never embeds us. Fix: add a tiny **entry-shim package** `reachy_claude_connector/` that carries a scrapeable `custom_app_url` and re-exports the real app class (whose `__module__` still resolves `static/` to `reachy_app/static/`). Then restructure the single static page into a segmented `Talk | Settings` shell.

**Tech Stack:** Python 3.10+ (robot venv is 3.12), setuptools, `reachy_mini` SDK (robot-only, lazily imported — untouched here), vanilla HTML/CSS/JS (no framework). Tests: the repo's custom runner `reachy_app/tests/test_smoke.py` (not pytest).

## Global Constraints

- **This phase is packaging + static front-end only.** No new runtime dependencies; `reachy_app/` Python modules and their **relative** imports are NOT churned.
- **`custom_app_url` stays `http://0.0.0.0:8042`** verbatim (the desktop app rewrites the host; only the port matters).
- **The app class stays `reachy_app.app.ReachyClaudeConnectorApp`.** The shim re-exports it; it is never redefined (so `_get_instance_path()` keeps resolving `static/` to `reachy_app/static/`).
- **Tests live in `reachy_app/tests/test_smoke.py`**: add a test *function*, then register it in the `main()` runner tuple. Run with `python -m reachy_app.tests.test_smoke` from the repo root (robot venv). `check(name, cond, extra)` is the assertion helper; a run exits non-zero if any check fails.
- **Dashboard app name stays `reachy_claude_connector`** (entry-point key unchanged).
- **Commits:** lowercase-prefixed subject (`feat:` / `docs:`), and append the `Claude-Session:` trailer per the session's CLAUDE guidance.
- Bulgarian STT/TTS and the whole Mac pipeline are untouched by this phase.

---

### Task 1: Dashboard entry shim (the embed fix)

**Files:**
- Create: `reachy_claude_connector/__init__.py`
- Create: `reachy_claude_connector/main.py`
- Modify: `pyproject.toml:23-34` (entry-point value + `packages.find`)
- Modify: `reachy_app/tests/test_smoke.py` (add `test_entry_shim_scrapeable`, register in `main()`)
- Modify: `INSTALL.md` (launch command + "How it works"); `reachy_app/README.md` (files map)

**Interfaces:**
- Consumes: `reachy_app.app.ReachyClaudeConnectorApp` (existing class, unchanged).
- Produces: importable module `reachy_claude_connector.main` exposing `ReachyClaudeConnectorApp` and a module-level `custom_app_url = "http://0.0.0.0:8042"`; a `python -m reachy_claude_connector.main` run path.

- [ ] **Step 1: Write the failing test**

Add to `reachy_app/tests/test_smoke.py` (near the other `test_*` functions, e.g. after `test_button_auth`). It replicates the daemon's exact scrape (`local_common_venv.py`) against our shim file:

```python
def test_entry_shim_scrapeable() -> None:
    print("embed: daemon can scrape custom_app_url from the entry shim")
    import os
    import re
    # The daemon reads site_packages/<entry-point-name>/main.py and regex-scrapes it
    # WITHOUT importing. Our entry-point name is `reachy_claude_connector`; mirror the
    # same file from the source tree (…/reachy_app/tests/test_smoke.py -> repo root).
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    shim = os.path.join(root, "reachy_claude_connector", "main.py")
    check("entry shim main.py exists", os.path.exists(shim), shim)
    text = open(shim, encoding="utf-8").read() if os.path.exists(shim) else ""
    # This pattern is copied verbatim from the daemon's _get_custom_app_url_from_file().
    m = re.search(r"""custom_app_url\s*(?::\s*[^=]+)?\s*=\s*["']([^"']+)["']""", text)
    check("custom_app_url is scrapeable", bool(m), "no regex match")
    check("scrapes to :8042", (m.group(1) if m else "") == "http://0.0.0.0:8042",
          m.group(1) if m else "<none>")
```

Register it in the `main()` runner tuple (add after `test_button_auth`):

```python
        test_wav_roundtrip, test_endpointer, test_button_server, test_button_auth,
        test_entry_shim_scrapeable,
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A3 "entry shim"`
Expected: FAIL — `❌ entry shim main.py exists` (the `reachy_claude_connector/` package doesn't exist yet), and the run ends `… failed` with a non-zero exit.

- [ ] **Step 3: Create the entry-shim package**

Create `reachy_claude_connector/__init__.py`:

```python
# reachy_claude_connector — dashboard entry shim (see main.py).
```

Create `reachy_claude_connector/main.py`:

```python
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
```

- [ ] **Step 4: Point the entry point at the shim and package it**

In `pyproject.toml`, change the entry-point **value** (keep the **key** `reachy_claude_connector`) and add the shim to the discovered packages.

Replace lines 23-24:

```toml
[project.entry-points."reachy_mini_apps"]
reachy_claude_connector = "reachy_app.app:ReachyClaudeConnectorApp"
```

with:

```toml
[project.entry-points."reachy_mini_apps"]
# The KEY (app name shown in the dashboard) stays `reachy_claude_connector`; the daemon
# also scrapes custom_app_url from site_packages/reachy_claude_connector/main.py — the shim.
reachy_claude_connector = "reachy_claude_connector.main:ReachyClaudeConnectorApp"
```

Replace line 30 (inside `[tool.setuptools.packages.find]`):

```toml
include = ["reachy_app*"]
```

with:

```toml
include = ["reachy_app*", "reachy_claude_connector*"]
```

(Leave `exclude = ["reachy_app.tests*"]` and `[tool.setuptools.package-data] reachy_app = ["static/*"]` unchanged — static stays in `reachy_app/`.)

- [ ] **Step 5: Verify the shim compiles, scrapes, and nothing regressed**

Run: `python -m py_compile reachy_claude_connector/main.py reachy_claude_connector/__init__.py && echo COMPILE_OK`
Expected: `COMPILE_OK`

Run: `python -m reachy_app.tests.test_smoke 2>&1 | tail -20`
Expected: the three `embed:` checks pass (`✅ entry shim main.py exists`, `✅ custom_app_url is scrapeable`, `✅ scrapes to :8042`) and the summary shows `0 failed` (the `full loop turn` test may print `⏭ SKIPPED` if the connector isn't running on :8080 — that is not a failure).

- [ ] **Step 6: Update the docs that describe the launch path**

In `INSTALL.md`, the maintainer notes still say the manager launches `python -m reachy_app.app`. Update them.

Replace (around line 123):

```
  how the dashboard discovers installed apps. The manager launches it as
  `/venvs/apps_venv/bin/python -m reachy_app.app`.
```

with:

```
  how the dashboard discovers installed apps. The manager launches it as
  `/venvs/apps_venv/bin/python -m reachy_claude_connector.main` (a thin entry shim; the
  real app is `reachy_app/app.py`). The shim exists because the daemon locates an app by
  matching the entry-point *name* to a site-packages folder and scrapes `custom_app_url`
  from that folder's `main.py` — so the folder name must equal the entry-point name
  `reachy_claude_connector`, which `reachy_app/` did not. Without the shim the desktop app
  never learns we have a UI and won't embed it.
```

In `pyproject.toml`, update the stale comment on line 21 (`python -m reachy_app.app …`) to reference `reachy_claude_connector.main`.

In `reachy_app/README.md`, in the `## Files` list, add one line:

```
- `../reachy_claude_connector/main.py` — dashboard entry shim (carries the scrapeable
  `custom_app_url`; re-exports `app.py`'s `ReachyClaudeConnectorApp`).
```

- [ ] **Step 7: Commit**

```bash
git add reachy_claude_connector/ pyproject.toml reachy_app/tests/test_smoke.py INSTALL.md reachy_app/README.md
git commit -m "feat: dashboard entry shim so the desktop app embeds our UI"
```

---

### Task 2: `Talk | Settings` UI shell

**Files:**
- Modify: `reachy_app/static/index.html` (restructure into a two-tab shell; read dashboard theme params)
- Modify: `reachy_app/tests/test_smoke.py` (add `test_shell_tabs`, register in `main()`)

**Interfaces:**
- Consumes: the existing `/status`, `/history`, `/press`, `/release` routes (unchanged) and the `?token=` / dashboard `?embedded=1&theme=…&accent=…&bg=…&fg=…` query params.
- Produces: a page containing `data-tab="talk"` and `data-tab="settings"` panels, a segmented nav, a Settings placeholder, and theme-param application — the shell Phase 2/3 populate.

- [ ] **Step 1: Write the failing test**

Add to `reachy_app/tests/test_smoke.py` (after `test_button_server`). `ButtonServer` serves the same `static/index.html`, so this asserts the restructured markup:

```python
def test_shell_tabs() -> None:
    print("shell: page has Talk|Settings nav, keeps hold-to-talk, reads theme param")
    srv = ButtonServer("127.0.0.1", 8097)
    srv.start()
    time.sleep(0.2)
    try:
        page = urllib.request.urlopen("http://127.0.0.1:8097/", timeout=2).read().decode()
        check("hold-to-talk preserved", "Hold" in page and "/press" in page)
        check("has Talk tab panel", 'data-tab="talk"' in page, "")
        check("has Settings tab panel", 'data-tab="settings"' in page, "")
        check("reads the dashboard theme param", '"theme"' in page or "'theme'" in page, "")
    finally:
        srv.stop()
```

Register it in the `main()` runner tuple (add right after `test_button_server`):

```python
        test_wav_roundtrip, test_endpointer, test_button_server, test_shell_tabs,
        test_button_auth, test_entry_shim_scrapeable,
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | grep -A4 "shell:"`
Expected: FAIL — `❌ has Talk tab panel` / `❌ has Settings tab panel` (current `index.html` has no tabs).

- [ ] **Step 3: Restructure `reachy_app/static/index.html`**

Replace the entire file with the two-tab shell below. It keeps the existing status chip, history log, and hold-to-talk exactly as they behaved; adds a segmented `Talk | Settings` nav (design choice B), a Settings placeholder, tab switching, and dashboard theme-param application. The hold-to-talk footer shows only on the Talk tab.

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no" />
<meta name="theme-color" content="#0e0f12" />
<title>Reachy</title>
<style>
  :root {
    --orange: #FF9900; --green: #17b877; --amber: #f5a623;
    --blue: #3b9dff; --red: #ff5a5a; --grey: #8a8f98;
    --bg: #0e0f12; --card: #1a1c22; --line: #2a2d36; --fg: #eaeaea;
  }
  :root[data-theme="light"] {
    --bg: #f6f7f9; --card: #ffffff; --line: #e2e4ea; --fg: #1c1e22; --grey: #9aa0a8;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    display: flex; flex-direction: column;
    background: var(--bg); color: var(--fg);
    font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
    -webkit-user-select: none; user-select: none;
  }

  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: .8rem 1rem; border-bottom: 1px solid var(--line); flex: 0 0 auto;
  }
  header .brand { font-weight: 600; font-size: 1rem; opacity: .85; }
  #status {
    display: flex; align-items: center; gap: .5rem;
    padding: .35rem .8rem; border-radius: 999px;
    background: var(--card); border: 1px solid var(--line);
    font-size: .85rem; font-weight: 500; transition: color .2s, border-color .2s;
  }
  #dot { width: .7rem; height: .7rem; border-radius: 50%; background: var(--grey); flex: 0 0 auto; }
  #status[data-state="idle"]           { color: var(--fg); }
  #status[data-state="listening"]      { color: #7ef0bd; border-color: #1c5f45; }
  #status[data-state="listening"] #dot { background: var(--green); animation: pulse 1s ease-in-out infinite; }
  #status[data-state="thinking"]       { color: #ffd98a; border-color: #6b5320; }
  #status[data-state="thinking"] #dot  { background: var(--amber); animation: blink .7s linear infinite; }
  #status[data-state="speaking"]       { color: #a9d6ff; border-color: #24557f; }
  #status[data-state="speaking"] #dot  { background: var(--blue); animation: pulse .55s ease-in-out infinite; }
  #status[data-state="error"]          { color: #ff9d9d; border-color: #7a2b2b; }
  #status[data-state="error"] #dot     { background: var(--red); }
  #status[data-state="offline"] #dot   { background: #555; }
  @keyframes pulse { 0%,100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.5); opacity: .6; } }
  @keyframes blink { 0%,100% { opacity: 1; } 50% { opacity: .25; } }

  /* segmented Talk | Settings nav */
  nav.seg { display: flex; gap: .3rem; margin: .7rem 1rem 0; padding: .25rem;
            background: var(--card); border: 1px solid var(--line); border-radius: .8rem; flex: 0 0 auto; }
  .seg-btn { flex: 1; border: none; background: transparent; color: var(--fg);
             font: inherit; font-weight: 600; padding: .5rem 0; border-radius: .6rem;
             cursor: pointer; opacity: .7; }
  .seg-btn.on { background: var(--orange); color: #fff; opacity: 1; }

  main { flex: 1 1 auto; display: flex; flex-direction: column; overflow: hidden; }
  .tab { flex: 1 1 auto; display: flex; flex-direction: column; overflow: hidden; }
  .tab.hidden { display: none; }

  #log { flex: 1 1 auto; overflow-y: auto; padding: 1rem; display: flex; flex-direction: column; gap: .6rem; }
  #empty { margin: auto; opacity: .35; font-size: .95rem; text-align: center; }
  .msg { max-width: 82%; padding: .6rem .85rem; border-radius: 1rem; font-size: 1rem; line-height: 1.35;
         white-space: pre-wrap; word-wrap: break-word; }
  .msg.you    { align-self: flex-end; background: #3a2c12; border: 1px solid #5a4520; border-bottom-right-radius: .3rem; }
  .msg.reachy { align-self: flex-start; background: var(--card); border: 1px solid var(--line); border-bottom-left-radius: .3rem; }
  .msg .who { display: block; font-size: .7rem; opacity: .5; margin-bottom: .15rem; }

  .placeholder { margin: auto; padding: 2rem; text-align: center; opacity: .5; font-size: .95rem; line-height: 1.5; }

  footer { flex: 0 0 auto; padding: .9rem 1rem calc(.9rem + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); }
  footer.hidden { display: none; }
  #talk {
    width: 100%; height: 4rem; border: none; border-radius: 1.2rem; cursor: pointer;
    color: #fff; font-size: 1.2rem; font-weight: 600; letter-spacing: .02em; touch-action: none;
    background: linear-gradient(180deg, #ffb340, var(--orange));
    box-shadow: 0 6px 22px rgba(255,153,0,.22);
    transition: transform .08s ease, box-shadow .15s, background .15s;
  }
  #talk.holding {
    background: linear-gradient(180deg, #3fe0a0, var(--green));
    box-shadow: 0 0 0 6px rgba(23,184,119,.15), 0 6px 22px rgba(23,184,119,.3);
    transform: scale(.99);
  }
</style>
</head>
<body>
  <header>
    <span class="brand">🤖 Reachy</span>
    <div id="status" data-state="idle" role="status" aria-live="polite">
      <span id="dot"></span><span id="label">Ready</span>
    </div>
  </header>

  <nav class="seg">
    <button class="seg-btn on" data-tab="talk">Talk</button>
    <button class="seg-btn" data-tab="settings">Settings</button>
  </nav>

  <main>
    <section id="tab-talk" class="tab" data-tab="talk">
      <div id="log"><div id="empty">Hold the button and say something to Reachy…</div></div>
    </section>
    <section id="tab-settings" class="tab hidden" data-tab="settings">
      <div class="placeholder">⚙️ Settings<br>Server connection & tuning arrive in the next update.</div>
    </section>
  </main>

  <footer id="talk-footer">
    <button id="talk">Hold to talk</button>
  </footer>

<script>
  // ---- dashboard theme params (?embedded=1&theme=dark|light&accent=RRGGBB&bg=..&fg=..) ----
  const Q = new URLSearchParams(location.search);
  (function applyTheme() {
    const theme = Q.get("theme");
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
    const setVar = (name, key, hash) => {
      const v = Q.get(key);
      if (v) document.documentElement.style.setProperty(name, (hash ? "#" : "") + v);
    };
    setVar("--accent", "accent", true); setVar("--orange", "accent", true);
    setVar("--bg", "bg", true); setVar("--fg", "fg", true);
  })();

  // ---- tab switching ----
  const footer = document.getElementById("talk-footer");
  document.querySelectorAll(".seg-btn").forEach(b => b.addEventListener("click", () => {
    const tab = b.dataset.tab;
    document.querySelectorAll(".seg-btn").forEach(x => x.classList.toggle("on", x === b));
    document.querySelectorAll("main .tab").forEach(s => s.classList.toggle("hidden", s.dataset.tab !== tab));
    footer.classList.toggle("hidden", tab !== "talk");
  }));

  // ---- hold-to-talk + live status/history (unchanged behaviour) ----
  const statusEl = document.getElementById("status");
  const labelEl  = document.getElementById("label");
  const logEl    = document.getElementById("log");
  const btn      = document.getElementById("talk");
  let held = false, lastSeq = -1;

  const TOKEN = Q.get("token") || "";
  const AUTH = TOKEN ? { "X-Auth-Token": TOKEN } : {};

  const LABELS = { idle: "Ready", listening: "Listening…", thinking: "Thinking…",
                   speaking: "Speaking…", error: "Error", offline: "Offline" };
  function renderStatus(state) { statusEl.dataset.state = state; labelEl.textContent = LABELS[state] || state; }

  async function post(path) {
    try { await fetch(path, { method: "POST", headers: AUTH, keepalive: true }); }
    catch (e) { renderStatus("offline"); }
  }
  function down(e) { e.preventDefault(); if (held) return;
    held = true; btn.classList.add("holding"); btn.textContent = "Listening…"; post("/press"); }
  function up(e) { if (e) e.preventDefault(); if (!held) return;
    held = false; btn.classList.remove("holding"); btn.textContent = "Hold to talk"; post("/release"); }
  btn.addEventListener("pointerdown", down);
  btn.addEventListener("pointerup", up);
  btn.addEventListener("pointerleave", up);
  btn.addEventListener("pointercancel", up);
  document.addEventListener("visibilitychange", () => { if (document.hidden) up(); });

  function bubble(cls, who, text) {
    const d = document.createElement("div"); d.className = "msg " + cls;
    const w = document.createElement("span"); w.className = "who"; w.textContent = who;
    d.appendChild(w); d.appendChild(document.createTextNode(text)); return d;
  }
  function renderHistory(turns) {
    const nearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 80;
    logEl.innerHTML = "";
    if (!turns.length) {
      const e = document.createElement("div"); e.id = "empty";
      e.textContent = "Hold the button and say something to Reachy…";
      logEl.appendChild(e); return;
    }
    for (const t of turns) {
      if (t.you)   logEl.appendChild(bubble("you", "You", t.you));
      if (t.reply) logEl.appendChild(bubble("reachy", "Reachy", t.reply));
    }
    if (nearBottom) logEl.scrollTop = logEl.scrollHeight;
  }

  async function pollStatus() {
    try { renderStatus((await (await fetch("/status", { headers: AUTH, cache: "no-store" })).json()).state); }
    catch (e) { renderStatus("offline"); }
  }
  async function pollHistory() {
    try {
      const data = await (await fetch("/history", { headers: AUTH, cache: "no-store" })).json();
      if (data.seq !== lastSeq) { lastSeq = data.seq; renderHistory(data.turns); }
    } catch (e) { /* keep last render */ }
  }
  setInterval(pollStatus, 350);
  setInterval(pollHistory, 1000);
  pollStatus(); pollHistory();
</script>
</body>
</html>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m reachy_app.tests.test_smoke 2>&1 | tail -20`
Expected: the `shell:` checks pass (`✅ hold-to-talk preserved`, `✅ has Talk tab panel`, `✅ has Settings tab panel`, `✅ reads the dashboard theme param`), the existing `button server` checks still pass, and the summary shows `0 failed`.

(Optional local eyeball, not required: `open reachy_app/static/index.html` — the Talk tab shows the hold-to-talk UI; the Settings toggle swaps to the placeholder and hides the button.)

- [ ] **Step 5: Commit**

```bash
git add reachy_app/static/index.html reachy_app/tests/test_smoke.py
git commit -m "feat: Talk | Settings shell for the embedded app UI"
```

---

## Self-Review

**Spec coverage (Phase 1 rows of the design):**
- "Embed fix so the dashboard auto-embeds us" → Task 1 (entry shim + pyproject + scrape-guard test). ✓
- "Restructure the page into an app UI with a Settings tab" → Task 2 (segmented `Talk | Settings`). ✓
- "The UI reads `?embedded=1&theme=…`" → Task 2 Step 3 `applyTheme()`. ✓
- `VERIFY-ON-DASHBOARD` (only fails on the robot/desktop daemon) → called out below; the scrape-guard test is the closest source-side proxy. ✓
- Deferred to later phases (correctly absent here): supervisor/worker, live config routes, discovery/beacon, PWA manifest.

**Placeholder scan:** No "TBD/TODO/handle appropriately". Every code step shows complete content; the Settings tab placeholder is an intentional, shipped Phase-1 state, not a plan gap.

**Type/name consistency:** `custom_app_url` string `http://0.0.0.0:8042` is identical in the shim, the class attribute, and the test's expected value. `test_entry_shim_scrapeable` and `test_shell_tabs` are both registered in the `main()` tuple. Entry-point key stays `reachy_claude_connector`; value → `reachy_claude_connector.main:ReachyClaudeConnectorApp`. Markers asserted by the test (`data-tab="talk"`, `data-tab="settings"`, `"theme"`) all appear verbatim in the Step-3 HTML.

## VERIFY-ON-DASHBOARD (needs the robot / macOS desktop app)

The scrape only *fails* on the robot / desktop-app daemon (a dev "Lite" daemon reads the class attribute directly). After installing this build into `apps_venv` and refreshing the desktop app:
1. `reachy_claude_connector` appears and, when started, its UI **auto-embeds** in the right panel (no manual navigation to `:8042`).
2. The embedded iframe picks up the dashboard theme (light/dark + accent).
3. The `Talk | Settings` toggle works inside the embed; hold-to-talk still drives the robot.

Confirm the entry point resolves in the app venv:
`/venvs/apps_venv/bin/python -c "from importlib.metadata import entry_points; e=[x for x in entry_points(group='reachy_mini_apps') if x.name=='reachy_claude_connector'][0]; print(e.value)"`
Expected: `reachy_claude_connector.main:ReachyClaudeConnectorApp`.

## Execution Handoff

Phases 2 (supervisor/worker + live config) and 3 (multi-server discovery) get their own plans once this lands — their exact steps depend on what Phase 1 establishes.
