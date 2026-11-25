#!/usr/bin/env bash
# Hook: watch the Telegram inbox and wake the agent on new messages
# from the owner. Also restarts the lightweight poller if it died.
#
# Configure via environment (or edit the defaults below):
#   BRIDGE_DIR       - bridge working directory (poller, inbox, logs)
#   BRIDGE_STATE     - state.json path (chat_id + offset)
#   BRIDGE_CHAT_ID   - the owner's Telegram chat id
#   BRIDGE_SEEN_DIR  - hook state directory (seen marker)
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

WEBHOOK_DIR="${BRIDGE_DIR:-$HOME/workspace/muse-telegram-bridge/runtime}"
INBOX="$WEBHOOK_DIR/inbox.jsonl"
STATE_FILE="${BRIDGE_STATE:-$WEBHOOK_DIR/state.json}"
STATE_DIR="${BRIDGE_SEEN_DIR:-$HOME/hooks/state/telegram-webhook}"
SEEN_FILE="$STATE_DIR/seen.json"
CHAT_ID="${BRIDGE_CHAT_ID:-}"

mkdir -p "$STATE_DIR"

# --- Supervision: restart the poller if it died or hung (silent). ---
POLLER_STALE=$(python3 - "$WEBHOOK_DIR/poller-state.json" <<'EOF'
import json, sys, time
from datetime import datetime
try:
    with open(sys.argv[1]) as f:
        beat = json.load(f).get("beat_at")
    dt = datetime.strptime(beat, "%Y-%m-%dT%H:%M:%S%z")
    print("1" if time.time() - dt.timestamp() > 120 else "0")
except Exception:
    print("0")
EOF
)
POLLER_ALIVE=0
if [ -f "$WEBHOOK_DIR/poller.pid" ] \
    && kill -0 "$(cat "$WEBHOOK_DIR/poller.pid")" 2>/dev/null; then
  POLLER_ALIVE=1
fi
if [ "$POLLER_ALIVE" = "1" ] && [ "$POLLER_STALE" = "1" ]; then
  kill "$(cat "$WEBHOOK_DIR/poller.pid")" 2>/dev/null || true
  sleep 2
  POLLER_ALIVE=0
  log "supervisor" '{"action":"hung poller killed"}'
fi
if [ "$POLLER_ALIVE" = "0" ]; then
  nohup python3 "$WEBHOOK_DIR/poller.py" \
    --state "$STATE_FILE" \
    --inbox "$INBOX" \
    --log "$WEBHOOK_DIR/poller.log" >> "$WEBHOOK_DIR/poller.log" 2>&1 &
  disown
  echo $! > "$WEBHOOK_DIR/poller.pid"
  log "supervisor" '{"action":"poller restarted"}'
fi

# --- Message detection: scan the inbox for new messages from the owner. ---
SCAN=$(python3 - "$INBOX" "$SEEN_FILE" "$CHAT_ID" <<'EOF'
import json, sys
inbox, seen_file, chat_id = sys.argv[1], sys.argv[2], sys.argv[3]
seen = 0
try:
    with open(seen_file) as f:
        seen = json.load(f).get("last_update_id", 0)
except (OSError, ValueError):
    seen = 0
new_messages = []
max_id = seen
try:
    with open(inbox) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                upd = json.loads(line)
            except ValueError:
                continue
            uid = upd.get("update_id", 0)
            if uid > max_id:
                max_id = uid
            if uid <= seen:
                continue
            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            if str(chat.get("id")) != chat_id:
                cb = upd.get("callback_query") or {}
                sender = cb.get("from") or {}
                if str(sender.get("id")) != chat_id:
                    continue
                data = cb.get("data")
                if not data:
                    continue
                cb_msg = cb.get("message") or {}
                new_messages.append({
                    "kind": "callback", "update_id": uid,
                    "callback_query_id": cb.get("id", ""),
                    "data": data,
                    "message_id": cb_msg.get("message_id", 0),
                    "date": cb_msg.get("date", 0)})
                continue
            text = msg.get("text")
            photo = msg.get("photo") or []
            document = msg.get("document") or {}
            voice = msg.get("voice") or {}
            if text:
                new_messages.append({"kind": "message", "update_id": uid,
                                     "text": text,
                                     "message_id": msg.get("message_id", 0),
                                     "date": msg.get("date", 0)})
            elif photo:
                largest = photo[-1]
                new_messages.append({"kind": "photo", "update_id": uid,
                                     "file_id": largest.get("file_id", ""),
                                     "caption": msg.get("caption", ""),
                                     "message_id": msg.get("message_id", 0),
                                     "date": msg.get("date", 0)})
            elif document:
                new_messages.append({"kind": "document", "update_id": uid,
                                     "file_id": document.get("file_id", ""),
                                     "file_name": document.get("file_name", ""),
                                     "mime_type": document.get("mime_type", ""),
                                     "file_size": document.get("file_size", 0),
                                     "caption": msg.get("caption", ""),
                                     "message_id": msg.get("message_id", 0),
                                     "date": msg.get("date", 0)})
            elif voice:
                new_messages.append({"kind": "voice", "update_id": uid,
                                     "file_id": voice.get("file_id", ""),
                                     "duration": voice.get("duration", 0),
                                     "message_id": msg.get("message_id", 0),
                                     "date": msg.get("date", 0)})
            else:
                continue
except OSError:
    pass
print(json.dumps({"new_messages": new_messages,
                  "max_update_id": max_id, "seen": seen}))
EOF
)

COUNT=$(echo "$SCAN" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['new_messages']))")
MAX_ID=$(echo "$SCAN" | python3 -c "import json,sys; print(json.load(sys.stdin)['max_update_id'])")
SEEN=$(echo "$SCAN" | python3 -c "import json,sys; print(json.load(sys.stdin)['seen'])")

if [ "$COUNT" -gt 0 ]; then
  if [ "$HATCH_HOOK_DRY_RUN" != "1" ]; then
    echo "{\"last_update_id\": $MAX_ID}" > "$SEEN_FILE"
  fi
  wake "new Telegram messages from the owner" "$SCAN"
elif [ "$MAX_ID" -gt "$SEEN" ]; then
  if [ "$HATCH_HOOK_DRY_RUN" != "1" ]; then
    echo "{\"last_update_id\": $MAX_ID}" > "$SEEN_FILE"
  fi
  silent "skipped non-owner updates"
else
  silent "no new Telegram messages"
fi
