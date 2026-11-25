#!/usr/bin/env python3
"""Lightweight Telegram poller for the Muse-Telegram bridge.

Long-polls getUpdates forever and appends each new update from the
owner as one JSON line to inbox.jsonl. A hook watches the inbox and
wakes the agent when a new message arrives. This process uses no
agent turns; an agent is woken only when there is a real message to
answer.

Layout (all under --runtime-dir unless a path is given explicitly):
  config.json       read-only owner config (chat_id) — the single
                    source of truth for who the owner is
  state.json        the poll offset only ({"offset": N})
  inbox.jsonl       claimed updates from the owner
  poller-state.json heartbeat (epoch timestamps, pid, cycles, errors)
  poller.lock       single-instance lock (flock)
  handled.txt       messages the agent has replied to

Reliability rules (see the code review):
- The offset lives in memory; state.json is only persistence. It is
  loaded once at startup and never re-read, so a corrupt file can
  never wipe the offset mid-run. A corrupt state or config at
  startup fails loudly (exit 1) instead of silently defaulting.
- The whole loop body is wrapped in try/except: no single bad
  update, bad offset, or failed write can kill the poller.
- Update-id epoch handling is defensive and unconfirmed
  against real Telegram behaviour. After more than seven days
  without updates, a stale stored offset is dropped once so a
  lower-id epoch could be seen; if Telegram never returns such
  updates, the path simply never activates.
- Only the owner's updates are written to the inbox (strangers
  still advance the offset, so Telegram does not resend them), and
  the inbox is compacted against the hook's seen marker.
- Signalling (read receipt, typing keeper, watchdog) runs in a
  daemon thread so a slow reaction can never stall the poll loop,
  and updates at or below the seen marker are not signalled again.
- Telegram 429 answers are honoured via retry_after; 409 (another
  poller) backs off instead of crashing.
- The heartbeat is written at once at startup and then by a
  separate thread every few seconds — but only until the main
  loop's progress deadline (set before every blocking wait, plus
  a grace period). A bounded wait (a long poll, a 429 backoff)
  keeps beating; a main loop stuck past its deadline goes stale,
  so the hook can kill and restart it instead of trusting a
  live-looking heartbeat forever.
- A keeper and a watchdog are started per message, not per chat,
  on purpose: a per-chat keeper would need to know when ALL of
  the chat's messages are handled, which means consulting the
  hook's pending state — a coupling that adds failure modes for
  the price of one short-lived process per message. Each keeper
  exits as soon as its own message is handled.

Usage:
  poller.py --config config.json --state state.json \
            --inbox inbox.jsonl --log poller.log \
            --runtime-dir runtime [--seen seen.json] [--chat-id N]
"""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_common  # noqa: E402
import telegram  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_ROTATE_BYTES = 5 * 1024 * 1024

_stop = threading.Event()
_in_poll = threading.Event()
_heartbeat_lock = threading.Lock()


def _handle_signal(signum, _frame):
    # A signal arriving while getUpdates is blocked must end the
    # poll at once: just setting the stop flag leaves the process
    # alive until the (up to 60 s) read returns, so the hook's
    # 5-second kill window is missed and systemd-style stops hang.
    # SystemExit is a BaseException, so it passes straight through
    # the loop's `except Exception` guards.
    _stop.set()
    if _in_poll.is_set():
        raise SystemExit(0)


def log_line(log_path, text):
    """Append one timestamped line, rotating the log past 5 MB.
    (Exceptions arriving here are already token-redacted by
    telegram.call, so no secret can leak through a log line.)"""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        try:
            if (os.path.exists(log_path)
                    and os.path.getsize(log_path) > LOG_ROTATE_BYTES):
                os.replace(log_path, log_path + ".1")
        except OSError:
            pass
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {text}\n")
    except OSError:
        pass


def load_owner_id(config_path, override=None):
    """Read the owner chat id from config (or the --chat-id
    override). Returns the id, or None when it is missing or
    invalid. Never defaults to 0. Validation is shared with the
    hook and setup.sh via bridge_common.parse_chat_id, so a
    digit string ("12345") is valid everywhere or nowhere."""
    if override is not None:
        return bridge_common.parse_chat_id(override)
    config = bridge_common.load_json_file(config_path)
    if not config:
        return None
    return bridge_common.parse_chat_id(config.get("chat_id"))


def load_offset(state_path):
    """Read the poll offset. Returns an int, or None when the
    state file is missing or corrupt. Never defaults to 0."""
    state = bridge_common.load_json_file(state_path)
    if state is None:
        return None
    return bridge_common.parse_offset(state.get("offset"))


def save_offset(state_path, offset, log_path):
    """Persist the offset atomically (offset only; the config
    holds the chat id). Returns True on success, False on
    failure — a failed save is logged, never raised."""
    try:
        bridge_common.atomic_write_json(state_path, {"offset": offset})
        return True
    except OSError as exc:
        log_line(log_path, f"cannot save offset {offset}: {exc}")
        return False


def write_heartbeat(state_file, info):
    with _heartbeat_lock:
        snapshot = dict(info)
    try:
        bridge_common.atomic_write_json(state_file, snapshot)
    except OSError:
        pass


def _set_progress_deadline(info, seconds):
    """The main loop promises progress within `seconds`: the
    heartbeat thread only refreshes beat_at until then. A bounded
    wait (a 50 s long poll, a 429 backoff) sets a deadline past
    its end, so a healthy poller keeps beating; a main loop stuck
    past its deadline stops beating and goes stale, so the hook
    can kill and restart it instead of trusting a live-looking
    heartbeat forever (review item D)."""
    with _heartbeat_lock:
        info["progress_deadline"] = time.time() + seconds


def heartbeat_loop(state_file, info, interval):
    """Keep the heartbeat fresh from a background thread, but
    only until the main loop's progress deadline: beating past
    it would hide a hung poller from the hook's stale check."""
    while not _stop.wait(interval):
        now = time.time()
        with _heartbeat_lock:
            deadline = info.get("progress_deadline", float("inf"))
            if now <= deadline:
                info["beat_at"] = now
        write_heartbeat(state_file, info)


def read_seen_state(seen_path):
    """The hook's seen state: (last_update_id, recent_ids,
    epoch_boundary_at). Missing/corrupt markers return
    (None, set(), None). Legacy markers with only
    last_update_id use the file mtime as the epoch boundary,
    matching scan_inbox, so compaction and signalling judge
    fresh lower-id lines the same way the scan does."""
    if not seen_path:
        return None, set(), None
    seen = bridge_common.load_json_file(seen_path)
    if seen is None:
        return None, set(), None
    value = bridge_common.parse_offset(seen.get("last_update_id"))
    if value is None:
        return None, set(), None
    recent = set()
    raw = seen.get("recent_update_ids")
    if isinstance(raw, list):
        for item in raw:
            parsed = bridge_common.parse_offset(item)
            if parsed is not None:
                recent.add(parsed)
    boundary = bridge_common.parse_timestamp(
        seen.get("epoch_boundary_at"))
    if boundary is None:
        try:
            boundary = os.path.getmtime(seen_path)
        except OSError:
            boundary = None
    return value, recent, boundary


def read_seen(seen_path):
    """Read the hook's seen marker. Returns the last confirmed
    update id, or None when the file is missing or corrupt."""
    return read_seen_state(seen_path)[0]


def read_seen_recent_ids(seen_path):
    """The recent update ids from the seen marker (epoch-reset
    state), as a set. Empty for legacy/corrupt markers. The
    signal loop uses it to avoid re-firing receipts for updates
    the scan already processed, without suppressing a fresh
    epoch-reset update below the marker."""
    return read_seen_state(seen_path)[1]


def compact_inbox(inbox_path, seen_id, log_path, recent_ids=None,
                  epoch_boundary_at=None, now=None):
    """Drop inbox lines at or below the seen marker (already
    delivered), keeping the file bounded. Atomic replace, so a
    concurrent reader sees either the old or the new file, never
    a mix. Only runs when the inbox is large. Lines are handled
    as bytes: a truncated UTF-8 byte must never make compaction
    (or the scan) fail on the whole file (review item A).

    Epoch consistency: a fresh lower-id line that the scan would
    still deliver as an epoch reset (received after the epoch
    boundary, within the recovery age, and not already in
    recent_ids) is preserved even though its id is at or below
    the marker. Compaction must not delete it before the scan
    has had a chance to handle it."""
    lines = bridge_common.read_inbox_lines(inbox_path)
    if len(lines) <= bridge_common.MAX_INBOX_LINES:
        return
    if seen_id is None:
        keep = lines[-bridge_common.MAX_INBOX_LINES // 2:]
    else:
        recent = set(recent_ids or ())
        now = time.time() if now is None else now
        recover_cutoff = now - bridge_common.MAX_MESSAGE_AGE_SECS
        keep = []
        for raw in lines:
            try:
                parsed = json.loads(
                    bridge_common.decode_inbox_line(raw))
            except (ValueError, RecursionError):
                # Invalid or deeply nested line: drop it during
                # compaction instead of failing compaction (and
                # retrying it) forever.
                continue
            if not isinstance(parsed, dict):
                continue
            uid = parsed.get("update_id")
            if not isinstance(uid, int) or isinstance(uid, bool):
                continue
            if uid > seen_id:
                keep.append(raw)
                continue
            if uid in recent or epoch_boundary_at is None:
                continue
            received = bridge_common.parse_timestamp(
                parsed.get("received_at"))
            if received is not None \
                    and received > epoch_boundary_at \
                    and received >= recover_cutoff:
                keep.append(raw)
    tmp_path = inbox_path + ".tmp.%d" % os.getpid()
    try:
        with open(tmp_path, "wb") as f:
            for raw in keep:
                f.write(raw + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, inbox_path)
        log_line(log_path,
                 f"inbox compacted: {len(lines)} -> {len(keep)} lines")
    except OSError as exc:
        log_line(log_path, f"inbox compaction failed: {exc}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def run_maintenance(inbox_path, seen_path, handled_path, runtime_dir,
                    log_path):
    """One maintenance pass: compact the inbox, prune handled.txt,
    clean up stale lock files. Called from the poll loop after
    20 non-empty cycles OR every 10 minutes — including on empty
    cycles, so a quiet bridge still gets it (the old code put
    this after the empty-cycle `continue` and it never ran)."""
    seen_id, recent_ids, epoch_boundary_at = read_seen_state(seen_path)
    compact_inbox(inbox_path, seen_id, log_path,
                  recent_ids=recent_ids,
                  epoch_boundary_at=epoch_boundary_at)
    prune_handled(handled_path, log_path)
    bridge_common.cleanup_stale_locks(
        os.path.join(runtime_dir, "locks"))


def prune_handled(handled_path, log_path):
    """Keep handled.txt bounded (last MAX_HANDLED_LINES entries).
    Old entries are long handled; the hook only consults recent
    ones, so pruning is safe.

    The replace runs under the same flock every append takes
    (bridge_common.append_handled), so a concurrent append can
    never be lost by the rewrite."""
    import fcntl

    try:
        with open(handled_path, encoding="utf-8",
                  errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= bridge_common.MAX_HANDLED_LINES * 2:
        return
    lock_path = handled_path + ".lock"
    try:
        lock_file = open(lock_path, "a", encoding="utf-8")
    except OSError:
        return
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        # Re-read under the lock: an append may have landed since
        # the first read, and it must survive the replace.
        try:
            with open(handled_path, encoding="utf-8",
                      errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return
        keep = lines[-bridge_common.MAX_HANDLED_LINES:]
        tmp_path = handled_path + ".tmp.%d" % os.getpid()
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(keep)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, handled_path)
        except OSError as exc:
            log_line(log_path, f"handled prune failed: {exc}")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    finally:
        lock_file.close()


def append_inbox(inbox_path, updates):
    """Append updates to the inbox, one JSON line each. Returns
    True on success, False on failure — and a failed write never
    leaves a partial line behind: the file is truncated back to
    its pre-write size, and a previous write that ended without
    a newline is repaired first, so the retry cannot glue two
    updates into one invalid JSON line.

    Binary mode on purpose (review item A): checking the last
    byte in text mode reads one CHARACTER, and a file ending
    mid-way through a multi-byte (e.g. Persian) character raises
    UnicodeDecodeError — which is not an OSError, so the old
    code logged it, never advanced the offset, and retried the
    same update forever. In binary mode the last byte is just
    compared to b"\\n", and no decode ever happens here."""
    # Stamp each claimed update with its arrival time. The scan
    # judges freshness by received_at (callbacks included), so
    # an old tap can no longer hide behind its button message's
    # date when the seen marker is lost (item B).
    received_at = time.time()
    stamped = []
    for upd in updates:
        if isinstance(upd, dict):
            copy = dict(upd)
            if bridge_common.parse_timestamp(
                    copy.get("received_at")) is None:
                copy["received_at"] = received_at
            stamped.append(copy)
        else:
            stamped.append(upd)
    lines = [(json.dumps(upd, ensure_ascii=False) + "\n").encode("utf-8")
             for upd in stamped]
    try:
        with open(inbox_path, "ab+") as f:
            f.seek(0, os.SEEK_END)
            size_before = f.tell()
            try:
                if size_before > 0:
                    f.seek(size_before - 1)
                    if f.read(1) != b"\n":
                        f.seek(size_before)
                        f.write(b"\n")
                        f.flush()
                for line in lines:
                    f.write(line)
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                # Roll back to the pre-write size: a partial line
                # would corrupt the next scan and lose an update.
                try:
                    f.flush()
                except OSError:
                    pass
                try:
                    os.ftruncate(f.fileno(), size_before)
                except OSError:
                    pass
                return False
    except OSError:
        return False
    return True


def start_detached(script, chat_id, key, runtime_dir, log_path):
    """Start a keeper/watchdog detached. Options come before a
    "--" separator and the positionals after it: in group mode
    the chat id is negative and the key looks like
    "-100...:7", which argparse would otherwise treat as an
    unknown option and exit rc=2 (hidden by DEVNULL stderr).
    Returns the Popen object, or None when the spawn failed."""
    try:
        return subprocess.Popen(
            [sys.executable, os.path.join(SCRIPT_DIR, script),
             "--runtime-dir", runtime_dir, "--", str(chat_id), key],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception as exc:  # noqa: BLE001 - best-effort spawn
        log_line(log_path, f"cannot start {script} for {key}: {exc}")
        return None


def signal_message(chat_id, message_id, runtime_dir, log_path):
    """Read receipt plus typing keeper plus stuck watchdog, all
    best-effort. A failure here must never block the message flow.
    Runs in a daemon thread so a slow reaction cannot stall the
    poll loop."""
    key = f"{chat_id}:{message_id}"
    try:
        telegram.call("setMessageReaction", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": "👀"}],
        }, timeout=10)
    except Exception as exc:  # noqa: BLE001 - best-effort signal
        log_line(log_path, f"read receipt failed for {key}: {exc}")
    start_detached("typing_keeper.py", chat_id, key, runtime_dir, log_path)
    start_detached("stuck_watchdog.py", chat_id, key, runtime_dir, log_path)


def signal_async(chat_id, message_id, runtime_dir, log_path):
    thread = threading.Thread(
        target=signal_message,
        args=(chat_id, message_id, runtime_dir, log_path),
        daemon=True)
    thread.start()


def _valid_update_ids(updates):
    return [u.get("update_id") for u in updates
            if isinstance(u, dict)
            and isinstance(u.get("update_id"), int)
            and not isinstance(u.get("update_id"), bool)]


def compute_new_offset(offset, updates):
    """Advance the offset past a batch of updates. Returns
    max(offset, max(valid update_id)) + 1, or the unchanged
    offset when no update carries a valid integer id (never
    resets to 1)."""
    valid_ids = _valid_update_ids(updates)
    if valid_ids:
        return max(offset, max(valid_ids)) + 1
    return offset


def epoch_reset_offset(offset, updates, now=None):
    """Detect a Telegram update-id epoch reset. Returns the new
    offset (max id + 1) when EVERY valid id in the batch is
    below the stored offset and the batch looks fresh (a recent
    message date, or a callback claimed just now) — the stored
    offset belongs to a previous id epoch, and keeping it would
    make getUpdates skip the new epoch's updates forever.
    Returns None for normal batches and for stale replays."""
    valid_ids = _valid_update_ids(updates)
    if not valid_ids or max(valid_ids) >= offset:
        return None
    now = time.time() if now is None else now
    cutoff = now - bridge_common.MAX_MESSAGE_AGE_SECS
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        if isinstance(upd.get("callback_query"), dict):
            return max(valid_ids) + 1
        msg = upd.get("message")
        if isinstance(msg, dict):
            date = bridge_common.parse_timestamp(msg.get("date"))
            if date is not None and date >= cutoff:
                return max(valid_ids) + 1
    return None


def should_drop_offset_for_silence(offset, last_update_at, now=None,
                                   already_dropped=False):
    """True when a stored offset should be dropped once after a
    long silence. The epoch-reset path is defensive and
    unconfirmed against real Telegram behaviour; with a high
    stored offset getUpdates may never return a lower-id epoch
    at all, so after OFFSET_SILENCE_DROP_SECS without any
    updates the poller polls once from offset 0. The scan's
    seen/recent/handled checks still stop old history from
    being re-woken if Telegram resends it."""
    if already_dropped or not offset or last_update_at is None:
        return False
    now = time.time() if now is None else now
    return now - last_update_at > bridge_common.OFFSET_SILENCE_DROP_SECS


def state_last_update_at(state_path, fallback):
    """Approximate the last update time from state.json's mtime
    (the offset is saved whenever a batch advances it). Falls
    back to the poller start time when the state file cannot be
    stat'ed, so a first run never drops its offset."""
    try:
        return os.path.getmtime(state_path)
    except OSError:
        return fallback


def signal_target(payload, owner_chat_id):
    """The (chat_id, message_id) the receipt/keeper/watchdog
    should target for a classified payload, or None. The chat
    is the payload's own chat (the group chat in group mode),
    so typing and the stuck hint land where the message came
    from, not in the owner's private chat."""
    if not isinstance(payload, dict) or payload.get("kind") == "callback":
        return None
    message_id = payload.get("message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool) \
            or not message_id:
        return None
    chat_id = payload.get("chat_id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) \
            or chat_id == 0:
        chat_id = owner_chat_id
    return chat_id, message_id


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    parser = argparse.ArgumentParser(description="Telegram lightweight poller")
    parser.add_argument("--config", required=True,
                        help="Owner config file (chat_id)")
    parser.add_argument("--state", required=True,
                        help="Offset state file")
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--runtime-dir", default=None,
                        help="Working dir for heartbeat/lock/handled "
                             "(default: the script's directory)")
    parser.add_argument("--seen", default=None,
                        help="Hook seen-marker file (for inbox "
                             "compaction and replay skipping)")
    parser.add_argument("--chat-id", type=int, default=None,
                        help="Owner chat id (overrides config)")
    parser.add_argument("--allow-groups", action="store_true",
                        help="Also claim the owner's group/supergroup "
                             "messages (BRIDGE_ALLOW_GROUPS)")
    parser.add_argument("--heartbeat-secs", type=float, default=15.0,
                        help="Heartbeat refresh interval (seconds)")
    parser.add_argument("--poll-timeout", type=float, default=60.0,
                        help="Client timeout for one getUpdates call "
                             "(seconds); the heartbeat deadline covers "
                             "this plus the grace below")
    parser.add_argument("--progress-grace-secs", type=float, default=60.0,
                        help="Extra seconds past a blocking wait's "
                             "expected end before the heartbeat thread "
                             "stops beating (review item D)")
    args = parser.parse_args()

    config_path = os.path.abspath(args.config)
    state_path = os.path.abspath(args.state)
    inbox_path = os.path.abspath(args.inbox)
    log_path = os.path.abspath(args.log)
    runtime_dir = os.path.abspath(args.runtime_dir or SCRIPT_DIR)
    seen_path = os.path.abspath(args.seen) if args.seen else None
    heartbeat_path = os.path.join(runtime_dir, "poller-state.json")
    handled_path = os.path.join(runtime_dir, "handled.txt")
    lock_path = os.path.join(runtime_dir, "poller.lock")
    os.makedirs(os.path.dirname(inbox_path), exist_ok=True)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    os.makedirs(runtime_dir, exist_ok=True)

    # Fail loudly on bad config/state instead of silently defaulting.
    chat_id = load_owner_id(config_path, args.chat_id)
    if not chat_id:
        log_line(log_path,
                 f"no valid chat_id in {config_path} (and no --chat-id); "
                 "refusing to start")
        print(f"no valid chat_id in {config_path}", file=sys.stderr)
        return 1
    offset = load_offset(state_path)
    if offset is None:
        # A missing state file is fine on first run (start at 0 and
        # let Telegram send only new updates); a corrupt one is not.
        if os.path.exists(state_path):
            log_line(log_path,
                     f"state file {state_path} is corrupt; refusing to "
                     "start (fix or delete it)")
            print(f"state file {state_path} is corrupt", file=sys.stderr)
            return 1
        offset = 0
        save_offset(state_path, offset, log_path)

    # "a" mode: never truncate the lock file before locking it.
    lock_file = open(lock_path, "a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log_line(log_path, "another poller is already running, exiting")
        return 0
    lock_file.write(str(os.getpid()))
    lock_file.flush()

    started_at = time.time()
    heartbeat = {"started_at": started_at, "beat_at": started_at,
                 "last_ok_at": None, "pid": os.getpid(),
                 "cycles": 0, "errors": 0,
                 "progress_deadline": started_at + args.poll_timeout
                 + args.progress_grace_secs}
    # The first heartbeat goes out at once (a hook tick in the
    # first seconds must see a live poller, not a missing file),
    # and a thread keeps it fresh while the main loop blocks.
    write_heartbeat(heartbeat_path, heartbeat)
    hb_thread = threading.Thread(
        target=heartbeat_loop,
        args=(heartbeat_path, heartbeat, args.heartbeat_secs),
        daemon=True)
    hb_thread.start()
    log_line(log_path, f"poller started (pid {os.getpid()})")
    backoff = 5
    cycles_since_maintenance = 0
    last_maintenance = time.time()
    try:
        _run_loop(args, config_path, state_path, inbox_path, log_path,
                  runtime_dir, seen_path, heartbeat_path, handled_path,
                  chat_id, offset, heartbeat, backoff,
                  cycles_since_maintenance, last_maintenance)
    except SystemExit:
        # Raised by the signal handler while blocked in a poll:
        # a clean, fast shutdown, not an error.
        log_line(log_path, "poller stopping (signal)")
        return 0
    log_line(log_path, "poller stopping")
    return 0


def _run_loop(args, config_path, state_path, inbox_path, log_path,
              runtime_dir, seen_path, heartbeat_path, handled_path,
              chat_id, offset, heartbeat, backoff,
              cycles_since_maintenance, last_maintenance):
    """The poll loop, split out of main() so the signal handler's
    SystemExit can end it cleanly."""
    grace = args.progress_grace_secs
    last_update_at = state_last_update_at(state_path, time.time())
    silence_drop_done = False
    while not _stop.is_set():
        cycle_start = time.time()
        try:
            heartbeat["cycles"] += 1
            heartbeat["beat_at"] = time.time()
            # Promise progress by the end of this poll (plus
            # grace): the heartbeat thread beats until then, so a
            # main loop stuck past it goes stale (item D).
            _set_progress_deadline(
                heartbeat, args.poll_timeout + grace)
            if should_drop_offset_for_silence(
                    offset, last_update_at, cycle_start,
                    silence_drop_done):
                log_line(
                    log_path,
                    f"offset dropped after long silence: {offset} -> 0")
                offset = 0
                silence_drop_done = True
                save_offset(state_path, offset, log_path)
            try:
                _in_poll.set()
                try:
                    result = telegram.call(
                        "getUpdates",
                        {"offset": offset, "timeout": 50,
                         "allowed_updates": ["message", "callback_query"]},
                        timeout=args.poll_timeout)
                finally:
                    _in_poll.clear()
            except Exception as exc:  # noqa: BLE001 - poller must never die
                retry_after = telegram.http_retry_after(exc)
                heartbeat["errors"] += 1
                write_heartbeat(heartbeat_path, heartbeat)
                if retry_after is not None:
                    wait = min(max(retry_after, 1), 300)
                    log_line(log_path,
                             f"getUpdates rate-limited (429); waiting "
                             f"{wait}s")
                    _set_progress_deadline(heartbeat, wait + grace)
                    _stop.wait(wait)
                    continue
                if (isinstance(exc, urllib.error.HTTPError)
                        and exc.code == 409):
                    log_line(log_path,
                             "getUpdates 409: another poller holds the "
                             "updates; backing off 30s")
                    _set_progress_deadline(heartbeat, 30 + grace)
                    _stop.wait(30)
                    continue
                log_line(log_path,
                         f"getUpdates failed: {exc}; retry in {backoff}s")
                _set_progress_deadline(heartbeat, backoff + grace)
                _stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 5
            if not isinstance(result, dict) or not result.get("ok"):
                heartbeat["errors"] += 1
                write_heartbeat(heartbeat_path, heartbeat)
                log_line(log_path, f"getUpdates not ok: {result}; retry in 5s")
                _set_progress_deadline(heartbeat, 5 + grace)
                _stop.wait(5)
                continue
            heartbeat["last_ok_at"] = time.time()
            write_heartbeat(heartbeat_path, heartbeat)
            updates = result.get("result") or []
            if not updates:
                # Long polling normally blocks ~50s, so an instant
                # empty return means the server is misbehaving (or
                # the call is mocked). Enforce a minimum cycle time
                # so the poller never busy-loops.
                elapsed = time.time() - cycle_start
                if elapsed < 1:
                    _stop.wait(1 - elapsed)
                # Time-based maintenance runs even on empty cycles:
                # a quiet bridge must still compact and prune.
                if time.time() - last_maintenance >= 600:
                    last_maintenance = time.time()
                    run_maintenance(inbox_path, seen_path, handled_path,
                                    runtime_dir, log_path)
                continue
            # The offset advances past ALL updates (including
            # strangers'), but only the owner's updates are written
            # to the inbox. A fresh batch entirely below the
            # stored offset means Telegram reused lower ids (an
            # epoch reset): adopt the new epoch's offset instead
            # of skipping its updates forever.
            reset_offset = epoch_reset_offset(offset, updates)
            if reset_offset is not None:
                new_offset = reset_offset
                log_line(log_path,
                         f"update-id epoch reset: offset {offset} "
                         f"-> {new_offset}")
            else:
                new_offset = compute_new_offset(offset, updates)
            if new_offset == offset and updates:
                log_line(log_path,
                         "updates without update_id; not advancing offset")
            owner_updates = []
            owner_payloads = []
            for upd in updates:
                kind, payload = bridge_common.classify(
                    upd, chat_id, allow_groups=args.allow_groups)
                if kind is not None:
                    owner_updates.append(upd)
                    owner_payloads.append((upd, payload))
            if owner_updates:
                if not append_inbox(inbox_path, owner_updates):
                    heartbeat["errors"] += 1
                    write_heartbeat(heartbeat_path, heartbeat)
                    log_line(log_path,
                             "cannot write inbox; retry in 5s")
                    _set_progress_deadline(heartbeat, 5 + grace)
                    _stop.wait(5)
                    continue
            offset = new_offset
            if not save_offset(state_path, offset, log_path):
                heartbeat["errors"] += 1
                write_heartbeat(heartbeat_path, heartbeat)
            log_line(log_path,
                     f"claimed {len(updates)} updates "
                     f"({len(owner_updates)} owner), offset -> {offset}")
            last_update_at = time.time()
            silence_drop_done = False
            seen_id = read_seen(seen_path)
            seen_recent = read_seen_recent_ids(seen_path)
            for upd, payload in owner_payloads:
                uid = upd.get("update_id", 0)
                # A replayed update (crash between inbox write and
                # offset save) must not re-fire the receipt, keeper,
                # or watchdog for an already-processed message.
                # With v5 seen state the recent-id set decides, so
                # a fresh epoch-reset update below the marker still
                # signals; a legacy marker keeps the old rule.
                if uid in seen_recent:
                    continue
                if seen_id is not None and uid <= seen_id \
                        and not seen_recent:
                    continue
                target = signal_target(payload, chat_id)
                if target is not None:
                    signal_async(target[0], target[1], runtime_dir,
                                 log_path)
            cycles_since_maintenance += 1
            # Maintenance runs after 20 non-empty cycles OR every
            # 10 minutes, whichever comes first.
            if (cycles_since_maintenance >= 20
                    or time.time() - last_maintenance >= 600):
                cycles_since_maintenance = 0
                last_maintenance = time.time()
                run_maintenance(inbox_path, seen_path, handled_path,
                                runtime_dir, log_path)
        except Exception as exc:  # noqa: BLE001 - the loop never dies
            heartbeat["errors"] += 1
            try:
                write_heartbeat(heartbeat_path, heartbeat)
            except Exception:  # noqa: BLE001 - heartbeat is best-effort
                pass
            log_line(log_path, f"poll loop error: {exc}; continuing")
            _set_progress_deadline(heartbeat, 5 + grace)
            _stop.wait(5)


if __name__ == "__main__":
    sys.exit(main())
