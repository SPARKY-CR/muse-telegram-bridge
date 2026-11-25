"""Tests for poller/bridge_common.py: classify, offsets, safe
paths, redaction, text splitting, and the singleton lock."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "poller"))
import bridge_common as bc  # noqa: E402

OWNER = 111111111
STRANGER = 777000


def _msg(update_id, **overrides):
    msg = {"message_id": 10, "date": 1700000000,
           "from": {"id": OWNER, "is_bot": False, "first_name": "Owner"},
           "chat": {"id": OWNER, "type": "private"},
           "text": "hello"}
    msg.update(overrides)
    return {"update_id": update_id, "message": msg}


class TestClassify(unittest.TestCase):
    def test_text_message(self):
        kind, payload = bc.classify(_msg(1), OWNER)
        self.assertEqual(kind, "message")
        self.assertEqual(payload["text"], "hello")
        self.assertEqual(payload["update_id"], 1)

    def test_photo_picks_largest(self):
        upd = _msg(2, text=None)
        upd["message"].pop("text")
        upd["message"]["photo"] = [
            {"file_id": "small", "width": 90},
            {"file_id": "large", "width": 1280}]
        upd["message"]["caption"] = "look"
        kind, payload = bc.classify(upd, OWNER)
        self.assertEqual(kind, "photo")
        self.assertEqual(payload["file_id"], "large")
        self.assertEqual(payload["caption"], "look")

    def test_document(self):
        upd = _msg(3, text=None)
        upd["message"].pop("text")
        upd["message"]["document"] = {
            "file_id": "doc1", "file_name": "notes.pdf",
            "mime_type": "application/pdf", "file_size": 1234}
        kind, payload = bc.classify(upd, OWNER)
        self.assertEqual(kind, "document")
        self.assertEqual(payload["file_name"], "notes.pdf")

    def test_voice(self):
        upd = _msg(4, text=None)
        upd["message"].pop("text")
        upd["message"]["voice"] = {"file_id": "v1", "duration": 5}
        kind, payload = bc.classify(upd, OWNER)
        self.assertEqual(kind, "voice")
        self.assertEqual(payload["duration"], 5)

    def test_callback(self):
        upd = {"update_id": 5, "callback_query": {
            "id": "cb1", "from": {"id": OWNER},
            "message": {"message_id": 9, "date": 1},
            "data": "cmd:status"}}
        kind, payload = bc.classify(upd, OWNER)
        self.assertEqual(kind, "callback")
        self.assertEqual(payload["data"], "cmd:status")
        self.assertEqual(payload["callback_query_id"], "cb1")

    def test_unsupported_sticker(self):
        upd = _msg(6, text=None)
        upd["message"].pop("text")
        upd["message"]["sticker"] = {"file_id": "s1"}
        kind, payload = bc.classify(upd, OWNER)
        self.assertEqual(kind, "unsupported")
        self.assertEqual(payload["unsupported_type"], "sticker")

    def test_unsupported_video(self):
        upd = _msg(7, text=None)
        upd["message"].pop("text")
        upd["message"]["video"] = {"file_id": "vid1"}
        kind, payload = bc.classify(upd, OWNER)
        self.assertEqual(kind, "unsupported")
        self.assertEqual(payload["unsupported_type"], "video")

    def test_stranger_ignored(self):
        upd = _msg(8)
        upd["message"]["from"] = {"id": STRANGER}
        kind, payload = bc.classify(upd, OWNER)
        self.assertIsNone(kind)
        self.assertIsNone(payload)

    def test_stranger_callback_ignored(self):
        upd = {"update_id": 9, "callback_query": {
            "id": "cb2", "from": {"id": STRANGER},
            "message": {"message_id": 9, "date": 1},
            "data": "cmd:status"}}
        kind, _ = bc.classify(upd, OWNER)
        self.assertIsNone(kind)

    def test_group_rejected_by_default(self):
        # #7: a group member must not pass by sharing the chat id.
        upd = _msg(10)
        upd["message"]["chat"] = {"id": -100123, "type": "group"}
        kind, _ = bc.classify(upd, OWNER)
        self.assertIsNone(kind)

    def test_group_allowed_with_flag(self):
        upd = _msg(11)
        upd["message"]["chat"] = {"id": -100123, "type": "group"}
        kind, _ = bc.classify(upd, OWNER, allow_groups=True)
        self.assertEqual(kind, "message")

    def test_group_stranger_rejected_even_with_flag(self):
        upd = _msg(12)
        upd["message"]["from"] = {"id": STRANGER}
        upd["message"]["chat"] = {"id": -100123, "type": "group"}
        kind, _ = bc.classify(upd, OWNER, allow_groups=True)
        self.assertIsNone(kind)

    def test_group_payload_carries_chat_id(self):
        upd = _msg(14)
        upd["message"]["chat"] = {"id": -100123, "type": "supergroup"}
        kind, payload = bc.classify(upd, OWNER, allow_groups=True)
        self.assertEqual(kind, "message")
        self.assertEqual(payload["chat_id"], -100123)
        self.assertEqual(payload["chat_type"], "supergroup")

    def test_group_callback_checks_chat_type(self):
        # Callbacks must honour allow_groups too: a tap in a
        # group is rejected by default and accepted (with the
        # group chat id) only with the flag.
        upd = {"update_id": 15, "callback_query": {
            "id": "cb9", "from": {"id": OWNER},
            "message": {"message_id": 9, "date": 1,
                        "chat": {"id": -100123, "type": "group"}},
            "data": "cmd:status"}}
        kind, _ = bc.classify(upd, OWNER)
        self.assertIsNone(kind)
        kind, payload = bc.classify(upd, OWNER, allow_groups=True)
        self.assertEqual(kind, "callback")
        self.assertEqual(payload["chat_id"], -100123)

    def test_malformed_nested_shapes_do_not_crash(self):
        # Valid JSON, wrong shapes: lists/strings where objects
        # belong must classify as not-owner, never raise.
        bad_updates = [
            {"update_id": 20, "callback_query": {
                "id": "x", "from": [], "data": "cmd:status"}},
            {"update_id": 21, "callback_query": {
                "id": "x", "from": {"id": OWNER}, "data": 5}},
            {"update_id": 22, "message": []},
            {"update_id": 23, "message": {
                "message_id": 1, "date": 1, "from": "owner",
                "chat": {"id": OWNER, "type": "private"},
                "text": "hi"}},
            {"update_id": 24, "message": {
                "message_id": 1, "date": 1, "from": {"id": OWNER},
                "chat": [], "text": "hi"}},
            {"update_id": 25, "message": {
                "message_id": 1, "date": 1, "from": {"id": OWNER},
                "chat": {"id": OWNER, "type": "private"},
                "photo": ["not-a-dict"]}},
        ]
        for upd in bad_updates:
            kind, _payload = bc.classify(upd, OWNER, allow_groups=True)
            self.assertIn(kind, (None, "unsupported"))

    def test_non_dict_and_bad_update_id(self):
        self.assertEqual(bc.classify(None, OWNER), (None, None))
        self.assertEqual(bc.classify({"update_id": "x"}, OWNER), (None, None))
        self.assertEqual(bc.classify({"update_id": True}, OWNER), (None, None))
        self.assertEqual(bc.classify({"update_id": 13}, OWNER), (None, None))


class TestParseOffset(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(bc.parse_offset(42), 42)
        self.assertEqual(bc.parse_offset(0), 0)
        self.assertEqual(bc.parse_offset("42"), 42)

    def test_invalid(self):
        self.assertIsNone(bc.parse_offset(None))
        self.assertIsNone(bc.parse_offset(True))
        self.assertIsNone(bc.parse_offset(False))
        self.assertIsNone(bc.parse_offset(3.5))
        self.assertIsNone(bc.parse_offset(-1))
        self.assertIsNone(bc.parse_offset("abc"))
        self.assertIsNone(bc.parse_offset(""))
        self.assertIsNone(bc.parse_offset([]))


class TestJsonFiles(unittest.TestCase):
    def test_load_json_file(self):
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, "good.json")
            with open(good, "w") as f:
                json.dump({"a": 1}, f)
            self.assertEqual(bc.load_json_file(good), {"a": 1})
            # Not an object.
            arr = os.path.join(d, "arr.json")
            with open(arr, "w") as f:
                f.write("[]")
            self.assertIsNone(bc.load_json_file(arr))
            # Corrupt.
            bad = os.path.join(d, "bad.json")
            with open(bad, "w") as f:
                f.write("{not json")
            self.assertIsNone(bc.load_json_file(bad))
            # Deeply nested: parser recursion must not escape.
            deep = os.path.join(d, "deep.json")
            with open(deep, "w") as f:
                f.write("[" * 20000 + "]" * 20000)
            self.assertIsNone(bc.load_json_file(deep))
            # Missing.
            self.assertIsNone(bc.load_json_file(os.path.join(d, "no.json")))

    def test_atomic_write_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "state.json")
            bc.atomic_write_json(path, {"offset": 7})
            self.assertEqual(bc.load_json_file(path), {"offset": 7})
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)
            # Overwrite is atomic and keeps the mode.
            bc.atomic_write_json(path, {"offset": 8})
            self.assertEqual(bc.load_json_file(path), {"offset": 8})


class TestRedact(unittest.TestCase):
    def test_redacts_secrets(self):
        self.assertEqual(
            bc.redact("url https://api.telegram.org/botSECRET123/getMe",
                      ["SECRET123"]),
            "url https://api.telegram.org/bot***/getMe")
        self.assertEqual(bc.redact("nothing here", ["SECRET123"]),
                         "nothing here")
        self.assertEqual(bc.redact("x", [""]), "x")


class TestSafePaths(unittest.TestCase):
    def test_sanitize_filename(self):
        self.assertEqual(bc.sanitize_filename("notes.pdf"), "notes.pdf")
        self.assertEqual(bc.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(bc.sanitize_filename("a/b\\c.txt"), "b_c.txt")
        self.assertEqual(bc.sanitize_filename(""), "file")
        self.assertEqual(bc.sanitize_filename("..."), "file")
        self.assertEqual(bc.sanitize_filename("weird name!.pdf"),
                         "weird_name_.pdf")

    def test_safe_download_path(self):
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "dl")
            os.makedirs(base)
            dest = bc.safe_download_path(base, 42, "uniq1", "notes.pdf")
            self.assertTrue(dest.startswith(os.path.realpath(base)))
            self.assertIn("42_uniq1_notes.pdf", dest)
            # Traversal in the original name cannot escape.
            dest2 = bc.safe_download_path(base, 43, "", "../../etc/passwd")
            self.assertTrue(dest2.startswith(os.path.realpath(base)))

    def test_validate_inside(self):
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "base")
            os.makedirs(base)
            inside = os.path.join(base, "f.txt")
            self.assertEqual(bc.validate_inside(base, inside),
                             os.path.realpath(inside))
            with self.assertRaises(ValueError):
                bc.validate_inside(base, "/etc/passwd")
            with self.assertRaises(ValueError):
                bc.validate_inside(base, os.path.join(base, "..", "esc"))

    def test_validate_inside_trailing_slashes(self):
        # The base itself is inside, with a trailing slash on
        # either side; a sibling with a shared prefix is not.
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "base")
            os.makedirs(base)
            self.assertEqual(bc.validate_inside(base + "/", base),
                             os.path.realpath(base))
            self.assertEqual(bc.validate_inside(base, base + "/"),
                             os.path.realpath(base))
            self.assertEqual(
                bc.validate_inside(base + "/", base + "/f.txt"),
                os.path.realpath(os.path.join(base, "f.txt")))
            with self.assertRaises(ValueError):
                bc.validate_inside(base, os.path.join(d, "base2", "f"))

    def test_is_image_file(self):
        # M7: the decision is by magic bytes, not by extension.
        # A text file named .png must be refused; real image
        # headers must pass; a missing file must be refused.
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "notes.png")
            with open(fake, "w") as f:
                f.write("just some text, not a picture")
            self.assertFalse(bc.is_image_file(fake))
            png = os.path.join(d, "real.png")
            with open(png, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
            self.assertTrue(bc.is_image_file(png))
            jpg = os.path.join(d, "real.JPG")
            with open(jpg, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 10)
            self.assertTrue(bc.is_image_file(jpg))
            gif = os.path.join(d, "real.gif")
            with open(gif, "wb") as f:
                f.write(b"GIF89a" + b"\x00" * 10)
            self.assertTrue(bc.is_image_file(gif))
            webp = os.path.join(d, "real.webp")
            with open(webp, "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4)
            self.assertTrue(bc.is_image_file(webp))
            riff_not_webp = os.path.join(d, "clip.webp")
            with open(riff_not_webp, "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 4)
            self.assertFalse(bc.is_image_file(riff_not_webp))
            self.assertFalse(bc.is_image_file(os.path.join(d, "nope.png")))
            self.assertFalse(bc.is_image_file(os.path.join(d, "x.pem")))

    def test_escape_multipart_filename(self):
        self.assertEqual(
            bc.escape_multipart_filename('a"b\r\nc\\d'), "abcd")
        self.assertEqual(bc.escape_multipart_filename("ok.png"), "ok.png")


class TestSplitText(unittest.TestCase):
    def test_short(self):
        self.assertEqual(bc.split_text("hi"), ["hi"])

    def test_exact_limit(self):
        text = "x" * 4096
        self.assertEqual(bc.split_text(text), [text])

    def test_long_splits(self):
        text = "word " * 2000  # 10000 chars
        chunks = bc.split_text(text)
        self.assertTrue(len(chunks) > 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4096)
        self.assertEqual(" ".join(c.strip() for c in chunks),
                         " ".join(text.split()))

    def test_long_word_forced_split(self):
        text = "x" * 9000
        chunks = bc.split_text(text)
        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), text)


class TestRetryAfter(unittest.TestCase):
    def test_parses(self):
        body = '{"ok":false,"error_code":429,"description":"Too Many '
        body += 'Requests","parameters":{"retry_after":34}}'
        self.assertEqual(bc.parse_retry_after_body(body), 34)

    def test_missing(self):
        self.assertIsNone(bc.parse_retry_after_body('{"ok":false}'))
        self.assertIsNone(bc.parse_retry_after_body('not json'))
        self.assertIsNone(bc.parse_retry_after_body(''))


class TestSingleton(unittest.TestCase):
    def test_second_acquire_fails(self):
        with tempfile.TemporaryDirectory() as d:
            lock_dir = os.path.join(d, "locks")
            first = bc.acquire_singleton(lock_dir, "typing:1:2")
            self.assertIsNotNone(first)
            second = bc.acquire_singleton(lock_dir, "typing:1:2")
            self.assertIsNone(second)
            first.close()
            third = bc.acquire_singleton(lock_dir, "typing:1:2")
            self.assertIsNotNone(third)
            third.close()

    def test_different_keys_coexist(self):
        with tempfile.TemporaryDirectory() as d:
            lock_dir = os.path.join(d, "locks")
            a = bc.acquire_singleton(lock_dir, "typing:1:2")
            b = bc.acquire_singleton(lock_dir, "typing:1:3")
            self.assertIsNotNone(a)
            self.assertIsNotNone(b)
            a.close()
            b.close()


class TestHandledSet(unittest.TestCase):
    def test_caches_and_refreshes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "handled.txt")
            with open(path, "w") as f:
                f.write("1:2\n")
            handled = bc.HandledSet(path)
            self.assertTrue(handled.contains("1:2"))
            self.assertFalse(handled.contains("1:3"))
            with open(path, "a") as f:
                f.write("1:3\n")
            # Force an mtime change (same-second writes may not move it).
            os.utime(path, (1000000000, 1000000000))
            self.assertTrue(handled.contains("1:3"))

    def test_same_mtime_append_still_seen(self):
        # L3: the cache key includes size and inode, so an append
        # that lands in the same timestamp still refreshes. The
        # mtime is rewound to its pre-append value to simulate a
        # filesystem with coarse timestamps.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "handled.txt")
            with open(path, "w") as f:
                f.write("1:2\n")
            before = os.stat(path)
            handled = bc.HandledSet(path)
            self.assertTrue(handled.contains("1:2"))
            with open(path, "a") as f:
                f.write("1:3\n")
            # Rewind the mtime to exactly the pre-append value.
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertTrue(handled.contains("1:3"))


class TestParseOffsetStrict(unittest.TestCase):
    def test_superscript_two_rejected(self):
        # L2: "²".isdigit() is True, but int("²") raises.
        self.assertIsNone(bc.parse_offset("²"))
        self.assertIsNone(bc.parse_offset("١٢٣"))  # Arabic-Indic digits

    def test_huge_digit_string_rejected(self):
        self.assertIsNone(bc.parse_offset("9" * 5000))

    def test_valid_still_accepted(self):
        self.assertEqual(bc.parse_offset(42), 42)
        self.assertEqual(bc.parse_offset("42"), 42)
        self.assertIsNone(bc.parse_offset(-1))
        self.assertIsNone(bc.parse_offset(True))
        self.assertIsNone(bc.parse_offset(None))


class TestParseChatId(unittest.TestCase):
    def test_int_and_digit_string(self):
        # M4: one shared validation; digit strings are accepted.
        self.assertEqual(bc.parse_chat_id(12345), 12345)
        self.assertEqual(bc.parse_chat_id("12345"), 12345)
        self.assertEqual(bc.parse_chat_id(" 12345 "), 12345)

    def test_invalid(self):
        self.assertIsNone(bc.parse_chat_id(0))
        self.assertIsNone(bc.parse_chat_id("0"))
        self.assertIsNone(bc.parse_chat_id(-5))
        self.assertIsNone(bc.parse_chat_id(True))
        self.assertIsNone(bc.parse_chat_id("abc"))
        self.assertIsNone(bc.parse_chat_id("12.5"))
        self.assertIsNone(bc.parse_chat_id(None))
        self.assertIsNone(bc.parse_chat_id(12.5))
        self.assertIsNone(bc.parse_chat_id(""))


class TestAtomicWriteMode(unittest.TestCase):
    def test_file_created_with_restrictive_mode(self):
        # L9: the file must be 0o600 from creation, not chmodded later.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            bc.atomic_write_json(path, {"last_update_id": 3})
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)
            with open(path) as f:
                self.assertEqual(json.load(f), {"last_update_id": 3})


class TestSplitIndentation(unittest.TestCase):
    def test_code_indentation_preserved(self):
        # L12: a split must not strip the indentation of the
        # following chunk.
        code = "def f():\n" + "    x = 1\n" * 600  # ~6600 chars
        chunks = bc.split_text(code, limit=4096)
        self.assertTrue(len(chunks) > 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4096)
        # Every continuation chunk must start with its indentation.
        self.assertTrue(chunks[1].startswith("    x = 1"))
        self.assertEqual("".join(
            c if i == 0 else "\n" + c for i, c in enumerate(chunks)),
            code)


class TestAppendHandled(unittest.TestCase):
    def test_append_and_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "handled.txt")
            self.assertTrue(bc.append_handled(path, "1:2"))
            self.assertFalse(bc.append_handled(path, "1:2"))
            with open(path) as f:
                self.assertEqual(f.read(), "1:2\n")

    def test_concurrent_appends_all_land(self):
        # M5: concurrent writers under the lock lose nothing.
        import threading
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "handled.txt")
            keys = ["1:%d" % i for i in range(50)]
            threads = [threading.Thread(target=bc.append_handled,
                                        args=(path, k)) for k in keys]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            with open(path) as f:
                found = {line.strip() for line in f if line.strip()}
            self.assertEqual(found, set(keys))

    def test_invalid_key_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "handled.txt")
            with self.assertRaises(ValueError):
                bc.append_handled(path, "")
            with self.assertRaises(ValueError):
                bc.append_handled(path, "1:2\n9:9")


class TestCleanupStaleLocks(unittest.TestCase):
    def test_removes_old_unlocked_keeps_live(self):
        # L4: orphaned lock files are cleaned up; a live lock is kept.
        with tempfile.TemporaryDirectory() as d:
            lock_dir = os.path.join(d, "locks")
            live = bc.acquire_singleton(lock_dir, "live-key")
            self.assertIsNotNone(live)
            stale = os.path.join(lock_dir, "lock-deadbeef")
            with open(stale, "w") as f:
                f.write("999999")
            old = time.time() - 90000
            os.utime(stale, (old, old))
            fresh = os.path.join(lock_dir, "lock-freshbeef")
            with open(fresh, "w") as f:
                f.write("999998")
            removed = bc.cleanup_stale_locks(lock_dir, max_age_secs=3600)
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(fresh))
            # The live lock file is untouched.
            live_path = os.path.join(
                lock_dir, "lock-" + bc.lock_name("live-key"))
            self.assertTrue(os.path.exists(live_path))
            live.close()


class TestAcks(unittest.TestCase):
    def test_append_and_load_latest_wins(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "handled.txt.ack")
            self.assertTrue(bc.append_ack(path, "1:2", now=100.0))
            self.assertTrue(bc.append_ack(path, "1:2", now=200.0))
            self.assertTrue(bc.append_ack(path, "1:cb:x", now=150.0))
            acks = bc.load_acks(path)
            self.assertEqual(acks["1:2"], 200.0)
            self.assertEqual(acks["1:cb:x"], 150.0)

    def test_load_acks_ignores_malformed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "acks.txt")
            with open(path, "w") as f:
                f.write("garbage\n1:2 notanumber\n1:3 42\n\n")
            self.assertEqual(bc.load_acks(path), {"1:3": 42.0})
            self.assertEqual(
                bc.load_acks(os.path.join(d, "missing")), {})

    def test_invalid_ack_key_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "acks.txt")
            with self.assertRaises(ValueError):
                bc.append_ack(path, "1:2 3")
            with self.assertRaises(ValueError):
                bc.append_ack(path, "")


if __name__ == "__main__":
    unittest.main()
