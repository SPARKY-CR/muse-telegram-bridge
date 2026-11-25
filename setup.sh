#!/usr/bin/env bash
# Set up the Muse-Telegram bridge runtime and start the poller.
# Safe to run again after a VM replacement or an update: it never
# starts a second poller (the poller's own lock prevents that) and
# never overwrites an existing config or state file.
set -euo pipefail
umask 077

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$BRIDGE_DIR/runtime"

mkdir -p "$RUNTIME/downloads" "$RUNTIME/transcripts" "$RUNTIME/locks" \
  "$RUNTIME/outbox"

# Owner config (chat_id) is separate from the poll offset, so a
# corrupt state file can never wipe the owner's id.
if [ ! -f "$RUNTIME/config.json" ]; then
  cp "$BRIDGE_DIR/config.example.json" "$RUNTIME/config.json"
  echo "Created $RUNTIME/config.json — edit it and set your chat_id,"
  echo "then run setup.sh again."
  exit 0
fi

# The chat id is validated with the SAME function the poller and
# the hook use (bridge_common.parse_chat_id), so a digit string
# ("12345") is valid everywhere — and an invalid id fails here,
# loudly, instead of killing the poller silently at startup.
CHAT_ID=$(PYTHONPATH="$BRIDGE_DIR/poller" python3 -c "
import json, sys
import bridge_common
try:
    with open(sys.argv[1]) as f:
        raw = json.load(f).get('chat_id', '')
except Exception:
    raw = ''
parsed = bridge_common.parse_chat_id(raw)
print(parsed if parsed else '')
" "$RUNTIME/config.json" 2>/dev/null || true)
if [ -z "$CHAT_ID" ]; then
  echo "config.json has no valid chat_id — set your Telegram chat id first."
  echo "Send a message to the bot, then run:"
  echo "  python3 bin/telegram.py getupdates"
  exit 1
fi

if [ ! -f "$RUNTIME/state.json" ]; then
  printf '{"offset": 0}' > "$RUNTIME/state.json"
fi

# If a poller is already running (it holds the lock), there is
# nothing to start.
if [ -f "$RUNTIME/poller.lock" ] \
    && ! flock -n "$RUNTIME/poller.lock" true 2>/dev/null; then
  echo "Poller is already running."
  exit 0
fi

# The poller runs from poller/ (never copied into runtime/), so
# edits take effect on the next restart without re-running setup.
# Its working files (heartbeat, lock, handled.txt) live in the
# runtime dir via --runtime-dir. The seen marker lives next to
# the inbox (the hook's default, overridable with
# BRIDGE_SEEN_DIR), so the two are never separated.
SEEN_DIR="${BRIDGE_SEEN_DIR:-$RUNTIME}"
OLD_BEAT=$(python3 -c "import json; print(json.load(open('$RUNTIME/poller-state.json')).get('beat_at', 0))" 2>/dev/null || echo 0)
nohup python3 "$BRIDGE_DIR/poller/poller.py" \
  --config "$RUNTIME/config.json" \
  --state "$RUNTIME/state.json" \
  --inbox "$RUNTIME/inbox.jsonl" \
  --log "$RUNTIME/poller.log" \
  --runtime-dir "$RUNTIME" \
  --seen "$SEEN_DIR/seen.json" \
  --chat-id "$CHAT_ID" \
  >> "$RUNTIME/poller.log" 2>&1 &
disown

# Verify the poller actually started: it writes its first
# heartbeat at once, so the heartbeat must become NEWER than
# before the start (a stale file from a dead poller does not
# count). If it never does, the poller died — report that
# instead of claiming success.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  NEW_BEAT=$(python3 -c "import json; print(json.load(open('$RUNTIME/poller-state.json')).get('beat_at', 0))" 2>/dev/null || echo 0)
  if python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) > float(sys.argv[2]) else 1)" "$NEW_BEAT" "$OLD_BEAT" 2>/dev/null; then
    echo "Poller is running (heartbeat at $RUNTIME/poller-state.json)."
    exit 0
  fi
  sleep 1
done
echo "The poller did not start. Last log lines:" >&2
tail -5 "$RUNTIME/poller.log" 2>/dev/null >&2 || true
exit 1
