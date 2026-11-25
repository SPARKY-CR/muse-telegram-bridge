#!/usr/bin/env python3
"""Keep Telegram 'typing...' alive while the agent handles a message.

Telegram shows a typing chat action for about 5 seconds, but agent
turns take longer, so a single action sent when a message is pulled
expires long before the reply lands. This keeper re-sends
sendChatAction typing every few seconds until the message is marked
handled (its key in handled.txt) or a time budget expires.

Usage: typing_keeper.py <chat_id> <chat_id:message_id>
       [--interval 4] [--max-secs 600]

Started detached by the poller when a message from the owner is
claimed. Best-effort: never raises, logs start/stop to
typing_keeper.log. A second keeper for the same message exits at
once (pidfile in /tmp).
"""

import argparse
import os
import sys
import time

WEBHOOK_DIR = os.path.dirname(os.path.abspath(__file__))
HANDLED = os.path.join(WEBHOOK_DIR, "handled.txt")
LOG = os.path.join(WEBHOOK_DIR, "typing_keeper.log")

sys.path.insert(0, os.path.join(WEBHOOK_DIR, "..", "bin"))
import telegram  # noqa: E402


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + msg + "\n")
    except OSError:
        pass


def is_handled(key):
    try:
        with open(HANDLED, encoding="utf-8") as f:
            return key in {line.strip() for line in f}
    except OSError:
        return False


def already_running(key):
    pidfile = "/tmp/tg-typing-%s.pid" % key.replace(":", "_")
    try:
        with open(pidfile, encoding="utf-8") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        pass
    try:
        with open(pidfile, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return False


def main():
    parser = argparse.ArgumentParser(description="Telegram typing keeper")
    parser.add_argument("chat_id")
    parser.add_argument("key", help="chat_id:message_id")
    parser.add_argument("--interval", type=float, default=4.0)
    parser.add_argument("--max-secs", type=float, default=600.0)
    args = parser.parse_args()

    if already_running(args.key):
        log("skip %s keeper already running" % args.key)
        return 0
    log("start %s chat=%s" % (args.key, args.chat_id))
    deadline = time.time() + args.max_secs
    while time.time() < deadline:
        if is_handled(args.key):
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
            if is_handled(args.key):
                log("stop %s handled" % args.key)
                return 0
            time.sleep(0.5)
    log("stop %s timeout %.0fs" % (args.key, args.max_secs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
