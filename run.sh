#!/usr/bin/env bash
# Start the whole thing: connector server (the brain) + reachy_app (button + audio).
#
#   ./run.sh                 # local backend (Mac mic/speaker), wake word off — for testing
#   ./run.sh --backend reachy   # on the real robot
#
# Anything you pass is forwarded to reachy_app.main. Ctrl-C stops both.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Piper voice: honor an existing PIPER_MODEL, else the downloaded default.
PIPER_MODEL="${PIPER_MODEL:-$ROOT/voices/bg_BG-dimitar-medium.onnx}"
if [ ! -f "$PIPER_MODEL" ]; then
  echo "!! Piper voice not found at: $PIPER_MODEL"
  echo "   Download it (see server/README.md) or set PIPER_MODEL."
  exit 1
fi

# Clean up any previous run.
pkill -f "uvicorn main:app" 2>/dev/null || true
pkill -f "reachy_app.main" 2>/dev/null || true
sleep 1

echo "▶ starting connector server (:8080) …"
( cd "$ROOT/server" && source .venv/bin/activate \
    && PIPER_MODEL="$PIPER_MODEL" exec uvicorn main:app --host 0.0.0.0 --port 8080 ) \
    > /tmp/connector.log 2>&1 &
CONNECTOR_PID=$!

# Stop the connector whenever this script exits (Ctrl-C included).
cleanup() { echo; echo "▶ stopping …"; kill "$CONNECTOR_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# Wait for the connector to be ready.
for i in $(seq 1 40); do
  curl -s localhost:8080/health >/dev/null 2>&1 && break
  sleep 1
  [ "$i" = 40 ] && { echo "!! connector didn't start — see /tmp/connector.log"; exit 1; }
done
echo "  connector ready. log: /tmp/connector.log"

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<mac-ip>')"
BUTTON_PORT="${BUTTON_PORT:-8081}"
echo
echo "📱 Open on your phone (same Wi-Fi):  http://$IP:$BUTTON_PORT/"
echo "   (local backend = phone is the button; mic/speaker are this Mac)"
echo "   Ctrl-C to stop both."
echo

# Run the app in the foreground so Ctrl-C lands here.
source "$ROOT/reachy_app/.venv/bin/activate"
exec python -m reachy_app.main --backend local --no-wakeword "$@"
