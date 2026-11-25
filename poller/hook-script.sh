#!/usr/bin/env bash
# Hook: watch the Telegram inbox and wake the agent on new messages
# from the owner. Also restarts the lightweight poller if it died.
#
# Configure via environment (or edit the defaults below):
#   BRIDGE_DIR       - bridge working directory (poller, inbox, logs).
#                      Default: $BRIDGE_ROOT/runtime. Set it
#                      explicitly when the runtime lives elsewhere.
#   BRIDGE_ROOT      - repository root (poller/, bin/); default is
#                      the parent of this script's directory. Set it
#                      explicitly when the hook body is stored
#                      somewhere else (e.g. the Muse hooks dir).
#   BRIDGE_CONFIG    - owner config file (chat_id); default
#                      $BRIDGE_DIR/config.json
#   BRIDGE_CHAT_ID   - override for the owner's Telegram chat id
#   BRIDGE_SEEN_DIR  - override for the seen/pending marker
#                      directory (default: next to the inbox, in
#                      BRIDGE_DIR, so the markers and the inbox
#                      can never be deleted separately)
#   BRIDGE_LEGACY_SEEN_DIR - where pre-v3 markers lived
#                      (default $HOME/hooks/state/telegram-webhook
#                      when HOME is set); copied over once on
#                      upgrade, see below
#   BRIDGE_ALLOW_GROUPS - set to 1 to answer the owner's
#                      group/supergroup chats too (passed to both
#                      the poller and the scan)
#   BRIDGE_POLLER_STALE_SECS - heartbeat age that counts as a
#                      hung poller (default 120; see the timing
#                      note at the stale check)
#
# The agent and telegram.py must use THIS runtime dir, not the
# repo's default runtime/: before waking, the hook exports
# BRIDGE_DIR and the TELEGRAM_* paths below, so handled marks,
# downloads, and photos can never land in a different directory
# than the one this hook scans (review item C2c).
#
# The chat id has ONE source of truth: the config file (the same
# file the poller reads), validated by bridge_common.parse_chat_id
# — the same validation the poller and setup.sh use. BRIDGE_CHAT_ID
# only overrides it. If neither yields a valid id, the hook fails
# loudly instead of silently dropping every message.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# BRIDGE_ROOT first: the working directory defaults to
# $BRIDGE_ROOT/runtime, so a copied hook with BRIDGE_ROOT set
# (and BRIDGE_DIR unset) uses the repo's runtime instead of a
# hardcoded path that does not exist here (review item C2c).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_ROOT="${BRIDGE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
WEBHOOK_DIR="${BRIDGE_DIR:-$BRIDGE_ROOT/runtime}"
POLLER_DIR="$BRIDGE_ROOT/poller"
INBOX="$WEBHOOK_DIR/inbox.jsonl"
CONFIG_FILE="${BRIDGE_CONFIG:-$WEBHOOK_DIR/config.json}"
# Seen/pending live next to the inbox by default: if the markers
# and the inbox are deleted together, no replay is possible.
STATE_DIR="${BRIDGE_SEEN_DIR:-$WEBHOOK_DIR}"
SEEN_FILE="$STATE_DIR/seen.json"
PENDING_FILE="$STATE_DIR/pending.json"
HANDLED_FILE="$WEBHOOK_DIR/handled.txt"
POLLER_PY="$POLLER_DIR/poller.py"
SCAN_PY="$POLLER_DIR/scan_inbox.py"
HEARTBEAT="$WEBHOOK_DIR/poller-state.json"
POLLER_LOCK="$WEBHOOK_DIR/poller.lock"

mkdir -p "$STATE_DIR" "$WEBHOOK_DIR"

# --- Migration from the pre-v3 marker location (v2 kept
# seen/pending in the Muse hook state dir). If the new location
# has no markers but the legacy one does, copy them over once,
# so the upgrade does not re-wake the last 24h of messages
# (review item B). The scan's handled-skip is the safety net
# when the legacy files are gone too. ---
# HOME may be unset (cron/systemd-style environments): under
# set -u a bare $HOME expansion would kill the hook, so the
# legacy default is only used when HOME is actually set.
LEGACY_STATE_DIR="${BRIDGE_LEGACY_SEEN_DIR:-}"
if [ -z "$LEGACY_STATE_DIR" ] && [ -n "${HOME:-}" ]; then
  LEGACY_STATE_DIR="$HOME/hooks/state/telegram-webhook"
fi
if [ -n "$LEGACY_STATE_DIR" ] && [ ! -f "$SEEN_FILE" ] \
    && [ -f "$LEGACY_STATE_DIR/seen.json" ]; then
  cp "$LEGACY_STATE_DIR/seen.json" "$SEEN_FILE" 2>/dev/null || true
  log "migrate" '{"action":"seen migrated from legacy hook state"}'
fi
if [ -n "$LEGACY_STATE_DIR" ] && [ ! -f "$PENDING_FILE" ] \
    && [ -f "$LEGACY_STATE_DIR/pending.json" ]; then
  cp "$LEGACY_STATE_DIR/pending.json" "$PENDING_FILE" 2>/dev/null || true
fi

# --- The bridge files must exist where BRIDGE_ROOT says they are.
# If the hook body was copied elsewhere without BRIDGE_ROOT, fail
# loudly instead of silently never waking. ---
if [ ! -f "$POLLER_PY" ]; then
  log "config" '{"error":"poller.py not found"}'
  echo "poller.py not found at $POLLER_PY; set BRIDGE_ROOT to the" >&2
  echo "muse-telegram-bridge repository root." >&2
  exit 1
fi
if [ ! -f "$SCAN_PY" ]; then
  log "config" '{"error":"scan_inbox.py not found"}'
  echo "scan_inbox.py not found at $SCAN_PY; set BRIDGE_ROOT to the" >&2
  echo "muse-telegram-bridge repository root." >&2
  exit 1
fi

# --- Concurrency: one hook run at a time. Overlapping runs would
# read the same seen marker and wake twice for one message.
# -E 99 makes "lock held" distinguishable from a real flock error:
# any other failure is loud, never mistaken for "another run". ---
exec 9>"$STATE_DIR/hook.lock"
FLOCK_RC=0
flock -n -E 99 9 || FLOCK_RC=$?
if [ "$FLOCK_RC" -eq 99 ]; then
  silent "another hook run is active"
  exit 0
elif [ "$FLOCK_RC" -ne 0 ]; then
  log "hook" '{"error":"cannot take hook lock"}'
  echo "cannot take hook lock (flock exit $FLOCK_RC)" >&2
  exit 1
fi

# --- Owner id: config file first, env override second. Both go
# through the SAME validation the poller uses, so the hook can
# never accept an id the poller rejects (e.g. a digit string). ---
CHAT_ID=$(BRIDGE_CHAT_ID="${BRIDGE_CHAT_ID:-}" CONFIG_FILE="$CONFIG_FILE" \
  PYTHONPATH="$POLLER_DIR" python3 -c "
import json, os, sys
import bridge_common
raw = os.environ.get('BRIDGE_CHAT_ID') or ''
if not raw:
    try:
        with open(os.environ['CONFIG_FILE']) as f:
            raw = json.load(f).get('chat_id', '')
    except Exception:
        raw = ''
parsed = bridge_common.parse_chat_id(raw)
print(parsed if parsed else '')
" 2>/dev/null || true)
if [ -z "$CHAT_ID" ]; then
  log "config" '{"error":"no valid owner chat id"}'
  echo "no valid owner chat id (config $CONFIG_FILE or BRIDGE_CHAT_ID)" >&2
  exit 1
fi

# --- Supervision: restart the poller if it died or hung. Liveness
# is tested with the lock itself, never with a PID file (PIDs are
# reused, and a dead PID makes a dead poller look alive).
# -E 99: exit 0 means the lock is free, 99 means held, anything
# else is a real error — then we still attempt a start, because
# the poller's own flock prevents a duplicate. ---
POLLER_RUNNING=0
LOCK_RC=0
flock -n -E 99 "$POLLER_LOCK" true 2>/dev/null || LOCK_RC=$?
if [ "$LOCK_RC" -eq 0 ]; then
  POLLER_RUNNING=0
elif [ "$LOCK_RC" -eq 99 ]; then
  POLLER_RUNNING=1
else
  log "supervisor" '{"warning":"poller lock check failed"}'
  POLLER_RUNNING=0
fi

# A missing or unparsable heartbeat means the poller never beats:
# that is stale (a poller that never beats must be flagged, not
# assumed healthy). Heartbeats carry epoch seconds now.
#
# Timing consistency: the poller beats every 15s until its
# progress deadline (poll timeout 60s + grace 60s = 120s), then
# beat_at freezes. This threshold (120s) must stay well above
# the 15s beat interval so a healthy poller is never flagged,
# and a hung one is killed once its frozen beat is 120s old —
# worst case ~240s after the stuck cycle began (120s deadline
# + 120s threshold). Keep the three numbers in step.
POLLER_STALE_SECS="${BRIDGE_POLLER_STALE_SECS:-120}"
POLLER_STALE=1
if [ -f "$HEARTBEAT" ]; then
  POLLER_STALE=$(python3 - "$HEARTBEAT" "$POLLER_STALE_SECS" <<'EOF'
import json, sys, time
try:
    with open(sys.argv[1]) as f:
        beat = json.load(f).get("beat_at")
    print("1" if time.time() - float(beat) > float(sys.argv[2]) else "0")
except Exception:
    print("1")
EOF
)
fi

if [ "$POLLER_RUNNING" = "1" ] && [ "$POLLER_STALE" = "1" ]; then
  # Hung: kill the PID from the heartbeat, but only after
  # verifying via /proc that it really is the poller, then
  # confirm it is gone (SIGKILL if it lingers). The heartbeat
  # path is passed as an argument, never interpolated into the
  # Python code.
  HUNG_PID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('pid',''))" "$HEARTBEAT" 2>/dev/null || true)
  if [ -n "$HUNG_PID" ] && [ -r "/proc/$HUNG_PID/cmdline" ] \
      && tr '\0' ' ' < "/proc/$HUNG_PID/cmdline" | grep -q "poller.py"; then
    kill "$HUNG_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$HUNG_PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$HUNG_PID" 2>/dev/null; then
      kill -9 "$HUNG_PID" 2>/dev/null || true
      sleep 1
    fi
    log "supervisor" '{"action":"hung poller killed"}'
  else
    log "supervisor" '{"action":"hung poller pid not verified, not killed"}'
  fi
  POLLER_RUNNING=0
fi

if [ "$POLLER_RUNNING" = "0" ]; then
  # 9>&- is essential: the hook holds its own lock on fd 9, and
  # a child started without it INHERITS the descriptor and holds
  # the hook lock for the rest of its life — every later hook run
  # would then report "another hook run is active" and no message
  # would ever wake again. The validated chat id is passed
  # explicitly so the poller uses exactly the id the hook did.
  # --allow-groups must reach the POLLER too: without it the
  # poller never claims group updates and the scan's flag has
  # nothing to deliver (the v4 BRIDGE_ALLOW_GROUPS bug).
  POLLER_EXTRA_ARGS=()
  if [ "${BRIDGE_ALLOW_GROUPS:-0}" = "1" ]; then
    POLLER_EXTRA_ARGS+=(--allow-groups)
  fi
  nohup python3 "$POLLER_PY" \
    --config "$CONFIG_FILE" \
    --state "$WEBHOOK_DIR/state.json" \
    --inbox "$INBOX" \
    --log "$WEBHOOK_DIR/poller.log" \
    --runtime-dir "$WEBHOOK_DIR" \
    --seen "$SEEN_FILE" \
    --chat-id "$CHAT_ID" \
    "${POLLER_EXTRA_ARGS[@]}" \
    >> "$WEBHOOK_DIR/poller.log" 2>&1 9>&- &
  disown
  log "supervisor" '{"action":"poller restarted"}'
fi

# The agent and telegram.py must use THIS runtime dir: export
# it (and the paths derived from it) before the scan and before
# ANY wake — including the scan-failure wake below — so a custom
# BRIDGE_DIR/BRIDGE_CONFIG cannot strand handled marks,
# downloads, or photos in the repo's default runtime/ (review
# items C2c and the v5 scan-failure export order).
export BRIDGE_DIR="$WEBHOOK_DIR"
export BRIDGE_CHAT_ID="$CHAT_ID"
export TELEGRAM_CONFIG_FILE="$CONFIG_FILE"
export TELEGRAM_HANDLED_FILE="$HANDLED_FILE"
export TELEGRAM_ACK_FILE="$HANDLED_FILE.ack"
export TELEGRAM_DOWNLOAD_DIR="$WEBHOOK_DIR/downloads"
export TELEGRAM_SEND_PATHS="$WEBHOOK_DIR/outbox"
export TELEGRAM_POLLER_LOCK="$POLLER_LOCK"

# --- Message detection: the Python scan owns the seen/pending
# markers (atomic writes, fail-closed on corruption, retry for
# unhandled messages). A dry run must not write any state. ---
SCAN_ARGS=(--ack "$HANDLED_FILE.ack")
if [ "${HATCH_HOOK_DRY_RUN:-0}" = "1" ]; then
  SCAN_ARGS+=(--dry-run)
fi
if [ "${BRIDGE_ALLOW_GROUPS:-0}" = "1" ]; then
  SCAN_ARGS+=(--allow-groups)
fi

SCAN_FAILURE_FILE="$STATE_DIR/scan-failures.count"
SCAN_FAILURE_NOTIFIED="$STATE_DIR/scan-failure-notified"
SCAN_FAILURE_NOTIFY_AFTER=3

if ! SCAN=$(python3 "$SCAN_PY" \
    --inbox "$INBOX" \
    --seen "$SEEN_FILE" \
    --pending "$PENDING_FILE" \
    --handled "$HANDLED_FILE" \
    --chat-id "$CHAT_ID" \
    "${SCAN_ARGS[@]}" 2>"$STATE_DIR/scan.err"); then
  # A scan failure must be VISIBLE, not a silent forever-stall:
  # log the first lines of scan.err with the consecutive-failure
  # count, and wake the agent once after repeated failures so
  # the owner hears the bridge is broken.
  SCAN_FAILURE_COUNT=0
  if [ -f "$SCAN_FAILURE_FILE" ]; then
    SCAN_FAILURE_COUNT=$(cat "$SCAN_FAILURE_FILE" 2>/dev/null || echo 0)
  fi
  case "$SCAN_FAILURE_COUNT" in
    ''|*[!0-9]*) SCAN_FAILURE_COUNT=0 ;;
  esac
  SCAN_FAILURE_COUNT=$((SCAN_FAILURE_COUNT + 1))
  printf '%s\n' "$SCAN_FAILURE_COUNT" > "$SCAN_FAILURE_FILE" 2>/dev/null || true
  SCAN_LOG_JSON=$(python3 - "$STATE_DIR/scan.err" "$SCAN_FAILURE_COUNT" <<'EOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
        err = "".join(f.readlines()[:5]).strip()[:500]
except OSError:
    err = ""
print(json.dumps({"error": "scan failed",
                  "consecutive_failures": int(sys.argv[2]),
                  "stderr": err}))
EOF
)
  log "scan" "$SCAN_LOG_JSON"
  if [ "$SCAN_FAILURE_COUNT" -ge "$SCAN_FAILURE_NOTIFY_AFTER" ] \
      && [ ! -f "$SCAN_FAILURE_NOTIFIED" ]; then
    : > "$SCAN_FAILURE_NOTIFIED" 2>/dev/null || true
    SCAN_FAILURE_PAYLOAD=$(python3 - "$STATE_DIR/scan.err" "$SCAN_FAILURE_COUNT" <<'EOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
        err = "".join(f.readlines()[:5]).strip()[:500]
except OSError:
    err = ""
print(json.dumps({
    "new_messages": [], "max_update_id": 0, "seen": 0,
    "pending": 0, "dropped": [],
    "warnings": ["Telegram bridge scan failed %s times in a row"
                 % sys.argv[2]],
    "scan_error": err}))
EOF
)
    wake "Telegram bridge scan failed repeatedly" "$SCAN_FAILURE_PAYLOAD" 9>&-
  else
    silent "scan failed"
  fi
  exit 0
fi
rm -f "$SCAN_FAILURE_FILE" "$SCAN_FAILURE_NOTIFIED"

# Wake for new messages OR for dropped ones: a payload with only
# dropped messages still needs the agent to tell the owner what
# was lost (review item C). The wake payload is the whole scan
# JSON, so the agent sees both lists.
COUNT=$(echo "$SCAN" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['new_messages']) + len(d.get('dropped', [])))")

if [ "$COUNT" -gt 0 ]; then
  # 9>&- is essential here too: a background process spawned by
  # the wake would otherwise inherit the hook lock (fd 9) and
  # block every later hook run (review item C1b).
  wake "new Telegram messages from the owner" "$SCAN" 9>&-
else
  silent "no new Telegram messages"
fi
