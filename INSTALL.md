# Installing on the Reachy Mini (as a dashboard app)

This installs the connector as a **proper Reachy Mini app** so you can
**run / stop / uninstall** it from the Reachy dashboard (Mac / iPhone app), just
like the built-in apps. The robot side is the thin half; the "brain" (speech-to-text,
Claude, text-to-speech) runs on your Mac — see the main [README](README.md).

```
  Reachy Mini (Pi)                         Mac
  ┌─────────────────────────┐              ┌───────────────────────────┐
  │ reachy_claude_connector │  POST /chat  │ connector server (:8080)  │
  │  app  (apps_venv)       │ ───────────► │  STT → Claude → TTS       │
  │  hold-to-talk :8042     │ ◄─────────── │  (needs CONNECTOR_TOKEN)  │
  └─────────────────────────┘   reply WAV  └───────────────────────────┘
```

## Prerequisites

1. **The Mac connector must be running and reachable from the robot.**
   On the Mac: `make run` (or `make server`). It listens on `0.0.0.0:8080`.
   Make sure `CONNECTOR_TOKEN` is set in `server/.env` (see the main README) — note it,
   you'll need the same value on the robot. Verify the robot can reach it (below).
2. **Robot and Mac on the same LAN.** You'll need the Mac's IP (e.g. `10.10.9.33`) and
   the robot's IP (e.g. `10.10.9.29`).
3. SSH access to the robot (`ssh pollen@<robot-ip>`).

> The robot ships with two Python venvs under `/venvs`: `mini_daemon` (the daemon)
> and **`apps_venv`** (where all apps are installed). We install into `apps_venv`.

## Install (once)

SSH to the robot and run:

```bash
ssh pollen@<robot-ip>

# 1) Get the code
git clone https://github.com/miraclebg/reachy-mini-claude-cli-connector.git
cd reachy-mini-claude-cli-connector

# 2) Point the app at your Mac connector (this file is read by the installed app).
#    Use the SAME CONNECTOR_TOKEN as the Mac's server/.env.
mkdir -p ~/.config/reachy-mini-claude
cat > ~/.config/reachy-mini-claude/config.env <<'EOF'
CONNECTOR_URL=http://<mac-ip>:8080
CONNECTOR_TOKEN=<paste the same token as server/.env>
EOF

# 3) (optional) confirm the robot can reach the Mac connector
curl -s -o /dev/null -w '%{http_code}\n' http://<mac-ip>:8080/health   # expect 200

# 4) Install into the robot's apps venv
/venvs/apps_venv/bin/pip install .
```

That's it. The app now registers under the `reachy_mini_apps` entry point and shows up
in the dashboard as **reachy_claude_connector** (source: *installed*).

## Run / stop / uninstall

### From the Reachy dashboard (Mac / iPhone app) — the normal way
- Open the dashboard → **Apps** → find **reachy_claude_connector** under installed apps.
- **Run** it. When it's running, open its page (the app exposes a hold-to-talk UI).
- **Stop** it from the dashboard when done.
- **Uninstall** (Remove) it from the dashboard to delete it.

### To talk to it
With the app running, open the hold-to-talk page on your phone (same Wi-Fi):

```
http://<robot-ip>:8042/
```

Press and hold while you speak, release when done. The page shows a live status
indicator (Listening / Thinking / Speaking) and the conversation history. The robot
moves its antennas/head per state and wobbles while speaking.

### Equivalent commands (if you prefer the terminal)
The dashboard just calls the daemon's HTTP API — you can do the same over SSH:

```bash
# start
curl -X POST http://<robot-ip>:8000/api/apps/start-app/reachy_claude_connector
# status
curl    http://<robot-ip>:8000/api/apps/current-app-status
# stop
curl -X POST http://<robot-ip>:8000/api/apps/stop-current-app
# uninstall (either the API…)
curl -X POST http://<robot-ip>:8000/api/apps/remove/reachy_claude_connector
#           …or pip directly
/venvs/apps_venv/bin/pip uninstall -y reachy-mini-claude-connector
```

## Updating to a new version

```bash
cd ~/reachy-mini-claude-cli-connector
git pull
/venvs/apps_venv/bin/pip install --force-reinstall --no-deps .
# then Stop + Run the app from the dashboard to pick up the new code
```

## Configuration reference

`~/.config/reachy-mini-claude/config.env` (read at app start):

| Key | Required | Meaning |
|-----|----------|---------|
| `CONNECTOR_URL` | yes | The Mac connector base URL, e.g. `http://10.10.9.33:8080`. |
| `CONNECTOR_TOKEN` | yes* | Must match the Mac's `server/.env`. *Required if the connector has auth on (it should). |
| `MAX_UTTERANCE_S` | no | Max seconds recorded per turn (default 15). |
| `VAD_SILENCE_MS` / `VAD_RMS_THRESHOLD` | no | End-of-speech tuning (wake-word path; the button uses release). |
| `SETTINGS_ALLOW` | no | Comma-separated IPs/CIDRs allowed to change which connector the robot is bound to (`POST /servers/select\|add\|rescan` on `:8042`). Empty = open (a warning is logged). Loopback and the currently-bound connector's host are always allowed. |

The **language/voice/model** all live on the **Mac** side (`server/.env` + the voice
prompt) — the robot just plays the audio the Mac returns. To change language, see the
main README "Language" section; nothing changes on the robot.

## How it works (for maintainers)

- The app is `reachy_app/app.py` → `class ReachyClaudeConnectorApp(ReachyMiniApp)`.
- `pyproject.toml` exposes it via `[project.entry-points."reachy_mini_apps"]`, which is
  how the dashboard discovers installed apps. The manager launches it as
  `/venvs/apps_venv/bin/python -m reachy_claude_connector.main` (a thin entry shim; the
  real app is `reachy_app/app.py`). The shim exists because the daemon locates an app by
  matching the entry-point *name* to a site-packages folder and scrapes `custom_app_url`
  from that folder's `main.py` — so the folder name must equal the entry-point name
  `reachy_claude_connector`, which `reachy_app/` did not. Without the shim the desktop app
  never learns we have a UI and won't embed it.
- The framework hands the app a connected `ReachyMini` and a `stop_event`, and serves
  `reachy_app/static/index.html` at `custom_app_url` (`:8042`); the app adds the
  `/press /release /status /history` routes to `self.settings_app`.
- It reuses the same `ConversationLoop`, `ReachyMiniBackend` (wrapping the provided
  `mini`), and `ConnectorClient` as the standalone `python -m reachy_app.main`.

## Troubleshooting

- **App page loads but replies fail / 401 in logs** → `CONNECTOR_TOKEN` on the robot
  doesn't match the Mac's `server/.env`, or the Mac connector isn't running/reachable.
  Re-check step 2/3.
- **"Sorry, I didn't catch that."** → the mic recorded silence (or you released too
  fast). Hold the button, speak, then release.
- **App not in the dashboard** → confirm the entry point is registered:
  `/venvs/apps_venv/bin/python -c "from importlib.metadata import entry_points; print([e.name for e in entry_points(group='reachy_mini_apps')])"`
  (should include `reachy_claude_connector`). Refresh the dashboard.
- **Nothing moves / no sound** → make sure no other app is running (the robot runs one
  app at a time); Stop the current app first.
