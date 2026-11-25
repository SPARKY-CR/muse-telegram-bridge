#!/usr/bin/env python3
"""Keep Telegram 'typing...' alive while the agent handles a message.

Telegram shows a typing chat action for about 5 seconds, but agent
turns take longer, so a single action sent when a message is pulled
expires long before the reply lands. This keeper re-sends
sendChatAction typing every few seconds until the message is marked
handled (its key in handled.txt) or a time budget expires.

Usage: typing_keeper.py <chat_id> <chat_id:message_id>
       [--interval 4] [--max-secs 600] [--runtime-dir DIR]

Started detached by the poller when a message from the owner is
claimed. Best-effort: never raises, logs start/stop to
typing_keeper.log. A second keeper for the same message exits at
once (flock on a lock file under the runtime dir, named by a hash
of the key — no predictable /tmp pidfiles).

The handled.txt check is mtime-cached, so the keeper does not
re-read a growing file twice a second. The runtime dir is passed
explicitly (--runtime-dir) so the keeper always watches the same
handled.txt the agent writes, wherever the scripts live.
"""

import argparse
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "bin"))
sys.path.insert(0, SCRIPT_DIR)
import bridge_common  # noqa: E402
import telegram  # noqa: E402


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Telegram typing keeper")
    parser.add_argument("chat_id")
    parser.add_argument("key", help="chat_id:message_id")
    parser.add_argument("--interval", type=float, default=4.0)
    parser.add_argument("--max-secs", type=float, default=600.0)
    parser.add_argument("--runtime-dir", default=None,
                        help="Runtime dir for handled.txt, logs, locks "
                             "(default: the script's directory)")
    args = parser.parse_args()

    runtime_dir = os.path.abspath(args.runtime_dir or SCRIPT_DIR)
    handled = bridge_common.HandledSet(
        os.path.join(runtime_dir, "handled.txt"))
    log_path = os.path.join(runtime_dir, "typing_keeper.log")

    def log(msg):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + msg + "\n")
        except OSError:
            pass

    lock = bridge_common.acquire_singleton(
        os.path.join(runtime_dir, "locks"), "typing:" + args.key)
    if lock is None:
        log("skip %s keeper already running" % args.key)
        return 0
    log("start %s chat=%s" % (args.key, args.chat_id))
    deadline = time.time() + args.max_secs
    while time.time() < deadline:
        if handled.contains(args.key):
            log("stop %s handled" % args.key)
            return 0
        try:
            telegram.call("sendChatAction",
                          {"chat_id": args.chat_id, "action": "typing"},
                          timeout=10)
        except Exception:  # noqa: BLE001 - keeper is best-effort
            pass
        end = time.time() + args.interval
        while time.time() < end:
            if handled.contains(args.key):
                log("stop %s handled" % args.key)
                return 0
            time.sleep(0.5)
    log("stop %s timeout %.0fs" % (args.key, args.max_secs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
