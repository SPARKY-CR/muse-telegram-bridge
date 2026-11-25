#!/usr/bin/env bash
# Set up the Muse-Telegram bridge runtime and start the poller.
# Run once after cloning, and again after a VM replacement.
set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$BRIDGE_DIR/runtime"

mkdir -p "$RUNTIME/downloads" "$RUNTIME/transcripts"

if [ ! -f "$RUNTIME/state.json" ]; then
  cp "$BRIDGE_DIR/config.example.json" "$RUNTIME/state.json"
  echo "Created $RUNTIME/state.json — edit it and set your chat_id,"
  echo "then run setup.sh again."
  exit 0
fi

CHAT_ID=$(python3 -c "import json; print(json.load(open('$RUNTIME/state.json')).get('chat_id', 0))")
if [ "$CHAT_ID" = "0" ]; then
  echo "state.json has chat_id 0 — set your Telegram chat id first."
  echo "Send a message to the bot, then run:"
  echo "  python3 bin/telegram.py getupdates"
  exit 1
fi

# The poller lives in poller/ but writes its runtime files next to state.json.
cp "$BRIDGE_DIR/poller/poller.py" "$BRIDGE_DIR/poller/typing_keeper.py" \
   "$BRIDGE_DIR/poller/stuck_watchdog.py" "$RUNTIME/" 2>/dev/null || true
ln -sf "$BRIDGE_DIR/bin" "$RUNTIME/bin" 2>/dev/null || true

nohup python3 "$RUNTIME/poller.py" \
  --state "$RUNTIME/state.json" \
  --inbox "$RUNTIME/inbox.jsonl" \
  --log "$RUNTIME/poller.log" >> "$RUNTIME/poller.log" 2>&1 &
disown
echo $! > "$RUNTIME/poller.pid"
echo "Poller started (pid $(cat "$RUNTIME/poller.pid"))."
echo "Check it with: cat $RUNTIME/poller-state.json"
