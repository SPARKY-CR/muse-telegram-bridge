"""Tests for poller/poller.py: owner config, offset handling,
inbox compaction, and handled pruning. The live poll loop is not
started here; these cover the state functions the loop depends on
(the review's items #2, #3, #13, #18, #21)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "poller"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import poller  # noqa: E402


class TestOwnerId(unittest.TestCase):
    def test_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w") as f:
                json.dump({"chat_id": 123}, f)
            self.assertEqual(poller.load_owner_id(path), 123)

    def test_override_wins(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w") as f:
                json.dump({"chat_id": 123}, f)
            self.assertEqual(poller.load_owner_id(path, override=456), 456)

    def test_missing_and_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "no.json")
            self.assertIsNone(poller.load_owner_id(missing))
            bad = os.path.join(d, "bad.json")
            with open(bad, "w") as f:
                f.write("{corrupt")
            self.assertIsNone(poller.load_owner_id(bad))
            zero = os.path.join(d, "zero.json")
            with open(zero, "w") as f:
                json.dump({"chat_id": 0}, f)
            # #2: never silently default to 0.
            self.assertIsNone(poller.load_owner_id(zero))
            wrong = os.path.join(d, "wrong.json")
            with open(wrong, "w") as f:
                json.dump({"chat_id": "abc"}, f)
            self.assertIsNone(poller.load_owner_id(wrong))

    def test_digit_string_accepted(self):
        # M4: a digit string is valid everywhere (the hook, the
        # poller, and setup.sh share one validation), so the
        # hook can no longer accept an id the poller rejects.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "str.json")
            with open(path, "w") as f:
                json.dump({"chat_id": "123"}, f)
            self.assertEqual(poller.load_owner_id(path), 123)


class TestOffset(unittest.TestCase):
    def test_load_offset(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with open(path, "w") as f:
                json.dump({"offset": 99}, f)
            self.assertEqual(poller.load_offset(path), 99)

    def test_load_offset_corrupt_is_none(self):
        # #2: a corrupt state file must not become offset 0.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with open(path, "w") as f:
                f.write('{"offset": ')
            self.assertIsNone(poller.load_offset(path))
            path2 = os.path.join(d, "state2.json")
            with open(path2, "w") as f:
                json.dump({"offset": None}, f)
            self.assertIsNone(poller.load_offset(path2))
            path3 = os.path.join(d, "state3.json")
            with open(path3, "w") as f:
                json.dump({"offset": "abc"}, f)
            self.assertIsNone(poller.load_offset(path3))

    def test_save_offset_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            log = os.path.join(d, "log.txt")
            self.assertTrue(poller.save_offset(path, 42, log))
            self.assertEqual(poller.load_offset(path), 42)
            # Offset only: the config holds the chat id.
            with open(path) as f:
                self.assertEqual(json.load(f), {"offset": 42})
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_save_offset_failure_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            # A directory where the state file should be.
            path = os.path.join(d, "state.json")
            os.makedirs(path)
            log = os.path.join(d, "log.txt")
            self.assertFalse(poller.save_offset(path, 1, log))


class TestComputeNewOffset(unittest.TestCase):
    def test_advances(self):
        updates = [{"update_id": 10}, {"update_id": 12}]
        self.assertEqual(poller.compute_new_offset(5, updates), 13)

    def test_never_goes_back(self):
        updates = [{"update_id": 3}]
        self.assertEqual(poller.compute_new_offset(10, updates), 11)

    def test_no_valid_ids_keeps_offset(self):
        # #3: no update with an update_id must not reset to 1.
        updates = [{"foo": 1}, {"update_id": None},
                   {"update_id": "x"}, {"update_id": True}]
        self.assertEqual(poller.compute_new_offset(7, updates), 7)
        self.assertEqual(poller.compute_new_offset(7, []), 7)


class TestReadSeen(unittest.TestCase):
    def test_reads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            with open(path, "w") as f:
                json.dump({"last_update_id": 55}, f)
            self.assertEqual(poller.read_seen(path), 55)

    def test_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(poller.read_seen(os.path.join(d, "no.json")))
            bad = os.path.join(d, "bad.json")
            with open(bad, "w") as f:
                f.write("[]")
            self.assertIsNone(poller.read_seen(bad))
            self.assertIsNone(poller.read_seen(None))


class TestCompactInbox(unittest.TestCase):
    def _write_inbox(self, path, ids):
        with open(path, "w") as f:
            for uid in ids:
                f.write(json.dumps({"update_id": uid}) + "\n")

    def test_compacts_below_seen(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            log = os.path.join(d, "log.txt")
            self._write_inbox(inbox, range(1, 2501))
            poller.compact_inbox(inbox, 2000, log)
            with open(inbox) as f:
                remaining = [json.loads(line)["update_id"]
                             for line in f if line.strip()]
            self.assertEqual(remaining, list(range(2001, 2501)))

    def test_small_inbox_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            log = os.path.join(d, "log.txt")
            self._write_inbox(inbox, range(1, 100))
            poller.compact_inbox(inbox, 50, log)
            with open(inbox) as f:
                self.assertEqual(len(f.readlines()), 99)

    def test_no_seen_keeps_tail(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            log = os.path.join(d, "log.txt")
            self._write_inbox(inbox, range(1, 2501))
            poller.compact_inbox(inbox, None, log)
            with open(inbox) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1000)
            self.assertEqual(json.loads(lines[0])["update_id"], 1501)

    def test_compaction_survives_wrong_shape_lines(self):
        # Valid JSON of the wrong shape (and a deeply nested
        # line) must be dropped by compaction, not crash it
        # forever (the old code called .get on a list).
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            log = os.path.join(d, "log.txt")
            self._write_inbox(inbox, range(1, 2100))
            with open(inbox, "a") as f:
                f.write("[]\n")
                f.write('"scalar"\n')
                f.write("[" * 20000 + "]" * 20000 + "\n")
                f.write('{"update_id": 2200}\n')
            poller.compact_inbox(inbox, 2000, log)  # must not raise
            with open(inbox) as f:
                remaining = [json.loads(line)["update_id"]
                             for line in f if line.strip()]
            self.assertEqual(remaining,
                             list(range(2001, 2100)) + [2200])

    def test_compaction_preserves_fresh_epoch_line(self):
        # A fresh lower-id epoch line must survive compaction
        # until scan handles it; once its id is in recent_ids
        # (already processed), compaction may drop it.
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            log = os.path.join(d, "log.txt")
            boundary = 1_000_000.0
            self._write_inbox(inbox, range(1, 2501))
            with open(inbox, "a") as f:
                f.write(json.dumps(
                    {"update_id": 3,
                     "received_at": boundary + 10}) + "\n")
            poller.compact_inbox(
                inbox, 2000, log, recent_ids=set(),
                epoch_boundary_at=boundary, now=boundary + 20)
            with open(inbox) as f:
                remaining = [json.loads(line)["update_id"]
                             for line in f if line.strip()]
            self.assertEqual(remaining.count(3), 1)
            self.assertEqual(
                [uid for uid in remaining if uid > 2000],
                list(range(2001, 2501)))

            self._write_inbox(inbox, range(1, 2501))
            with open(inbox, "a") as f:
                f.write(json.dumps(
                    {"update_id": 3,
                     "received_at": boundary + 10}) + "\n")
            poller.compact_inbox(
                inbox, 2000, log, recent_ids={3},
                epoch_boundary_at=boundary, now=boundary + 20)
            with open(inbox) as f:
                remaining = [json.loads(line)["update_id"]
                             for line in f if line.strip()]
            self.assertNotIn(3, remaining)


class TestPruneHandled(unittest.TestCase):
    def test_prunes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "handled.txt")
            log = os.path.join(d, "log.txt")
            with open(path, "w") as f:
                for i in range(2500):
                    f.write(f"1:{i}\n")
            poller.prune_handled(path, log)
            with open(path) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1000)
            self.assertEqual(lines[0].strip(), "1:1500")

    def test_small_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "handled.txt")
            log = os.path.join(d, "log.txt")
            with open(path, "w") as f:
                f.write("1:2\n")
            poller.prune_handled(path, log)
            with open(path) as f:
                self.assertEqual(f.read(), "1:2\n")

    def test_concurrent_append_survives_prune(self):
        # M5: an append racing the prune must not be lost.
        import threading
        import bridge_common as bc
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "handled.txt")
            log = os.path.join(d, "log.txt")
            with open(path, "w") as f:
                for i in range(2500):
                    f.write(f"1:{i}\n")
            results = {}

            def _prune():
                poller.prune_handled(path, log)

            def _append():
                results["added"] = bc.append_handled(path, "9:9")

            t1 = threading.Thread(target=_prune)
            t2 = threading.Thread(target=_append)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            with open(path) as f:
                keys = {line.strip() for line in f if line.strip()}
            self.assertIn("9:9", keys)
            self.assertTrue(results.get("added"))


class TestAppendInbox(unittest.TestCase):
    def test_appends_valid_lines(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            updates = [{"update_id": 1, "message": {"text": "a"}},
                       {"update_id": 2, "message": {"text": "b"}}]
            self.assertTrue(poller.append_inbox(inbox, updates))
            with open(inbox) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["update_id"], 1)
            self.assertEqual(json.loads(lines[1])["update_id"], 2)

    def test_append_stamps_received_at(self):
        # Every claimed update carries its arrival time, so the
        # scan can cut off old callbacks by received_at.
        import time
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            before = time.time()
            self.assertTrue(poller.append_inbox(
                inbox, [{"update_id": 1}]))
            with open(inbox) as f:
                stamped = json.loads(f.readline())
            self.assertGreaterEqual(stamped["received_at"], before)
            # The caller's dict is not mutated.
            upd = {"update_id": 2}
            poller.append_inbox(inbox, [upd])
            self.assertNotIn("received_at", upd)

    def test_repairs_missing_trailing_newline(self):
        # M3: a previous write that ended mid-line must not glue
        # the next update onto it.
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            with open(inbox, "w") as f:
                f.write('{"update_id": 1}')  # no trailing newline
            self.assertTrue(poller.append_inbox(
                inbox, [{"update_id": 2}]))
            with open(inbox) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["update_id"], 1)
            self.assertEqual(json.loads(lines[1])["update_id"], 2)

    def test_failed_write_leaves_no_partial_line(self):
        # M3: a write cut short mid-line (simulated ENOSPC) must
        # roll the file back to its old size — a partial JSON
        # line would corrupt the next scan and lose an update.
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            with open(inbox, "w") as f:
                f.write('{"update_id": 1}\n')
            with open(inbox, "rb") as f:
                before = f.read()

            class PartialBinaryWriter:
                """Binary file object that writes the first few
                bytes, then fails — a mid-line cut, exactly the
                ENOSPC case. append_inbox opens the inbox in
                binary mode (item A), so the mock is binary too."""
                _failed = False

                def __init__(self, raw):
                    self._raw = raw

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self._raw.close()
                    return False

                def seek(self, *args):
                    return self._raw.seek(*args)

                def tell(self):
                    return self._raw.tell()

                def read(self, *args):
                    return self._raw.read(*args)

                def flush(self):
                    return self._raw.flush()

                def fileno(self):
                    return self._raw.fileno()

                def write(self, data):
                    if not PartialBinaryWriter._failed:
                        PartialBinaryWriter._failed = True
                        self._raw.write(data[:5])
                        self._raw.flush()
                        raise OSError("No space left on device")
                    raise OSError("No space left on device")

            real_open = open

            def _failing_open(*args, **kwargs):
                return PartialBinaryWriter(real_open(*args, **kwargs))

            with mock.patch("builtins.open", _failing_open):
                self.assertFalse(poller.append_inbox(
                    inbox, [{"update_id": 2}]))
            with open(inbox, "rb") as f:
                self.assertEqual(f.read(), before)

    def test_unwritable_inbox_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            os.makedirs(inbox)  # a directory, not a file
            self.assertFalse(poller.append_inbox(
                inbox, [{"update_id": 1}]))

    def test_truncated_utf8_tail_does_not_break_append(self):
        # A: a previous write cut mid-way through a multi-byte
        # (Persian) character must not raise UnicodeDecodeError
        # and stall the poller forever. The tail byte is repaired
        # with a newline and the new update appends cleanly; the
        # truncated line itself stays invalid JSON and is skipped
        # by the scan — it must never block the valid lines
        # around it.
        import time
        owner = 12345

        def _msg(uid, text):
            return {"update_id": uid,
                    "message": {"message_id": uid,
                                "date": int(time.time()),
                                "from": {"id": owner, "is_bot": False},
                                "chat": {"id": owner, "type": "private"},
                                "text": text}}

        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            with open(inbox, "wb") as f:
                f.write((json.dumps(_msg(1, "سلام"), ensure_ascii=False)
                         + "\n").encode("utf-8"))
                # A write cut after the first byte of "م" (0xD9).
                f.write(b'{"update_id": 2, "message": {"text": "\xd9')
            self.assertTrue(poller.append_inbox(inbox, [_msg(3, "ok")]))
            import bridge_common
            import scan_inbox
            lines = bridge_common.read_inbox_lines(inbox)
            self.assertEqual(len(lines), 3)
            # The scan delivers the two valid lines and skips the
            # truncated tail — no raise, no stuck offset.
            seen = os.path.join(d, "seen.json")
            pending = os.path.join(d, "pending.json")
            handled = os.path.join(d, "handled.txt")
            open(handled, "w").close()
            payload, _warnings = scan_inbox.scan(
                inbox, seen, pending, handled, owner)
            self.assertEqual(
                [m["update_id"] for m in payload["new_messages"]],
                [1, 3])

    def test_truncated_utf8_tail_in_compaction(self):
        # A: compact_inbox must survive the same truncated tail.
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            log = os.path.join(d, "poller.log")
            with open(inbox, "wb") as f:
                for uid in range(1, 2100):
                    f.write((json.dumps({"update_id": uid}) + "\n")
                            .encode("utf-8"))
                f.write(b'{"update_id": 2100, "text": "\xd9')
            poller.compact_inbox(inbox, 1500, log)
            import bridge_common
            lines = bridge_common.read_inbox_lines(inbox)
            self.assertTrue(all(
                json.loads(bridge_common.decode_inbox_line(raw))
                .get("update_id", 0) > 1500 for raw in lines if raw))


class TestEpochResetOffset(unittest.TestCase):
    def test_fresh_batch_below_offset_resets(self):
        import time
        updates = [{"update_id": 3, "message": {
            "message_id": 3, "date": int(time.time())}}]
        self.assertEqual(poller.epoch_reset_offset(1000, updates), 4)

    def test_stale_batch_below_offset_does_not_reset(self):
        import time
        old = int(time.time()) - 48 * 3600
        updates = [{"update_id": 3, "message": {
            "message_id": 3, "date": old}}]
        self.assertIsNone(poller.epoch_reset_offset(1000, updates))

    def test_normal_batch_does_not_reset(self):
        updates = [{"update_id": 1001, "message": {"date": 1}}]
        self.assertIsNone(poller.epoch_reset_offset(1000, updates))
        self.assertIsNone(poller.epoch_reset_offset(1000, []))


class TestOffsetSilenceDrop(unittest.TestCase):
    def test_drop_only_after_seven_days_without_updates(self):
        now = 1_000_000_000.0
        week = 7 * 24 * 3600
        self.assertTrue(poller.should_drop_offset_for_silence(
            1000, now - week - 1, now=now))
        self.assertFalse(poller.should_drop_offset_for_silence(
            1000, now - week + 60, now=now))
        self.assertFalse(poller.should_drop_offset_for_silence(
            0, now - week - 1, now=now))
        self.assertFalse(poller.should_drop_offset_for_silence(
            1000, now - week - 1, now=now, already_dropped=True))
        self.assertFalse(poller.should_drop_offset_for_silence(
            1000, None, now=now))


class TestSignalTarget(unittest.TestCase):
    def test_group_payload_targets_group_chat(self):
        payload = {"kind": "message", "message_id": 7,
                   "chat_id": -100123}
        self.assertEqual(poller.signal_target(payload, 111),
                         (-100123, 7))

    def test_private_payload_targets_owner(self):
        payload = {"kind": "message", "message_id": 7,
                   "chat_id": 111}
        self.assertEqual(poller.signal_target(payload, 111), (111, 7))

    def test_callback_and_missing_id_have_no_target(self):
        self.assertIsNone(poller.signal_target(
            {"kind": "callback", "message_id": 7}, 111))
        self.assertIsNone(poller.signal_target(
            {"kind": "message", "message_id": 0}, 111))


class TestHeartbeatDeadline(unittest.TestCase):
    def test_heartbeat_stops_past_deadline(self):
        # D: the heartbeat thread must stop refreshing beat_at
        # once the main loop's progress deadline has passed — a
        # stuck main loop must go stale, not look alive forever.
        import threading
        import time
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "poller-state.json")
            start = time.time()
            info = {"started_at": start, "beat_at": start,
                    "last_ok_at": None, "pid": 123, "cycles": 0,
                    "errors": 0,
                    "progress_deadline": start + 0.3}
            poller._stop.clear()
            thread = threading.Thread(
                target=poller.heartbeat_loop,
                args=(state, info, 0.1), daemon=True)
            try:
                thread.start()
                time.sleep(0.6)  # past the deadline
                beat_after = info["beat_at"]
                # beat_at froze at (or just past) the deadline.
                self.assertLess(beat_after - start, 1.0)
                time.sleep(0.3)
                # Still frozen: no further beats after the deadline.
                self.assertEqual(info["beat_at"], beat_after)
            finally:
                poller._stop.set()
                thread.join(timeout=2)
                poller._stop.clear()

    def test_heartbeat_advances_before_deadline(self):
        # D: before the deadline the thread beats normally.
        import threading
        import time
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "poller-state.json")
            start = time.time()
            info = {"started_at": start, "beat_at": start,
                    "last_ok_at": None, "pid": 123, "cycles": 0,
                    "errors": 0,
                    "progress_deadline": start + 30}
            poller._stop.clear()
            thread = threading.Thread(
                target=poller.heartbeat_loop,
                args=(state, info, 0.1), daemon=True)
            try:
                thread.start()
                time.sleep(0.5)
                self.assertGreater(info["beat_at"], start)
            finally:
                poller._stop.set()
                thread.join(timeout=2)
                poller._stop.clear()


if __name__ == "__main__":
    unittest.main()
