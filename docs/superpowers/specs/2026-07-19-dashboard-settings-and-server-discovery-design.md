# Dashboard settings & LAN server discovery — design

**Date:** 2026-07-19
**Status:** Approved (design). The embed fix only *fails* on the real robot / desktop-app
daemon, so its verification is flagged `VERIFY-ON-DASHBOARD`; a couple of SDK calls are
`VERIFY-ON-HARDWARE`.
**Related:** extends the packaged-app form (`reachy_app/app.py`, `INSTALL.md`) and the
connector settings (`server/config.py`). UI mockups saved under
`.superpowers/brainstorm/49799-1784455891/content/{shell,server-picker,gate,config}.html`.

## Goal

Two operator-facing goals on one settings surface:

1. **The app's UI opens *inside* the Reachy desktop app** (like other apps do on start),
   not only by manually browsing to `http://<robot>:8042/` — and it exposes the app's
   **configuration**, editable **live**: changes apply without restarting the app, so the
   GUI you're editing from never dies under you.
2. **LAN autodiscovery of the Mac connector.** The robot app finds connector Macs on the
   Wi-Fi so `CONNECTOR_URL` stops needing manual reconfiguration every time the robot
   moves networks. **Multi-server**: several Macs (home / office laptops) are discovered,
   named, and chosen between.

One design; built in three independently-shippable phases (see Build sequence).

## Platform reality (researched — drives the whole design)

Non-obvious facts from the `reachy_mini` SDK + desktop/iOS apps that shape every decision:

- **Desktop embed is a real, first-class mechanism.** The desktop app (Tauri) iframes an
  app's `custom_app_url` in its right panel and **auto-opens it** the moment the URL
  responds (HEAD-polls every 2 s), passing `?embedded=1&theme=…&accent=…&bg=…&fg=…` so the
  page can theme itself. It **rewrites the host** (`0.0.0.0` → the robot's real IP), so
  `http://0.0.0.0:8042` is *fine* — the port is all that matters.
- **Why ours doesn't embed today (root cause, confirmed).** On the robot / desktop-app
  daemon, the daemon discovers an app's UI by scraping `custom_app_url` out of
  `site_packages/<entry-point-name>/main.py`. Our entry-point **name** is
  `reachy_claude_connector` but our package dir is `reachy_app`, so it looks for
  `reachy_claude_connector/main.py`, finds nothing, reports `custom_app_url = None`, and
  the desktop app bails (`if (!customAppUrl) return null`). **It's a packaging mismatch,
  not `0.0.0.0` and not the code.** (A plain dev "Lite" daemon reads the class attribute
  directly and *would* work — which is why it looks fine locally but not on the robot.)
- **iPhone is a different world — deliberately out of scope for embedding.** The official
  Reachy iOS app (App Store id 6766823749) embeds **only Hugging Face *Space* JS apps over
  WebRTC** (via HF central signaling); it never touches a locally-installed Python app's
  `custom_app_url`/`:8042`. cookAIware appears there because it *also* ships a JS Space
  flavour. Matching that would mean publishing a WebRTC/HF-Space app whose audio path
  (robot ↔ phone/cloud) **cannot carry our Mac-side Bulgarian faster-whisper → Claude →
  Piper pipeline**, and the daemon's `RobotAppLock` makes a phone-embed session and our
  local app **mutually exclusive** on the robot. Decision below.
- **mDNS already exists** in the stack (`_reachy-mini._tcp`) but only for discovering the
  *robot*. For discovering the *Mac connector* we use a plain UDP beacon (below) — no
  macOS mDNSResponder / port-5353 registration fight.

## Core decisions (locked)

1. **Persistent supervisor + restartable in-process worker** (not a second OS process).
   The dashboard-launched process stays up forever, owns the `ReachyMini` handle, and
   serves the UI; a worker *thread* (loop + client + backend) is torn down and rebuilt on
   any config/server change, pointing at the same handle.
   *Rejected:* a separate OS "service" process — on the robot the heavy lifting **is**
   hardware I/O, and the `ReachyMini` handle lives in the one process the daemon launched
   (one app / one connection at a time); a second process would fight for it. *Rejected:*
   mutating individual fields live (fragile); rebuilding the whole worker is clean.
2. **Multi-server, never auto-switch.** If the last-used server is reachable at launch,
   use it silently. Otherwise **park** and let the user pick from discovered servers.
   *Rejected:* auto-switching to a single token-verified match — the user wants an explicit
   choice, and silent brain-swapping is surprising.
3. **UDP broadcast beacon** for Mac→robot discovery. *Rejected:* mDNS/zeroconf (macOS
   registration needs `dns-sd -R` shell-out); supporting both (YAGNI).
4. **Desktop embed + phone browser/PWA; the official iOS-app embed is out of scope.**
   *Rejected:* a JS Hugging Face Space + WebRTC flavour — it abandons the Mac Bulgarian /
   Claude pipeline that is the whole reason this project exists, and can't co-hold the
   robot with the local app anyway. The phone experience is the responsive page in the
   browser, installable as a home-screen **PWA**.
5. **Curated config, push-to-talk only.** Show only controls that do something in the
   on-robot app. *Rejected:* expose-all (wake-word/VAD/raw-audio knobs are dead in this
   mode — `wake=None`, robot samplerate comes from the SDK); *deferred:* wiring "Hey
   Reachy" into the app (would make VAD tuning meaningful — a later scope bump).

## Architecture

```
  Reachy Mini (Pi) — one dashboard-managed process ───────────────┐
   ┌───────────────── SUPERVISOR (persistent, owns ReachyMini) ─┐  │
   │  settings_app (FastAPI @ :8042):  UI + config/server API   │  │
   │  runtime config (mutable)   saved-servers store (disk)     │  │
   │  discovery listener  ◄─────── UDP beacons on the LAN       │  │
   │        │ build / rebuild / park / restart-on-crash         │  │
   │        ▼                                                    │  │
   │  WORKER thread (rebuildable): ConversationLoop +           │  │
   │  ConnectorClient(url,token) + ReachyMiniBackend(same mini) │  │
   └────────────────────────────────────────────────────────────┘  │
                    │ POST /chat (only when a server is bound)       │
        ── LAN ─────┼──────────────────────◄── UDP beacon ──────────┘
                    ▼                        (every ~10 s)
   ┌──────────── Mac connector (server/) ───────────────────────┐
   │  FastAPI :8080  STT→Claude→TTS   +   BEACON advertiser      │
   │  GET /health (open, liveness)   GET /whoami (token → id,name)│
   └────────────────────────────────────────────────────────────┘
```

- **Supervisor** = the `ReachyMiniApp.run()` process. It never restarts while the app is
  "running"; it owns the `ReachyMini` handle and the HTTP UI. Holds the mutable runtime
  config + the currently-bound server.
- **Worker** = a thread running `ConversationLoop`. Rebuilt (stop → join → recreate →
  start) whenever config changes, the server is switched, or it crashes. **Parked** (no
  thread) when no server is bound → the Talk tab shows the gate.
- **Config model** — replace the frozen import-time `Settings` with a mutable runtime
  config the worker reads at (re)build time. Add a **saved-servers store**
  (`~/.config/reachy-mini-claude/servers.json`): `[{id, name, url, token, last_used_at}]`
  plus `last_selected_id`. Live tunables persist to the same config dir.
- **Discovery** — Mac broadcasts a beacon; robot passively listens and maintains a live
  discovered-server list, cross-checked against the saved store by stable `id`.

## The embed fix (Phase 1)

The daemon scrapes `custom_app_url` from `site_packages/<entry-point-name>/main.py`, so
**the entry-point name must equal the installed package dir, and that dir's `main.py` must
contain a `custom_app_url = "…"` line.**

- **Recommended:** rename the package `reachy_app/` → `reachy_claude_connector/` (mechanical
  import rename) and host the `ReachyMiniApp` subclass in its `main.py`
  (`entry-point: reachy_claude_connector = "reachy_claude_connector.main:ReachyClaudeConnectorApp"`),
  with `custom_app_url = "http://0.0.0.0:8042"` defined there. This aligns the scrape path,
  the run path, **and** keeps the friendly dashboard name `reachy_claude_connector`.
  Touch points: package dir, all `reachy_app.*` imports, `pyproject.toml`, `Makefile`,
  `run.sh`, tests, `INSTALL.md`, `README`, `CLAUDE.md`.
- **Lighter fallback:** keep the `reachy_app` package, set the entry-point name to
  `reachy_app`, and put `custom_app_url` + the app class in `reachy_app/main.py` — accepts
  the plainer dashboard label "reachy_app".

`VERIFY-ON-DASHBOARD`: this only *fails* on the robot / macOS desktop-app daemon (the
scrape path); confirm the app auto-embeds there after the fix. The dashboard passes
`?embedded=1&theme=…` — the UI reads those to theme itself.

## UI — the four screens (approved via mockups)

Narrow single column (~450 px desktop panel / phone width), **mobile-first**. Nav =
**top segmented toggle `[ Talk | Settings ]`** under the header (chosen over bottom-tabs
and gear-sheet).

- **Talk tab** — today's hold-to-talk + live status + history, unchanged, plus the
  **parked "gate"** state.
- **Gate** (Talk tab, when no server bound): "Pick a brain to start" with discovered
  servers listed **inline** for one-tap connect (reachable last-used pre-highlighted);
  plus *scanning* and *nothing-found* variants. Never shown when the last-used server is
  reachable at launch (then it just connects → Ready).
- **Settings → Server connection** — the picker: friendly-name rows with live status/RTT;
  **inline token** entry (row expands); reachable last-used pre-selected + `LAST USED`
  badge; **saved-but-offline servers dimmed** (not hidden); a **＋ Add by address**
  fallback; a **⟳ Rescan**.
- **Settings → config** — curated live params (below). Header connection chip; a
  connected-server summary row (→ Manage opens the picker).

**PWA:** `manifest.json`, `apple-touch-icon`, theme-color meta, optional minimal service
worker so the served page installs to the phone home screen.

## settings_app API (robot, same origin as the page)

| Method | Route | Purpose |
|---|---|---|
| GET | `/config` | current live config values |
| POST | `/config` | update live params → supervisor rebuilds the worker |
| GET | `/servers` | `{discovered:[…], saved:[…], selected_id}` (merged by `id`) |
| POST | `/servers/select` | `{id or url, token?}` → verify (`/whoami`) → bind → (re)start worker |
| POST | `/servers/add` | manual add-by-address `{url, token}` → verify → save |
| POST | `/servers/rescan` | clear + re-listen; optional active solicitation probe |
| POST | `/restart-app` | media-backend change → `POST localhost:8000/api/apps/restart-current-app` |

Flat routes to match the existing `/press /release /status /history /move /frame` (which
stay). All are token-free *to the page* (same-origin, on the robot); the **connector**
token guards the Mac side.

## Discovery protocol & behaviour

- **Beacon (Mac `server/`):** every ~10 s, UDP broadcast on the subnet, fixed port
  (`DISCOVERY_PORT`, default e.g. 48569), payload
  `{"reachy_connector":1,"id":"<uuid>","name":"<SERVER_NAME>","url":"http://<ip>:8080"}`.
  **No token in the beacon.** `id` is generated once and persisted (server state file);
  `name` defaults to the hostname, overridable via `SERVER_NAME`. Gated by
  `DISCOVERY_BEACON` (default on).
- **Listener (robot):** passively receives beacons → live list `{id,name,url,seen_at}`;
  a `/health` ping drives the green "online" dot + RTT.
- **Verify + connect:** selecting a server calls `GET <url>/whoami` **with the token**;
  200 + matching `id` confirms reachability **and** token (and defeats a spoofed beacon).
  401 → prompt for token (inline). `/health` stays open (liveness only); **`/whoami` is
  token-protected** — this is new, because today `/health` is the only probe and it's
  unauthenticated, so it can't verify a token.
- **Launch logic:** load `last_selected_id`; if its saved server verifies → bind + run
  **silently**; else **park** + show the gate; discovery populates candidates; user
  selects (+token) → bind + run. **Never auto-switch.**
- **Switch anytime** from Settings. **Manual add-by-address** covers beacon-blocked LANs.
- **Caveat:** UDP broadcast is LAN/subnet-local; VPNs and segmented Wi-Fi break it — hence
  the always-available manual add.

## Config parameters

| Param | Panel | Apply |
|---|---|---|
| server url / token / (per-server) | Server connection | live (rebuild worker) |
| `REQUEST_TIMEOUT_S` (Reply timeout) | Conversation | live |
| `MAX_UTTERANCE_S` (Max utterance) | Conversation | live |
| `LOG_LEVEL` | Diagnostics | live |
| `reachy_media_backend` (Audio pipeline) | Advanced | **restart** (Save & restart app) |
| `wakeword_*`, VAD (`vad_*`), `backend`, `sample_rate`, `frame_ms`, `button_*` | — | **curated out** (dead in on-robot push-to-talk mode) |

`media_backend` is fixed when the framework constructs `ReachyMini` before `run()`, so it
alone needs a full app restart (via the daemon) — clearly flagged in the UI.

## Components touched

**Mac `server/`**
- `beacon.py` *(new)* — UDP broadcaster; started/stopped from `main.py` lifespan.
- `main.py` — start the beacon; add `GET /whoami` (token-protected: `{id, name, version}`).
- `config.py` / `.env.example` — `SERVER_NAME`, `DISCOVERY_BEACON`, `DISCOVERY_PORT`,
  persisted `server_id` (state file).

**Robot `reachy_app/`** (package possibly renamed — see embed fix)
- `pyproject.toml` — entry-point name aligned to the package dir (embed fix).
- `main.py` — carries `custom_app_url` for the scraper (and, recommended, the app class).
- `app.py` — supervisor/worker split; config + servers routes on `settings_app`; serve the
  new UI; media-backend restart via daemon.
- `config.py` — mutable runtime config; saved-servers store + persistence helpers.
- `discovery.py` *(new)* — UDP beacon listener + server registry + `/whoami` verify.
- `supervisor.py` *(new, or in `app.py`)* — worker lifecycle: build / rebuild / park /
  crash-restart, sharing the one `ReachyMini`.
- `loop.py` — read tunables from the runtime config at (re)build; unchanged turn logic.
- `static/` — new UI (Talk + Settings tabs, picker, gate, config), PWA manifest + icons +
  service worker; read the dashboard theme query params.
- `tests/test_smoke.py` — discovery listener parses beacons; saved-servers store round-trip;
  supervisor rebuild swaps client/params live; select-server + token-verify flow through
  the `FakeBackend`; `/whoami` verification. No robot required.

## Error handling

- **Connector unreachable mid-session** → the turn errors (existing path); status reflects
  it; the saved server stays selected, discovery keeps its dot red; user can switch.
- **Token wrong/missing** → `/whoami` 401 → inline token prompt; never silently proceed.
- **Beacon blocked / nothing found** → gate "nothing found" state + add-by-address.
- **Worker crash** → supervisor catches, marks error, auto-restarts with backoff; UI never
  dies (it's in the persistent process).
- **Media-backend change** → explicit "Save & restart app" (daemon restart), Settings
  reopens after the app comes back (dashboard re-embeds on readiness).

## Testing

Mac: `server/` — beacon payload/interval; `/whoami` auth (200 with token, 401 without).
Robot: `reachy_app/` fake-backend suite — listener parses/merges beacons; store persists;
supervisor rebuild applies new url/params without touching the handle; select flow
verifies token; parked state runs no worker. The **embed fix** is only verifiable on the
robot / desktop-app daemon (`VERIFY-ON-DASHBOARD`) — manual on-hardware check: app
auto-embeds, theme params applied, restart-app round-trips.

## Build sequence (phases)

1. **Embed + shell** — the packaging fix so the UI auto-embeds in the desktop app;
   restructure `static/` into the segmented Talk|Settings shell (Settings can start
   near-empty). *Most visible win; smallest change; proves the embed.*
2. **Supervisor/worker + live config** — persistent/restartable split; `/api/config`
   read/write + the config panel; persistence. *Task 1 proper.*
3. **Multi-server discovery** — Mac beacon + `/whoami`; robot listener + saved-servers
   store; the picker + gate + launch/park logic; `/api/servers/*`. *Task 2.* (May spin off
   its own implementation plan.)

## Out of scope / deferred

- **Official iOS-app embedding** — needs a JS HF-Space + WebRTC app incompatible with the
  Mac Bulgarian/Claude pipeline and mutually exclusive with the local app on the robot.
  Recorded as a deliberate decision. Phone = responsive page in the browser / PWA.
- **Wake word in the app** (would revive VAD tuning as real controls).
- **Auto-switching** servers; **mDNS** discovery; a second OS service process.

## Security posture

- The **token remains the real access control** (unchanged). The beacon advertises **no
  secret** — only `{id, name, url}`; the token is entered in the UI and stored locally on
  the robot (`servers.json`), per server. `/whoami` (token-gated) both verifies the token
  and cross-checks the beacon's claimed `id`, so a rogue beacon can't impersonate a known
  brain. Discovery is LAN-only.

## Verify-on-hardware / dashboard

1. `VERIFY-ON-DASHBOARD` — after the packaging fix, the app auto-embeds in the macOS
   desktop app and the iframe applies the theme params.
2. `VERIFY-ON-HARDWARE` — `POST /api/apps/restart-current-app` from inside the app cleanly
   bounces it (for the media-backend change) and the dashboard re-embeds on readiness.
3. `VERIFY-ON-HARDWARE` — UDP broadcast is delivered/received across the actual robot↔Mac
   Wi-Fi (some APs isolate clients); confirm the beacon and fall back to add-by-address if
   the AP blocks broadcast.
