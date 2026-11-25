#!/usr/bin/env python3
"""Lightweight Telegram poller for the custom.telegram connector.

Long-polls getUpdates forever and appends each new update as one JSON
line to inbox.jsonl. A hook watches the inbox and wakes the agent when
a new message from the owner arrives. This process uses no agent turns;
an agent is woken only when there is a real message to answer.

The poller re-reads the offset from state.json before every poll and
advances it only after the inbox write is fsynced, so Telegram never
resends a claimed update and a concurrent poller never double-claims.

When a message from the owner is claimed, the poller also:
- reacts to it with 👀 (read receipt, best-effort),
- starts a detached typing_keeper that keeps the typing indicator
  alive until the message is marked handled,
- starts a detached stuck_watchdog that hints in chat if the message
  stays unhandled for too long.

Reliability: a single-instance lock (poller.lock) prevents two
pollers from running, and a heartbeat file (poller-state.json) is
written after every cycle so the hook can tell a hung poller from a
healthy one. On any error it logs and retries with backoff; it never
exits on its own. The webhook hook restarts it if the process dies
or the heartbeat goes stale.

Usage:
  poller.py --state ../state.json --inbox inbox.jsonl --log poller.log
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "bin"))
import telegram  # noqa: E402

WEBHOOK_DIR = os.path.dirname(os.path.abspath(__file__))


def log_line(log_path, text):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {text}\n")
    except OSError:
        pass


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load_state(state_path):
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass
    return {"chat_id": 0, "offset": 0}


def save_offset(state_path, offset):
    state = load_state(state_path)
    state["offset"] = offset
    tmp_path = state_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, state_path)
    try:
        fd = os.open(state_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def write_heartbeat(state_file, info):
    tmp_path = state_file + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(info, f)
        os.replace(tmp_path, state_file)
    except OSError:
        pass


def start_detached(script, chat_id, key, log_path):
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(WEBHOOK_DIR, script),
             str(chat_id), key],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as exc:
        log_line(log_path, f"cannot start {script} for {key}: {exc}")


def signal_message(chat_id, message_id, log_path):
    """Read receipt plus typing keeper plus stuck watchdog, all
    best-effort. A failure here must never block the message flow."""
    key = f"{chat_id}:{message_id}"
    try:
        telegram.call("setMessageReaction", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": "👀"}],
        }, timeout=10)
    except Exception as exc:  # noqa: BLE001 - best-effort signal
        log_line(log_path, f"read receipt failed for {key}: {exc}")
    start_detached("typing_keeper.py", chat_id, key, log_path)
    start_detached("stuck_watchdog.py", chat_id, key, log_path)


def main():
    parser = argparse.ArgumentParser(description="Telegram lightweight poller")
    parser.add_argument("--state", required=True)
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--chat-id", type=int, default=None,
                        help="Owner chat id (overrides state.json)")
    args = parser.parse_args()

    state_path = os.path.abspath(args.state)
    inbox_path = os.path.abspath(args.inbox)
    log_path = os.path.abspath(args.log)
    heartbeat_path = os.path.join(WEBHOOK_DIR, "poller-state.json")
    lock_path = os.path.join(WEBHOOK_DIR, "poller.lock")
    os.makedirs(os.path.dirname(inbox_path), exist_ok=True)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    lock_file = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log_line(log_path, "another poller is already running, exiting")
        return 0
    lock_file.write(str(os.getpid()))
    lock_file.flush()

    started_at = iso_now()
    heartbeat = {"started_at": started_at, "beat_at": started_at,
                 "last_ok_at": None, "pid": os.getpid(),
                 "cycles": 0, "errors": 0}
    log_line(log_path, f"poller started (pid {os.getpid()})")
    backoff = 5
    while True:
        state = load_state(state_path)
        offset = int(state.get("offset", 0))
        chat_id = args.chat_id or int(state.get("chat_id", 0))
        try:
            result = telegram.call(
                "getUpdates", {"offset": offset, "timeout": 50}, timeout=60)
        except Exception as exc:  # noqa: BLE001 - poller must never die
            heartbeat["errors"] += 1
            heartbeat["beat_at"] = iso_now()
            heartbeat["cycles"] += 1
            write_heartbeat(heartbeat_path, heartbeat)
            log_line(log_path, f"getUpdates failed: {exc}; retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        backoff = 5
        heartbeat["cycles"] += 1
        heartbeat["beat_at"] = iso_now()
        if not isinstance(result, dict) or not result.get("ok"):
            heartbeat["errors"] += 1
            write_heartbeat(heartbeat_path, heartbeat)
            log_line(log_path, f"getUpdates not ok: {result}; retry in 5s")
            time.sleep(5)
            continue
        heartbeat["last_ok_at"] = iso_now()
        write_heartbeat(heartbeat_path, heartbeat)
        updates = result.get("result") or []
        if not updates:
            continue
        try:
            with open(inbox_path, "a", encoding="utf-8") as f:
                for upd in updates:
                    f.write(json.dumps(upd, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            log_line(log_path, f"cannot write inbox: {exc}; retry in 5s")
            time.sleep(5)
            continue
        new_offset = max(u.get("update_id", 0) for u in updates) + 1
        save_offset(state_path, new_offset)
        log_line(log_path,
                 f"claimed {len(updates)} updates, offset -> {new_offset}")
        for upd in updates:
            msg = upd.get("message") or {}
            msg_chat = (msg.get("chat") or {}).get("id")
            message_id = msg.get("message_id")
            if msg_chat == chat_id and message_id:
                signal_message(chat_id, message_id, log_path)


if __name__ == "__main__":
    main()
