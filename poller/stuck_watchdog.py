#!/usr/bin/env python3
"""Watch one Telegram message and hint in chat if handling stalls.

Started detached by the poller alongside typing_keeper.py whenever a
message from the owner is claimed. The agent handling the message can
stall for a long time, most confusingly when one of its tool calls is
waiting on a user approval prompt that only exists in the host app:
the agent's turn is frozen, so from the chat it looks like the bot
read the message, typed for a while, then vanished. This watchdog is
the out-of-band observer for exactly that case: it polls handled.txt,
and if the message is still unhandled after --threshold seconds it
posts ONE hint into the same chat (replying to the stalled message)
saying it is still being worked on and that an approval prompt in the
host app may be waiting. It then keeps watching and exits as soon as
the message is marked handled, or when --max-secs expires.

It deliberately does not claim to know WHY handling stalled. A
genuinely long task trips the same hint, so the wording stays
conditional. Pure script: no agent turn, no tokens.

Usage: stuck_watchdog.py <chat_id> <chat_id:message_id>
       [--threshold 300] [--interval 15] [--max-secs 7200]

Best-effort like typing_keeper.py: never raises, logs start/stop and
the hint to stuck_watchdog.log, and a second watchdog for the same
message exits at once (pidfile in /tmp).
"""

import argparse
import os
import sys
import time

WEBHOOK_DIR = os.path.dirname(os.path.abspath(__file__))
HANDLED = os.path.join(WEBHOOK_DIR, "handled.txt")
LOG = os.path.join(WEBHOOK_DIR, "stuck_watchdog.log")

sys.path.insert(0, os.path.join(WEBHOOK_DIR, "..", "bin"))
import telegram  # noqa: E402

HINT = os.environ.get(
    "STUCK_HINT",
    "This message is taking a while — I'm still working on it. "
    "If there's an approval card waiting in the Muse app, "
    "tap it so I can continue.")


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
    pidfile = "/tmp/tg-stuck-%s.pid" % key.replace(":", "_")
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
    parser = argparse.ArgumentParser(description="Telegram stuck watchdog")
    parser.add_argument("chat_id")
    parser.add_argument("key", help="chat_id:message_id")
    parser.add_argument("--threshold", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--max-secs", type=float, default=7200.0)
    args = parser.parse_args()

    try:
        message_id = int(args.key.split(":", 1)[1])
    except (IndexError, ValueError):
        log("bad key %r" % args.key)
        return 2

    if already_running(args.key):
        log("skip %s watchdog already running" % args.key)
        return 0
    log("start %s chat=%s" % (args.key, args.chat_id))
    deadline = time.time() + args.max_secs
    hint_at = time.time() + args.threshold
    hint_sent = False
    while time.time() < deadline:
        if is_handled(args.key):
            log("stop %s handled" % args.key)
            return 0
        if not hint_sent and time.time() >= hint_at:
            try:
                telegram.call("sendMessage", {
                    "chat_id": args.chat_id,
                    "text": HINT,
                    "reply_to_message_id": message_id,
                }, timeout=15)
                log("hint sent for %s" % args.key)
            except Exception as exc:  # noqa: BLE001 - watchdog is best-effort
                log("hint failed for %s: %s" % (args.key, exc))
            hint_sent = True
        time.sleep(args.interval)
    log("stop %s timeout %.0fs" % (args.key, args.max_secs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
