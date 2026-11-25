"""Fuzz and concurrency tests for poller/scan_inbox.py (the v2
review's test gaps #3 and the missing scenarios): randomly corrupt
seen.json, pending.json, and inbox.jsonl — the scan must never
crash, never replay a message, and never move the seen marker
backwards. Two concurrent scans must not double-wake."""
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "poller"))
import scan_inbox  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 12345

CORRUPTIONS = [
    "{corrupt",
    "[]",
    "null",
    '"just a string"',
    "12345",
    '{"last_update_id": "abc"}',
    '{"last_update_id": null}',
    '{"last_update_id": -5}',
    '{"entries": "not a dict"}',
    '{"entries": {"x": "not a dict"}}',
    '{"entries": {"1": {}}}',
    '{"entries": {"999999999999999999999": {"payload": {}}}}',
    "",
    "\x00\x01\x02 binary junk",
    '{"last_update_id": 5} trailing garbage',
]


def _msg(update_id):
    return {"update_id": update_id,
            "message": {"message_id": update_id,
                        "date": int(time.time()),
                        "from": {"id": OWNER, "is_bot": False},
                        "chat": {"id": OWNER, "type": "private"},
                        "text": "hello %d" % update_id}}


class FuzzCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.inbox = os.path.join(d, "inbox.jsonl")
        self.seen = os.path.join(d, "seen.json")
        self.pending = os.path.join(d, "pending.json")
        self.handled = os.path.join(d, "handled.txt")
        open(self.handled, "w").close()
        with open(self.inbox, "w") as f:
            for uid in range(1, 31):
                f.write(json.dumps(_msg(uid)) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _scan(self):
        payload, _warnings = scan_inbox.scan(
            self.inbox, self.seen, self.pending, self.handled, OWNER,
            retry_after=600)
        return payload

    def _corrupt(self, rng, path):
        with open(path, "w", errors="replace") as f:
            f.write(rng.choice(CORRUPTIONS))

    def _corrupt_bytes(self, rng, path):
        # Raw-byte corruption, written in binary: truncated UTF-8
        # sequences, lone continuation bytes, NULs. The old fuzz
        # wrote text with errors="replace", so it could never
        # produce invalid UTF-8 — and never saw item A.
        with open(path, "wb") as f:
            f.write(rng.choice(RAW_CORRUPTIONS))


# Raw-byte corruptions for the item-A fuzz: a write cut mid-way
# through a multi-byte (Persian) character, lone continuation
# bytes, invalid starts, NULs — all things errors="replace" text
# writes can never produce.
RAW_CORRUPTIONS = [
    b"\xd9",                    # first byte of a 2-byte char, cut
    b"\xe2\x82",                # first two bytes of a 3-byte char
    b"\xf0\x9f\x98",            # first three bytes of a 4-byte char
    b"\x80\x80\x80",            # lone continuation bytes
    b"\xff\xfe invalid",       # invalid start bytes
    b'{"update_id": 31, "message": {"text": "\xd9\x85\xd9',  # cut Persian
    b'{"update_id": 32}\n\x00\x01\x02 binary junk\n',
    b'{"update_id": 33, "message": {"text": "hello"}}\n\xd9',
    b"\x00",
    b"",
]


class TestScanFuzz(FuzzCase):
    def test_random_corruption_never_crashes_or_replays(self):
        # 200 rounds of random corruption across the three files.
        # Invariants: the scan never raises; the seen marker never
        # moves backwards; no update is woken twice except as a
        # redelivery (which carries "redelivery": true).
        rng = random.Random(20261125)
        seen_before = 0
        woken = set()
        for round_no in range(200):
            target = rng.choice(["seen", "pending", "inbox", "none"])
            if target == "seen":
                self._corrupt(rng, self.seen)
            elif target == "pending":
                self._corrupt(rng, self.pending)
            elif target == "inbox":
                # Corrupt one random line of the inbox.
                with open(self.inbox) as f:
                    lines = f.readlines()
                if lines:
                    i = rng.randrange(len(lines))
                    lines[i] = rng.choice(CORRUPTIONS) + "\n"
                    with open(self.inbox, "w") as f:
                        f.writelines(lines)
            # Occasionally mark a message handled, so pending clears.
            if rng.random() < 0.3 and woken:
                uid = rng.choice(sorted(woken))
                with open(self.handled, "a") as f:
                    f.write(f"{OWNER}:{uid}\n")
            payload = self._scan()  # must never raise
            self.assertGreaterEqual(payload["seen"], seen_before,
                                    "round %d: seen moved backwards"
                                    % round_no)
            seen_before = payload["seen"]
            for msg in payload["new_messages"]:
                uid = msg["update_id"]
                if uid in woken and not msg.get("redelivery"):
                    self.fail("round %d: update %d replayed without "
                              "a redelivery flag" % (round_no, uid))
                woken.add(uid)

    def test_raw_byte_corruption_never_crashes_or_replays(self):
        # A: 100 rounds of RAW-byte corruption (truncated UTF-8,
        # lone continuation bytes, NULs) across seen, pending,
        # and the inbox. The scan must never raise (a
        # UnicodeDecodeError used to kill it) and the seen marker
        # must never move backwards.
        rng = random.Random(20261126)
        seen_before = 0
        woken = set()
        for round_no in range(100):
            target = rng.choice(["seen", "pending", "inbox", "none"])
            if target in ("seen", "pending"):
                path = self.seen if target == "seen" else self.pending
                self._corrupt_bytes(rng, path)
            elif target == "inbox":
                # Replace one random inbox line with raw bytes.
                with open(self.inbox, "rb") as f:
                    lines = f.readlines()
                if lines:
                    i = rng.randrange(len(lines))
                    lines[i] = rng.choice(RAW_CORRUPTIONS) + b"\n"
                    with open(self.inbox, "wb") as f:
                        f.writelines(lines)
            if rng.random() < 0.3 and woken:
                uid = rng.choice(sorted(woken))
                with open(self.handled, "a") as f:
                    f.write(f"{OWNER}:{uid}\n")
            payload = self._scan()  # must never raise
            self.assertGreaterEqual(payload["seen"], seen_before,
                                    "round %d: seen moved backwards"
                                    % round_no)
            seen_before = payload["seen"]
            for msg in payload["new_messages"]:
                uid = msg["update_id"]
                if uid in woken and not msg.get("redelivery"):
                    self.fail("round %d: update %d replayed without "
                              "a redelivery flag" % (round_no, uid))
                woken.add(uid)

    def test_truncated_utf8_tail_scans_clean(self):
        # A, directly: an inbox whose last line ends mid-way
        # through a Persian character (a write cut by SIGKILL)
        # must scan the valid lines and skip the bad tail.
        with open(self.inbox, "ab") as f:
            f.write(b'{"update_id": 31, "message": {"text": "\xd9')
        payload = self._scan()
        self.assertEqual([m["update_id"] for m in payload["new_messages"]],
                         list(range(1, 21)))  # per-wake cap
        self.assertEqual(payload["seen"], 20)

    def test_wrong_shape_and_deep_lines_never_crash(self):
        # Valid JSON with the wrong shape (arrays, scalars,
        # nested wrong types) and a deeply nested line that
        # exhausts the JSON parser: the scan skips them and
        # still delivers the valid updates.
        with open(self.inbox, "a") as f:
            f.write("[]\n")
            f.write('{"update_id": 31, "message": []}\n')
            f.write('{"update_id": 32, "callback_query": []}\n')
            f.write("[" * 20000 + "]" * 20000 + "\n")
            f.write('{"update_id": 33, "message": {"from": [], '
                    '"chat": "x", "text": "hi"}}\n')
        payload = self._scan()  # must never raise
        self.assertEqual(
            [m["update_id"] for m in payload["new_messages"]],
            list(range(1, 21)))
        self.assertEqual(payload["seen"], 20)

    def test_missing_seen_and_pending_with_full_inbox(self):
        # Both markers deleted at once: at most the fresh
        # messages are delivered, never the whole history.
        payload = self._scan()
        self.assertEqual(len(payload["new_messages"]), 20)  # per-wake cap
        os.unlink(self.seen)
        os.unlink(self.pending)
        # Age every inbox message past the recovery window.
        old = int(time.time()) - 48 * 3600
        with open(self.inbox) as f:
            lines = f.readlines()
        with open(self.inbox, "w") as f:
            for line in lines:
                upd = json.loads(line)
                upd["message"]["date"] = old
                f.write(json.dumps(upd) + "\n")
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["seen"], 30)


class TestConcurrentScans(FuzzCase):
    def _scan_cmd(self):
        script = os.path.join(REPO, "poller", "scan_inbox.py")
        return [sys.executable, script,
                "--inbox", self.inbox, "--seen", self.seen,
                "--pending", self.pending, "--handled", self.handled,
                "--chat-id", str(OWNER)]

    def test_scan_with_held_lock_exits_empty(self):
        # Deterministic: with the scan lock held by this process,
        # a scan exits 0 with an empty payload instead of waking.
        import fcntl
        lock_path = self.pending + ".lock"
        with open(lock_path, "a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc = subprocess.run(self._scan_cmd(), capture_output=True,
                                  text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["new_messages"], [])
        self.assertTrue(any("another scan" in w
                            for w in payload["warnings"]))

    def test_two_scans_do_not_double_wake(self):
        # Two scans started at once: whether they overlap (one
        # wins the lock, total 20) or run back to back (20 + 10),
        # no update may ever be woken twice.
        procs = [subprocess.Popen(self._scan_cmd(), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
                 for _ in range(2)]
        outputs = [p.communicate(timeout=30) for p in procs]
        codes = [p.returncode for p in procs]
        self.assertEqual(codes, [0, 0])
        payloads = [json.loads(out.decode()) for out, _ in outputs]
        ids = [m["update_id"] for p in payloads
               for m in p["new_messages"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(len(ids), (20, 30))


if __name__ == "__main__":
    unittest.main()
