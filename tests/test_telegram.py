"""Tests for bin/telegram.py: download confinement, photo
allow-listing, filename escaping, message splitting, edit
idempotence, and token redaction (the review's items #8, #9,
#12, #20). Network calls are mocked; no Telegram request is made."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "poller"))
import telegram  # noqa: E402


class TelegramTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.download_dir = os.path.join(self.tmp.name, "downloads")
        os.makedirs(self.download_dir)
        self.send_dir = os.path.join(self.tmp.name, "send")
        os.makedirs(self.send_dir)
        self.patch_dl = mock.patch.dict(
            os.environ, {"TELEGRAM_DOWNLOAD_DIR": self.download_dir})
        self.patch_send = mock.patch.dict(
            os.environ, {"TELEGRAM_SEND_PATHS": self.send_dir})
        self.patch_dl.start()
        self.patch_send.start()
        # Never touch the real credential system in tests.
        self.patch_token = mock.patch.object(
            telegram, "get_token", return_value="TESTTOKEN")
        self.patch_token.start()

    def tearDown(self):
        self.patch_token.stop()
        self.patch_send.stop()
        self.patch_dl.stop()
        self.tmp.cleanup()


class TestDownload(TelegramTestCase):
    def _mock_getfile(self, file_path="photos/a.jpg",
                      unique="uniq1"):
        return mock.patch.object(
            telegram, "call",
            return_value={"ok": True, "result": {
                "file_path": file_path, "file_unique_id": unique}})

    def _mock_bytes(self, data=b"hello-bytes"):
        response = mock.MagicMock()
        response.read.side_effect = [data, b""]
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        opener = mock.MagicMock()
        opener.open.return_value = response
        return mock.patch.object(urllib.request, "build_opener",
                                 return_value=opener)

    def test_download_to_dir_generates_safe_name(self):
        with self._mock_getfile(), self._mock_bytes():
            final, total = telegram.download_file("fid1", self.download_dir)
        self.assertTrue(final.startswith(os.path.realpath(self.download_dir)))
        self.assertIn("file_uniq1_a.jpg", final)
        self.assertEqual(total, 11)
        with open(final, "rb") as f:
            self.assertEqual(f.read(), b"hello-bytes")
        mode = os.stat(final).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_download_traversal_blocked(self):
        # #8: a crafted destination must not escape the download dir.
        with self._mock_getfile():
            with self.assertRaises(ValueError):
                telegram.download_file(
                    "fid1", "/tmp/evil.txt")
            with self.assertRaises(ValueError):
                telegram.download_file(
                    "fid1", os.path.join(self.download_dir, "..", "evil.txt"))

    def test_download_outside_dir_with_trailing_slash_blocked(self):
        # A directory destination outside the download dir is
        # refused, trailing slash or not (never silently
        # reinterpreted as the download dir).
        outside = os.path.join(self.tmp.name, "elsewhere") + os.sep
        with self._mock_getfile():
            with self.assertRaises(ValueError):
                telegram.download_file("fid1", outside)

    def test_download_traversal_in_name_sanitized(self):
        with self._mock_getfile(file_path="../../etc/passwd"), \
                self._mock_bytes():
            final, _ = telegram.download_file("fid1", self.download_dir)
        self.assertTrue(final.startswith(os.path.realpath(self.download_dir)))
        self.assertNotIn("..", os.path.basename(final))

    def test_download_size_cap(self):
        with self._mock_getfile(), \
                self._mock_bytes(b"x" * 100):
            with self.assertRaises(ValueError):
                telegram.download_file("fid1", self.download_dir,
                                       max_bytes=10)
        # No partial file left behind.
        self.assertEqual(os.listdir(self.download_dir), [])

    def test_download_getfile_failure(self):
        with mock.patch.object(telegram, "call",
                               return_value={"ok": False}):
            with self.assertRaises(RuntimeError):
                telegram.download_file("fid1", self.download_dir)


class TestSendPhoto(TelegramTestCase):
    def _photo(self, name="pic.png", size=100):
        path = os.path.join(self.send_dir, name)
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"x" * size)
        return path

    def _mock_upload(self):
        body = json.dumps(
            {"ok": True, "result": {"message_id": 5}}).encode()
        response = mock.MagicMock()
        # side_effect (not return_value): the real response reader
        # loops until read() returns b"", so a fixed return value
        # would loop forever and exhaust memory.
        response.read.side_effect = [body, b""]
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        opener = mock.MagicMock()
        opener.open.return_value = response
        return mock.patch.object(urllib.request, "build_opener",
                                 return_value=opener)

    def test_photo_inside_allowed_dir_ok(self):
        path = self._photo()
        with self._mock_upload():
            result = telegram.call_multipart(
                "sendPhoto", {"chat_id": "1"}, "photo", path)
        self.assertTrue(result["ok"])

    def test_photo_outside_allowed_dir_refused(self):
        # #9: a file outside the allow-list cannot be uploaded.
        outside = os.path.join(self.tmp.name, "secret.png")
        with open(outside, "wb") as f:
            f.write(b"x" * 10)
        with self.assertRaises(ValueError):
            telegram.call_multipart("sendPhoto", {"chat_id": "1"},
                                    "photo", outside)

    def test_non_image_refused(self):
        path = os.path.join(self.send_dir, "key.pem")
        with open(path, "w") as f:
            f.write("private key material")
        with self.assertRaises(ValueError):
            telegram.call_multipart("sendPhoto", {"chat_id": "1"},
                                    "photo", path)

    def test_text_file_named_png_refused(self):
        # M7: the extension alone is not enough — a text file
        # renamed to .png must be refused by its magic bytes.
        path = os.path.join(self.send_dir, "notes.png")
        with open(path, "w") as f:
            f.write("these are meeting notes, not pixels")
        with self.assertRaises(ValueError):
            telegram.call_multipart("sendPhoto", {"chat_id": "1"},
                                    "photo", path)

    def test_oversize_refused(self):
        path = self._photo(size=11 * 1024 * 1024)
        with self.assertRaises(ValueError):
            telegram.call_multipart("sendPhoto", {"chat_id": "1"},
                                    "photo", path)

    def test_filename_escaped_in_header(self):
        # #9: CR/LF/quotes in the file name must not reach the header.
        path = self._photo('a"b.png')
        captured = {}

        class FakeResponse:
            def __init__(self):
                self._sent = False

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, size=-1):
                # The real response reader loops until b"".
                if self._sent:
                    return b""
                self._sent = True
                return json.dumps({"ok": True}).encode()

        def fake_open(request, timeout=None):
            captured["data"] = request.data
            return FakeResponse()

        opener = mock.MagicMock()
        opener.open.side_effect = fake_open
        with mock.patch.object(urllib.request, "build_opener",
                               return_value=opener):
            telegram.call_multipart("sendPhoto", {"chat_id": "1"},
                                    "photo", path)
        header = captured["data"]
        self.assertNotIn(b'"b.png"', header)
        self.assertIn(b'filename="ab.png"', header)


class TestSendText(unittest.TestCase):
    def test_short_sends_once(self):
        with mock.patch.object(telegram, "call",
                               return_value={"ok": True}) as m:
            telegram.send_text("1", "hi", reply_to=9)
        self.assertEqual(m.call_count, 1)
        params = m.call_args[0][1]
        self.assertEqual(params["reply_to_message_id"], 9)
        self.assertTrue(params["allow_sending_without_reply"])

    def test_long_splits_and_replies_once(self):
        text = "word " * 2000
        with mock.patch.object(telegram, "call",
                               return_value={"ok": True}) as m:
            telegram.send_text("1", text, reply_to=9)
        self.assertGreater(m.call_count, 1)
        first = m.call_args_list[0][0][1]
        second = m.call_args_list[1][0][1]
        self.assertIn("reply_to_message_id", first)
        self.assertNotIn("reply_to_message_id", second)
        for call_args in m.call_args_list:
            self.assertLessEqual(len(call_args[0][1]["text"]), 4096)

    def test_second_chunk_failure_reports_parts_sent(self):
        # M6: chunk 1 went out, chunk 2 failed — the error must
        # say how many parts were sent, so the caller can resume.
        text = "word " * 2000  # splits into several chunks
        calls = {"n": 0}

        def _flaky(method, params=None, timeout=30):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("network down")
            return {"ok": True, "result": {"message_id": 1}}

        with mock.patch.object(telegram, "call", side_effect=_flaky):
            with self.assertRaises(telegram.PartialSendError) as ctx:
                telegram.send_text("1", text)
        self.assertEqual(ctx.exception.parts_sent, 1)
        self.assertGreater(ctx.exception.parts_total, 1)
        self.assertEqual(len(ctx.exception.results), 1)

    def test_first_chunk_failure_raises_original(self):
        # M6: nothing was delivered, so there is no partial state.
        with mock.patch.object(telegram, "call",
                               side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError) as ctx:
                telegram.send_text("1", "word " * 2000)
        self.assertNotIsInstance(ctx.exception,
                                 telegram.PartialSendError)


class TestEditText(unittest.TestCase):
    def test_identical_text_is_success(self):
        # #20: "message is not modified" must not fail the edit.
        body = json.dumps({"ok": False, "error_code": 400,
                           "description": "Bad Request: message is not "
                           "modified"}).encode()
        err = urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None)
        err.read = lambda: body
        with mock.patch.object(telegram, "call", side_effect=err):
            result = telegram.edit_text("1", 5, "same")
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["unchanged"])

    def test_other_400_reraises(self):
        body = json.dumps({"ok": False, "error_code": 400,
                           "description": "Bad Request: message to edit "
                           "not found"}).encode()
        err = urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None)
        err.read = lambda: body
        with mock.patch.object(telegram, "call", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                telegram.edit_text("1", 5, "new")


class TestRedaction(unittest.TestCase):
    def test_url_error_redacted(self):
        # #12: an InvalidURL embeds the request URL (and token).
        token = "SECRET-TOKEN-123"

        def boom(request, timeout=None):
            raise urllib.error.URLError(
                "https://api.telegram.org/botSECRET-TOKEN-123/getMe")

        opener = mock.MagicMock()
        opener.open.side_effect = boom
        with mock.patch.object(telegram, "get_token",
                               return_value=token), \
                mock.patch.object(urllib.request, "build_opener",
                                  return_value=opener):
            with self.assertRaises(RuntimeError) as ctx:
                telegram.call("getMe")
        self.assertNotIn("SECRET-TOKEN-123", str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_no_chained_traceback_leaks_token(self):
        # L1: the redacted error must not carry the original
        # exception (with its token-bearing URL) in __cause__.
        token = "SECRET-TOKEN-123"

        def boom(request, timeout=None):
            raise urllib.error.URLError(
                "https://api.telegram.org/botSECRET-TOKEN-123/getMe")

        opener = mock.MagicMock()
        opener.open.side_effect = boom
        with mock.patch.object(telegram, "get_token",
                               return_value=token), \
                mock.patch.object(urllib.request, "build_opener",
                                  return_value=opener):
            with self.assertRaises(RuntimeError) as ctx:
                telegram.call("getMe")
        exc = ctx.exception
        self.assertIsNone(exc.__cause__)
        self.assertTrue(exc.__suppress_context__)
        import traceback
        rendered = "".join(traceback.format_exception(exc))
        self.assertNotIn("SECRET-TOKEN-123", rendered)

    def test_http_error_preserved(self):
        err = urllib.error.HTTPError("http://x", 429, "Too Many", {}, None)
        opener = mock.MagicMock()
        opener.open.side_effect = err
        with mock.patch.object(telegram, "get_token",
                               return_value="T"), \
                mock.patch.object(urllib.request, "build_opener",
                                  return_value=opener):
            with self.assertRaises(urllib.error.HTTPError):
                telegram.call("getMe")

    def test_retry_after_parsed(self):
        body = json.dumps({"ok": False, "parameters": {"retry_after": 12}})
        err = urllib.error.HTTPError("http://x", 429, "Too Many", {}, None)
        err.read = lambda: body.encode()
        self.assertEqual(telegram.http_retry_after(err), 12)
        err2 = urllib.error.HTTPError("http://x", 400, "Bad", {}, None)
        self.assertIsNone(telegram.http_retry_after(err2))


class TestPollerRunning(unittest.TestCase):
    def test_missing_lock_is_not_running(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(telegram.poller_running(
                os.path.join(d, "no.lock")))

    def test_held_lock_is_running(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "poller.lock")
            f = open(path, "a")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.assertTrue(telegram.poller_running(path))
            finally:
                f.close()
            self.assertFalse(telegram.poller_running(path))


class TestCliSendStdin(unittest.TestCase):
    def test_send_dash_reads_stdin(self):
        with mock.patch.dict(os.environ, {"BRIDGE_CHAT_ID": "1"}), \
                mock.patch.object(telegram, "call",
                                  return_value={"ok": True, "result": {}}), \
                mock.patch.object(sys, "stdin", io.StringIO("from stdin")):
            code = telegram.main(["send", "1", "-"])
        self.assertEqual(code, 0)

    def test_menu_option(self):
        with mock.patch.dict(os.environ, {"BRIDGE_CHAT_ID": "1"}), \
                mock.patch.object(telegram, "call",
                                  return_value={"ok": True, "result": {}}) as m:
            code = telegram.main(["send", "1", "hi", "--menu", "main"])
        self.assertEqual(code, 0)
        params = m.call_args[0][1]
        self.assertIn("inline_keyboard", params["reply_markup"])

    def test_getupdates_refused_while_poller_runs(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            lock = os.path.join(d, "poller.lock")
            f = open(lock, "a")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with mock.patch.dict(os.environ,
                                     {"TELEGRAM_POLLER_LOCK": lock}):
                    code = telegram.main(["getupdates"])
                self.assertEqual(code, 2)
            finally:
                f.close()

    def test_partial_send_cli_reports_parts_sent(self):
        # M6: the CLI prints JSON with parts_sent on a partial send.
        import io as _io
        calls = {"n": 0}

        def _flaky(method, params=None, timeout=30):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("network down")
            return {"ok": True, "result": {"message_id": 1}}

        with mock.patch.dict(os.environ, {"BRIDGE_CHAT_ID": "1"}), \
                mock.patch.object(telegram, "call", side_effect=_flaky), \
                mock.patch.object(sys, "stdout", _io.StringIO()) as out:
            code = telegram.main(["send", "1", "word " * 2000])
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["parts_sent"], 1)
        self.assertGreater(payload["parts_total"], 1)


class TestMarkHandled(unittest.TestCase):
    def test_cli_marks_and_dedups(self):
        # M5: mark_handled appends under a lock and skips duplicates.
        with tempfile.TemporaryDirectory() as d:
            handled = os.path.join(d, "handled.txt")
            code = telegram.main(["mark_handled", "123:456",
                                  "--handled-file", handled])
            self.assertEqual(code, 0)
            code = telegram.main(["mark_handled", "123:456",
                                  "--handled-file", handled])
            self.assertEqual(code, 0)
            with open(handled) as f:
                self.assertEqual(f.read(), "123:456\n")

    def test_cli_callback_key(self):
        with tempfile.TemporaryDirectory() as d:
            handled = os.path.join(d, "handled.txt")
            code = telegram.main(["mark_handled", "123:cb:abc",
                                  "--handled-file", handled])
            self.assertEqual(code, 0)
            with open(handled) as f:
                self.assertEqual(f.read(), "123:cb:abc\n")


class TestChatRestriction(unittest.TestCase):
    def test_send_to_arbitrary_chat_refused(self):
        # A prompt-injected agent must not message strangers:
        # only the configured owner chat is allowed.
        with mock.patch.dict(os.environ, {"BRIDGE_CHAT_ID": "111"}), \
                mock.patch.object(telegram, "call",
                                  return_value={"ok": True}) as m:
            code = telegram.main(["send", "999", "hi"])
        self.assertEqual(code, 1)
        m.assert_not_called()

    def test_send_to_owner_allowed(self):
        with mock.patch.dict(os.environ, {"BRIDGE_CHAT_ID": "111"}), \
                mock.patch.object(telegram, "call",
                                  return_value={"ok": True,
                                                "result": {}}) as m:
            code = telegram.main(["send", "111", "hi"])
        self.assertEqual(code, 0)
        m.assert_called_once()

    def test_send_to_recent_bridge_group_chat_allowed(self):
        # A group chat with a pending payload is a recent
        # bridge chat; an unknown group is not.
        with tempfile.TemporaryDirectory() as d:
            pending = os.path.join(d, "pending.json")
            with open(pending, "w") as f:
                json.dump({"entries": {"1": {"payload": {
                    "kind": "message", "chat_id": -100123}}}}, f)
            env = {"BRIDGE_CHAT_ID": "111",
                   "TELEGRAM_PENDING_FILE": pending}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(telegram, "call",
                                      return_value={"ok": True,
                                                    "result": {}}):
                self.assertEqual(
                    telegram.main(["send", "-100123", "hi"]), 0)
                self.assertEqual(
                    telegram.main(["send", "-999", "hi"]), 1)

    def test_send_photo_to_arbitrary_chat_refused(self):
        with mock.patch.dict(os.environ, {"BRIDGE_CHAT_ID": "111"}), \
                mock.patch.object(telegram, "call_multipart",
                                  return_value={"ok": True}) as m:
            code = telegram.main(["send_photo", "999", "x.png"])
        self.assertEqual(code, 1)
        m.assert_not_called()


class TestAckCli(unittest.TestCase):
    def test_ack_writes_loadable_ack(self):
        import bridge_common as bc
        with tempfile.TemporaryDirectory() as d:
            ack_file = os.path.join(d, "handled.txt.ack")
            code = telegram.main(["ack", "123:456",
                                  "--ack-file", ack_file])
            self.assertEqual(code, 0)
            acks = bc.load_acks(ack_file)
            self.assertIn("123:456", acks)


if __name__ == "__main__":
    unittest.main()
