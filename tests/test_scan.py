"""Tests for poller/scan_inbox.py: seen/pending markers, retry
after a failed wake, fail-closed corruption handling, and the
shared classification (the review's items #1, #4, #6, #7)."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "poller"))
import scan_inbox  # noqa: E402

OWNER = 111111111
STRANGER = 777000


def _msg(update_id, message_id=None, **overrides):
    # Fresh dates by default: a first scan with a missing seen
    # marker only delivers messages younger than 24h (H2), so
    # test messages must look new unless a test says otherwise.
    msg = {"message_id": message_id or update_id, "date": int(time.time()),
           "from": {"id": OWNER, "is_bot": False, "first_name": "Owner"},
           "chat": {"id": OWNER, "type": "private"},
           "text": "hello %d" % update_id}
    msg.update(overrides)
    return {"update_id": update_id, "message": msg}


def _stranger_msg(update_id):
    msg = {"message_id": update_id, "date": int(time.time()),
           "from": {"id": STRANGER, "is_bot": False},
           "chat": {"id": STRANGER, "type": "private"},
           "text": "spam"}
    return {"update_id": update_id, "message": msg}


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.inbox = os.path.join(d, "inbox.jsonl")
        self.seen = os.path.join(d, "seen.json")
        self.pending = os.path.join(d, "pending.json")
        self.handled = os.path.join(d, "handled.txt")
        open(self.inbox, "w").close()
        open(self.handled, "w").close()

    def tearDown(self):
        self.tmp.cleanup()

    def _append(self, *updates):
        with open(self.inbox, "a") as f:
            for upd in updates:
                f.write(json.dumps(upd, ensure_ascii=False) + "\n")

    def _scan(self, **kwargs):
        kwargs.setdefault("retry_after", 600)
        payload, _warnings = scan_inbox.scan(
            self.inbox, self.seen, self.pending, self.handled, OWNER,
            **kwargs)
        return payload

    def _seen_value(self):
        with open(self.seen) as f:
            return json.load(f)["last_update_id"]


class TestBasicScan(ScanTestCase):
    def test_empty_inbox(self):
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["seen"], 0)

    def test_new_message_woken(self):
        self._append(_msg(1), _msg(2))
        payload = self._scan()
        self.assertEqual(len(payload["new_messages"]), 2)
        self.assertEqual(payload["new_messages"][0]["text"], "hello 1")
        self.assertEqual(self._seen_value(), 2)

    def test_second_scan_silent(self):
        self._append(_msg(1))
        self._scan()
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])

    def test_stranger_advances_seen_without_wake(self):
        self._append(_stranger_msg(1), _msg(2))
        payload = self._scan()
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertEqual(payload["new_messages"][0]["update_id"], 2)
        self.assertEqual(self._seen_value(), 2)

    def test_unsupported_type_woken(self):
        # #6: a sticker must wake the agent (so it can reply
        # "can't handle that yet") instead of being ignored while
        # the poller already signalled it.
        upd = _msg(1, text=None)
        upd["message"].pop("text")
        upd["message"]["sticker"] = {"file_id": "s1"}
        self._append(upd)
        payload = self._scan()
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertEqual(payload["new_messages"][0]["kind"], "unsupported")
        self.assertEqual(payload["new_messages"][0]["unsupported_type"],
                         "sticker")

    def test_group_message_ignored(self):
        upd = _msg(1)
        upd["message"]["chat"] = {"id": -100123, "type": "group"}
        self._append(upd)
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])

    def test_callback_woken(self):
        upd = {"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": OWNER},
            "message": {"message_id": 9, "date": int(time.time())},
            "data": "cmd:status"}}
        self._append(upd)
        payload = self._scan()
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertEqual(payload["new_messages"][0]["kind"], "callback")

    def test_dry_run_writes_nothing(self):
        self._append(_msg(1))
        payload = self._scan(dry_run=True)
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertFalse(os.path.exists(self.seen))
        self.assertFalse(os.path.exists(self.pending))


class TestPendingAndRetry(ScanTestCase):
    def test_pending_survives_until_handled(self):
        # #1: a woken message stays pending until the agent
        # confirms it in handled.txt.
        self._append(_msg(1))
        payload = self._scan()
        self.assertEqual(payload["pending"], 1)
        # Not re-woken while the retry window is open.
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["pending"], 1)
        # Mark handled: pending clears.
        with open(self.handled, "a") as f:
            f.write(f"{OWNER}:1\n")
        payload = self._scan()
        self.assertEqual(payload["pending"], 0)

    def test_failed_wake_retried_after_window(self):
        # #1: if the wake failed (or the turn was dropped), the
        # message is re-woken once the retry window passes.
        self._append(_msg(1))
        self._scan(retry_after=600)
        # Age the pending entry past the window.
        with open(self.pending) as f:
            data = json.load(f)
        data["entries"]["1"]["woken_at"] = time.time() - 601
        with open(self.pending, "w") as f:
            json.dump(data, f)
        payload = self._scan(retry_after=600)
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertEqual(payload["new_messages"][0]["update_id"], 1)
        self.assertTrue(any("re-waking" in w
                            for w in payload["warnings"]))

    def test_pending_survives_inbox_compaction(self):
        # The pending payload is stored in pending.json, so a
        # compacted inbox does not lose the retry.
        self._append(_msg(1))
        self._scan(retry_after=600)
        open(self.inbox, "w").close()  # compacted away
        with open(self.pending) as f:
            data = json.load(f)
        data["entries"]["1"]["woken_at"] = time.time() - 601
        with open(self.pending, "w") as f:
            json.dump(data, f)
        payload = self._scan(retry_after=600)
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertEqual(payload["new_messages"][0]["text"], "hello 1")


class TestCorruption(ScanTestCase):
    def test_corrupt_seen_fails_closed(self):
        # #4: a corrupt seen file must not replay history.
        self._append(_msg(1), _msg(2))
        with open(self.seen, "w") as f:
            f.write("{corrupt")
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["seen"], 2)
        self.assertTrue(payload["warnings"])
        # A genuinely new message still arrives.
        self._append(_msg(3))
        payload = self._scan()
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertEqual(payload["new_messages"][0]["update_id"], 3)

    def test_non_object_seen_fails_closed(self):
        # #4: valid JSON that is not an object must not crash.
        self._append(_msg(1))
        with open(self.seen, "w") as f:
            f.write("[]")
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["seen"], 1)

    def test_corrupt_inbox_lines_skipped(self):
        self._append(_msg(1))
        with open(self.inbox, "a") as f:
            f.write("this is not json\n")
            f.write('{"update_id": "not-an-int"}\n')
        self._append(_msg(2))
        payload = self._scan()
        self.assertEqual([m["update_id"] for m in payload["new_messages"]],
                         [1, 2])

    def test_corrupt_pending_falls_back_to_bak(self):
        self._append(_msg(1))
        self._scan()
        # A second scan writes pending.json.bak from the first copy.
        self._append(_msg(2))
        self._scan()
        with open(self.pending, "w") as f:
            f.write("{corrupt")
        payload = self._scan()
        # The .bak copy still holds update 1 as pending, so the
        # retry record survives; nothing new is due.
        self.assertEqual(payload["pending"], 1)
        self.assertEqual(payload["new_messages"], [])

    def test_corrupt_seen_falls_back_to_bak(self):
        # A corrupt seen file falls back to seen.json.bak (the
        # latest known-good marker), not to the inbox max — so
        # the marker never moves backwards when the inbox lost
        # a line.
        self._append(_msg(1), _msg(2))
        self._scan()
        self.assertEqual(self._seen_value(), 2)
        # Drop the last inbox line (as if it were corrupted away).
        with open(self.inbox, "w") as f:
            f.write(json.dumps(_msg(1)) + "\n")
        with open(self.seen, "w") as f:
            f.write("{corrupt")
        payload = self._scan()
        self.assertEqual(payload["seen"], 2)
        self.assertEqual(payload["new_messages"], [])


class TestRetryCap(ScanTestCase):
    def _age_pending(self, seconds):
        with open(self.pending) as f:
            data = json.load(f)
        for entry in data["entries"].values():
            entry["woken_at"] = time.time() - seconds
        with open(self.pending, "w") as f:
            json.dump(data, f)

    def test_redelivery_carries_attempt_fields(self):
        # H1: a re-wake is marked as a redelivery with its number.
        self._append(_msg(1))
        self._scan(retry_after=60)
        self._age_pending(61)
        payload = self._scan(retry_after=60)
        self.assertEqual(len(payload["new_messages"]), 1)
        msg = payload["new_messages"][0]
        self.assertTrue(msg.get("redelivery"))
        self.assertEqual(msg.get("attempt"), 2)

    def test_retry_backoff_grows(self):
        # H1: the wait doubles after each attempt (60s, 120s, ...).
        self._append(_msg(1))
        self._scan(retry_after=60)
        self._age_pending(61)
        self._scan(retry_after=60)  # attempt 2, next wait 120s
        self._age_pending(61)       # only 61s passed: too soon
        payload = self._scan(retry_after=60)
        self.assertEqual(payload["new_messages"], [])
        self._age_pending(121)      # 121s passed: due
        payload = self._scan(retry_after=60)
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertEqual(payload["new_messages"][0]["attempt"], 3)

    def test_retry_cap_drops_message(self):
        # H1: after MAX_RETRY_ATTEMPTS the message is dropped
        # and reported, never woken again. The dropped entry
        # stays pending and its notice is redelivered until the
        # drop key is handled (v5: a failed wake must not lose
        # the notice itself).
        import bridge_common as bc
        self._append(_msg(1))
        self._scan(retry_after=1)
        payload = None
        for _ in range(bc.MAX_RETRY_ATTEMPTS):
            self._age_pending(3601)
            payload = self._scan(retry_after=1)
        # The last iteration is the one that drops it.
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["pending"], 1)
        self.assertEqual(len(payload["dropped"]), 1)
        self.assertEqual(payload["dropped"][0]["update_id"], 1)
        self.assertIn("retry cap", payload["dropped"][0]["reason"])
        self.assertEqual(payload["dropped"][0]["drop_key"],
                         f"{OWNER}:drop:1")
        # An immediate rescan does not duplicate the notice;
        # after the retry interval it returns (the wake that
        # carried it may have failed)...
        payload = self._scan(retry_after=1)
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["dropped"], [])
        with open(self.pending) as f:
            data = json.load(f)
        data["entries"]["1"]["dropped_notified_at"] = \
            time.time() - 2
        with open(self.pending, "w") as f:
            json.dump(data, f)
        payload = self._scan(retry_after=1)
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(len(payload["dropped"]), 1)
        # ...until the agent confirms the owner was told.
        with open(self.handled, "a") as f:
            f.write(f"{OWNER}:drop:1\n")
        payload = self._scan(retry_after=1)
        self.assertEqual(payload["pending"], 0)
        self.assertEqual(payload["dropped"], [])

    def test_pending_ttl_drops_message(self):
        # H1: a message pending past the TTL is dropped even
        # with attempts left.
        self._append(_msg(1))
        self._scan(retry_after=600)
        with open(self.pending) as f:
            data = json.load(f)
        entry = data["entries"]["1"]
        entry["first_woken_at"] = time.time() - 25 * 3600
        entry["woken_at"] = time.time() - 601
        with open(self.pending, "w") as f:
            json.dump(data, f)
        payload = self._scan(retry_after=600)
        self.assertEqual(payload["new_messages"], [])
        # Dropped, but kept pending until the drop key is handled.
        self.assertEqual(payload["pending"], 1)
        self.assertEqual(payload["dropped"][0]["reason"],
                         "pending TTL expired")


class TestMissingSeen(ScanTestCase):
    def test_missing_seen_skips_old_inbox(self):
        # H2: a lost seen marker must not replay the whole inbox.
        old = int(time.time()) - 48 * 3600
        for uid in range(1, 6):
            self._append(_msg(uid, date=old))
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["seen"], 5)
        self.assertTrue(any("missing" in w for w in payload["warnings"]))

    def test_missing_seen_delivers_fresh_messages(self):
        # H2: on a first run the fresh messages still arrive.
        old = int(time.time()) - 48 * 3600
        self._append(_msg(1, date=old), _msg(2, date=old))
        self._append(_msg(3), _msg(4))  # fresh
        payload = self._scan()
        self.assertEqual([m["update_id"] for m in payload["new_messages"]],
                         [3, 4])
        self.assertEqual(payload["seen"], 4)

    def test_per_wake_cap(self):
        # H2: at most MAX_MESSAGES_PER_WAKE per scan; the rest
        # arrive on the next scan (the marker does not skip them).
        import bridge_common as bc
        for uid in range(1, 26):
            self._append(_msg(uid))
        payload = self._scan()
        self.assertEqual(len(payload["new_messages"]),
                         bc.MAX_MESSAGES_PER_WAKE)
        self.assertEqual(payload["seen"], bc.MAX_MESSAGES_PER_WAKE)
        payload = self._scan()
        self.assertEqual([m["update_id"] for m in payload["new_messages"]],
                         list(range(21, 26)))
        self.assertEqual(payload["seen"], 25)

    def test_partial_last_line_skipped(self):
        # A truncated last line (a cut-off write) must not crash
        # the scan or lose the valid lines around it.
        self._append(_msg(1), _msg(2))
        with open(self.inbox, "a") as f:
            f.write('{"update_id": 3, "message": {"text": "cut')
        payload = self._scan()
        self.assertEqual([m["update_id"] for m in payload["new_messages"]],
                         [1, 2])


class TestHandledSkip(ScanTestCase):
    def test_missing_seen_with_full_handled_wakes_nothing(self):
        # B (v2->v3 upgrade): the seen marker was lost, but every
        # message was already answered. None may re-wake — the
        # marker still advances past them.
        for uid in range(1, 4):
            self._append(_msg(uid))
        with open(self.handled, "w") as f:
            for uid in range(1, 4):
                f.write(f"{OWNER}:{uid}\n")
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["seen"], 3)

    def test_missing_seen_handled_callback_not_rewoken(self):
        # B: the same for a button tap — its handled key is
        # chat:cb:<callback_query_id>, not the message id.
        cb = {"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": OWNER},
            "message": {"message_id": 9, "date": int(time.time())},
            "data": "cmd:status"}}
        self._append(cb)
        with open(self.handled, "w") as f:
            f.write(f"{OWNER}:cb:cb1\n")
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["seen"], 1)

    def test_unhandled_message_still_wakes_after_marker_loss(self):
        # B: the skip must not swallow a message that was never
        # answered — only handled ones are skipped.
        for uid in range(1, 4):
            self._append(_msg(uid))
        with open(self.handled, "w") as f:
            f.write(f"{OWNER}:1\n{OWNER}:2\n")
        payload = self._scan()
        self.assertEqual([m["update_id"] for m in payload["new_messages"]],
                         [3])


class TestDroppedEntries(ScanTestCase):
    def _age_pending(self, seconds):
        with open(self.pending) as f:
            data = json.load(f)
        for entry in data["entries"].values():
            entry["woken_at"] = time.time() - seconds
        with open(self.pending, "w") as f:
            json.dump(data, f)

    def test_drop_only_after_backoff_elapsed(self):
        # C: a message at the retry cap must NOT be dropped
        # minutes after its latest wake while the agent may still
        # be working on it — only once the backoff has elapsed.
        self._append(_msg(1))
        self._scan(retry_after=600)
        with open(self.pending) as f:
            data = json.load(f)
        entry = data["entries"]["1"]
        entry["attempts"] = 4  # MAX_RETRY_ATTEMPTS
        entry["woken_at"] = time.time() - 30  # just woken, agent busy
        entry["first_woken_at"] = time.time() - 30
        with open(self.pending, "w") as f:
            json.dump(data, f)
        payload = self._scan(retry_after=600)
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["dropped"], [])
        self.assertEqual(payload["pending"], 1)
        # Now the backoff (600 * 2**3 = 4800s, capped at 3600s)
        # has elapsed: the drop happens.
        self._age_pending(3601)
        payload = self._scan(retry_after=600)
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["pending"], 1)
        self.assertEqual(len(payload["dropped"]), 1)
        self.assertEqual(payload["dropped"][0]["reason"],
                         "retry cap reached")

    def test_dropped_entry_describes_the_message(self):
        # C: a dropped entry must carry message_id, date, and a
        # text excerpt, so the agent can tell the owner what was
        # lost instead of only an update id.
        self._append(_msg(1))
        self._scan(retry_after=1)
        for _ in range(4):
            self._age_pending(3601)
            payload = self._scan(retry_after=1)
        self.assertEqual(len(payload["dropped"]), 1)
        dropped = payload["dropped"][0]
        self.assertEqual(dropped["update_id"], 1)
        self.assertEqual(dropped["message_id"], 1)
        self.assertIn("date", dropped)
        self.assertEqual(dropped["text"], "hello 1")


class TestCallbackKeys(ScanTestCase):
    def test_two_callbacks_same_message_distinct_keys(self):
        # L11: two taps on one message must not share a handled key.
        def _cb(uid, cb_id):
            return {"update_id": uid, "callback_query": {
                "id": cb_id, "from": {"id": OWNER},
                "message": {"message_id": 9, "date": int(time.time())},
                "data": "cmd:status"}}
        self._append(_cb(1, "cb1"), _cb(2, "cb2"))
        payload = self._scan()
        self.assertEqual(payload["pending"], 2)
        # Handle only the first tap.
        with open(self.handled, "a") as f:
            f.write(f"{OWNER}:cb:cb1\n")
        payload = self._scan()
        self.assertEqual(payload["pending"], 1)
        # Handle the second tap: pending clears.
        with open(self.handled, "a") as f:
            f.write(f"{OWNER}:cb:cb2\n")
        payload = self._scan()
        self.assertEqual(payload["pending"], 0)


def _callback(update_id, cb_id="cb1", button_date=None,
              chat=None, received_at=None):
    upd = {"update_id": update_id, "callback_query": {
        "id": cb_id, "from": {"id": OWNER},
        "message": {"message_id": 9,
                    "date": button_date or int(time.time()),
                    "chat": chat or {"id": OWNER, "type": "private"}},
        "data": "cmd:status"}}
    if received_at is not None:
        upd["received_at"] = received_at
    return upd


class TestReceivedAtCutoff(ScanTestCase):
    def test_old_callback_cut_off_by_received_at(self):
        # B: a tap that ARRIVED 48h ago is cut off on marker
        # loss even though its button message is fresh — the
        # received_at stamp decides, not the button date.
        old = time.time() - 48 * 3600
        self._append(_callback(1, received_at=old))
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["seen"], 1)

    def test_fresh_tap_on_old_button_delivered(self):
        # B: a tap that arrived just now is delivered even
        # though the button message is old.
        old = int(time.time()) - 48 * 3600
        self._append(_callback(1, button_date=old,
                               received_at=time.time()))
        payload = self._scan()
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertEqual(payload["new_messages"][0]["kind"], "callback")

    def test_legacy_callback_without_stamp_uses_button_date(self):
        # Migration: a pre-v5 line has no received_at. An old
        # button date means the tap is cut off (no exemption);
        # a fresh button date still delivers.
        old = int(time.time()) - 48 * 3600
        self._append(_callback(1, cb_id="old", button_date=old))
        self._append(_callback(2, cb_id="fresh"))
        payload = self._scan()
        self.assertEqual(
            [m["callback_query_id"] for m in payload["new_messages"]],
            ["fresh"])
        self.assertEqual(payload["seen"], 2)

    def test_realistic_v3_callback_handled_key(self):
        # A v3-era callback (chat present, handled by its
        # chat:cb:<id> key) never re-wakes on marker loss.
        self._append(_callback(1, cb_id="987654321"))
        with open(self.handled, "w") as f:
            f.write(f"{OWNER}:cb:987654321\n")
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["seen"], 1)


class TestDroppedRedelivery(ScanTestCase):
    def test_dropped_notice_survives_failed_wake(self):
        # End to end: the scan drops a message; an immediate
        # rescan must NOT duplicate the notice, but after the
        # retry interval the notice returns if the wake failed
        # (the agent never marked the drop key).
        self._append(_msg(1))
        self._scan(retry_after=1)
        with open(self.pending) as f:
            data = json.load(f)
        entry = data["entries"]["1"]
        entry["attempts"] = 4
        entry["woken_at"] = time.time() - 4000
        entry["first_woken_at"] = time.time() - 4000
        with open(self.pending, "w") as f:
            json.dump(data, f)
        payload = self._scan(retry_after=1)
        self.assertEqual(len(payload["dropped"]), 1)
        with open(self.pending) as f:
            entry = json.load(f)["entries"]["1"]
        self.assertIn("dropped_notified_at", entry)
        self.assertEqual(entry["retry_after"], 1)
        # Immediate rescan: no duplicate dropped notice.
        payload = self._scan(retry_after=1)
        self.assertEqual(payload["dropped"], [])
        self.assertEqual(payload["pending"], 1)
        # After retry_after, the failed wake's notice returns.
        with open(self.pending) as f:
            data = json.load(f)
        data["entries"]["1"]["dropped_notified_at"] = \
            time.time() - 2
        with open(self.pending, "w") as f:
            json.dump(data, f)
        payload = self._scan(retry_after=1)
        self.assertEqual(len(payload["dropped"]), 1)
        self.assertEqual(payload["dropped"][0]["drop_key"],
                         f"{OWNER}:drop:1")
        # Agent tells the owner and confirms: notice stops.
        with open(self.handled, "a") as f:
            f.write(f"{OWNER}:drop:1\n")
        payload = self._scan(retry_after=1)
        self.assertEqual(payload["dropped"], [])
        self.assertEqual(payload["pending"], 0)


class TestMalformedPending(ScanTestCase):
    def test_string_payload_does_not_crash_scan(self):
        # Valid JSON, wrong shape: one bad pending entry must
        # be skipped with a warning, not crash every scan.
        with open(self.pending, "w") as f:
            json.dump({"entries": {
                "1": {"payload": "bad", "attempts": 1,
                      "woken_at": time.time() - 4000,
                      "first_woken_at": time.time() - 4000},
                "2": "not-an-entry",
            }}, f)
        self._append(_msg(3))
        payload = self._scan(retry_after=1)
        self.assertEqual(
            [m["update_id"] for m in payload["new_messages"]], [3])
        self.assertEqual(payload["pending"], 1)
        self.assertTrue(any(
            "malformed pending entry" in warning
            for warning in payload["warnings"]))
        # The bad entries were removed, so the next scan is clean.
        payload = self._scan(retry_after=1)
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["pending"], 1)

    def test_bad_numeric_fields_do_not_crash_scan(self):
        self._append(_msg(1))
        self._scan(retry_after=1)
        with open(self.pending) as f:
            data = json.load(f)
        entry = data["entries"]["1"]
        entry["attempts"] = "many"
        entry["woken_at"] = []
        entry["first_woken_at"] = {}
        with open(self.pending, "w") as f:
            json.dump(data, f)
        payload = self._scan(retry_after=1)  # must not raise
        self.assertEqual(payload["pending"], 1)
        with open(self.pending) as f:
            entry = json.load(f)["entries"]["1"]
        self.assertIsInstance(entry["attempts"], int)
        self.assertIsInstance(entry["woken_at"], float)


class TestAck(ScanTestCase):
    def _age_pending(self, seconds):
        with open(self.pending) as f:
            data = json.load(f)
        for entry in data["entries"].values():
            entry["woken_at"] = time.time() - seconds
        with open(self.pending, "w") as f:
            json.dump(data, f)

    def _ack(self, key, when):
        with open(self.handled + ".ack", "w") as f:
            f.write(f"{key} {when}\n")

    def test_fresh_ack_postpones_redelivery(self):
        # Long-running work: the retry window passed, but a
        # fresh ack means the agent is still on it — no
        # duplicate wake.
        self._append(_msg(1))
        self._scan(retry_after=60)
        self._age_pending(3600)  # retry long due
        self._ack(f"{OWNER}:1", time.time())  # acked just now
        payload = self._scan(retry_after=60)
        self.assertEqual(payload["new_messages"], [])
        self.assertEqual(payload["pending"], 1)

    def test_expired_ack_lets_redelivery_proceed(self):
        import bridge_common as bc
        self._append(_msg(1))
        self._scan(retry_after=60)
        self._age_pending(3600)
        self._ack(f"{OWNER}:1",
                  time.time() - bc.ACK_TTL_SECS - 60)
        payload = self._scan(retry_after=60)
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertTrue(payload["new_messages"][0].get("redelivery"))

    def test_stale_ack_from_previous_attempt_ignored(self):
        # An ack older than the latest wake belongs to an
        # earlier attempt and must not postpone this one.
        self._append(_msg(1))
        self._scan(retry_after=60)
        self._age_pending(61)
        self._scan(retry_after=60)  # attempt 2, woken_at = now
        self._ack(f"{OWNER}:1", time.time() - 3600)  # pre-attempt-2
        self._age_pending(121)
        payload = self._scan(retry_after=60)
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertEqual(payload["new_messages"][0]["attempt"], 3)


class TestGroupScan(ScanTestCase):
    def test_group_message_wakes_with_flag(self):
        upd = _msg(1)
        upd["message"]["chat"] = {"id": -100123, "type": "group"}
        self._append(upd)
        payload = self._scan(allow_groups=True)
        self.assertEqual(len(payload["new_messages"]), 1)
        self.assertEqual(payload["new_messages"][0]["chat_id"],
                         -100123)
        # Handled key uses the group chat, not the owner chat.
        with open(self.handled, "a") as f:
            f.write("-100123:1\n")
        payload = self._scan(allow_groups=True)
        self.assertEqual(payload["pending"], 0)

    def test_group_callback_needs_flag(self):
        self._append(_callback(
            1, chat={"id": -100123, "type": "supergroup"}))
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])
        payload = self._scan(allow_groups=True)
        self.assertEqual(payload["new_messages"], [])  # seen advanced


class TestMalformedShapes(ScanTestCase):
    def test_wrong_shape_and_deep_lines_skipped(self):
        # Valid JSON of the wrong shape and a deeply nested
        # line must be skipped, with the valid updates around
        # them delivered and the scan never raising.
        self._append(_msg(1))
        with open(self.inbox, "a") as f:
            f.write("[]\n")
            f.write('"just a string"\n')
            f.write("12345\n")
            f.write('{"update_id": 50, "message": []}\n')
            f.write("[" * 20000 + "]" * 20000 + "\n")
            f.write('{"update_id": true, "message": {}}\n')
        self._append(_msg(2))
        payload = self._scan()
        self.assertEqual(
            [m["update_id"] for m in payload["new_messages"]], [1, 2])
        self.assertTrue(any("malformed" in w
                            for w in payload["warnings"]))


class TestEpochReset(ScanTestCase):
    def _write_seen(self, value, boundary, recent=None):
        with open(self.seen, "w") as f:
            json.dump({"last_update_id": value,
                       "recent_update_ids": recent or [],
                       "epoch_boundary_at": boundary}, f)

    def test_fresh_lower_id_delivered_once(self):
        # Telegram reused lower ids after a long silence: the
        # fresh update below the marker is delivered, not
        # ignored forever, and not delivered twice.
        boundary = time.time() - 3600
        self._write_seen(100, boundary)
        upd = _msg(5)
        upd["received_at"] = time.time()
        self._append(upd)
        payload = self._scan()
        self.assertEqual(
            [m["update_id"] for m in payload["new_messages"]], [5])
        self.assertEqual(payload["seen"], 100)
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])

    def test_old_lower_id_stays_skipped(self):
        boundary = time.time() - 3600
        self._write_seen(100, boundary)
        upd = _msg(5)
        upd["received_at"] = boundary - 60  # arrived pre-boundary
        self._append(upd)
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])

    def test_lower_id_without_stamp_stays_skipped(self):
        # No received_at: no proof it is new — fail closed.
        self._write_seen(100, time.time() - 3600)
        self._append(_msg(5))
        payload = self._scan()
        self.assertEqual(payload["new_messages"], [])


class TestV3MigrationFixtures(ScanTestCase):
    def test_v3_pending_callback_without_chat_id_clears(self):
        # A pre-v5 pending payload has no chat_id/received_at;
        # its chat:cb:<id> handled key must still clear it.
        pending = {"entries": {"7": {
            "payload": {"kind": "callback", "update_id": 7,
                        "callback_query_id": "cb7",
                        "data": "cmd:help", "message_id": 3,
                        "date": int(time.time())},
            "woken_at": time.time(), "first_woken_at": time.time(),
            "attempts": 1}}}
        with open(self.pending, "w") as f:
            json.dump(pending, f)
        with open(self.seen, "w") as f:
            json.dump({"last_update_id": 7}, f)  # legacy shape
        with open(self.handled, "w") as f:
            f.write(f"{OWNER}:cb:cb7\n")
        payload = self._scan()
        self.assertEqual(payload["pending"], 0)
        self.assertEqual(payload["new_messages"], [])


if __name__ == "__main__":
    unittest.main()
