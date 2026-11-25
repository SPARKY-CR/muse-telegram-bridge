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
        self.assertEqual(params["link_preview_options"],
                         {"is_disabled": True})

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


def _parse_error():
    body = json.dumps({"ok": False, "error_code": 400,
                       "description": "Bad Request: can't parse "
                       "entities: unsupported start tag"}).encode()
    err = urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None)
    err.read = lambda: body
    return err


class TestParseMode(unittest.TestCase):
    def test_send_html_success(self):
        with mock.patch.object(telegram, "call",
                               return_value={"ok": True}) as m:
            telegram.send_text("111111111", "<b>hi</b>",
                               parse_mode="HTML")
        params = m.call_args[0][1]
        self.assertEqual(params["parse_mode"], "HTML")
        self.assertEqual(params["text"], "<b>hi</b>")

    def test_send_long_html_falls_back_to_plain_chunks(self):
        text = "<b>" + ("word " * 2000) + "</b>"
        self.assertGreater(len(text), 4096)
        with mock.patch.object(telegram, "call",
                               return_value={"ok": True}) as m:
            telegram.send_text("111111111", text, parse_mode="HTML")
        self.assertGreater(m.call_count, 1)
        for call_args in m.call_args_list:
            params = call_args[0][1]
            self.assertNotIn("parse_mode", params)
            self.assertNotIn("<b>", params["text"])
            self.assertLessEqual(len(params["text"]), 4096)

    def test_send_html_parse_failure_retries_plain(self):
        with mock.patch.object(
                telegram, "call",
                side_effect=[_parse_error(), {"ok": True}]) as m:
            telegram.send_text("111111111", "<b>hi &amp; bye</b>",
                               parse_mode="HTML")
        self.assertEqual(m.call_count, 2)
        first = m.call_args_list[0][0][1]
        second = m.call_args_list[1][0][1]
        self.assertEqual(first["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", second)
        self.assertEqual(second["text"], "hi & bye")

    def test_sanitize_html_allowlist_and_links(self):
        dirty = ('<script>alert(1)</script><b onclick="x">hi</b> '
                 '<a href="javascript:alert(1)">bad</a> '
                 '<a href="http://example.com">http</a> '
                 '<a href="https://example.com">ok</a> &lt;tag&gt;')
        clean = telegram.sanitize_html(dirty)
        self.assertNotIn("<script", clean)
        self.assertNotIn("onclick", clean)
        self.assertNotIn("javascript:", clean)
        self.assertNotIn("http://example.com", clean)
        self.assertNotIn("https://example.com", clean)
        self.assertNotIn("<a", clean)
        self.assertIn("bad http ok", clean)
        self.assertIn("<b>hi</b>", clean)
        self.assertIn("&lt;tag&gt;", clean)

    def test_strip_urls_removes_bare_links(self):
        text = ("see https://example.com/path?q=1 and "
                "www.example.org/x plus t.me/somechannel "
                "and http://a.b/c, file telegram.py stays")
        clean = telegram.strip_urls(text)
        self.assertNotIn("example.com", clean)
        self.assertNotIn("example.org", clean)
        self.assertNotIn("t.me", clean)
        self.assertNotIn("http", clean)
        self.assertIn("telegram.py stays", clean)
        self.assertEqual(clean.count("[link removed]"), 4)

    def test_send_strip_urls_flag(self):
        with mock.patch.object(telegram, "call",
                               return_value={"ok": True}) as m:
            telegram.send_text(
                "111111111", "summary https://example.com/x end",
                strip_urls_flag=True)
        sent = m.call_args[0][1]["text"]
        self.assertNotIn("example.com", sent)
        self.assertIn("[link removed]", sent)
        with mock.patch.object(telegram, "call",
                               return_value={"ok": True}) as m:
            telegram.send_text(
                "111111111",
                '<b>note</b> <a href="https://example.com">'
                "https://example.com</a>",
                parse_mode="HTML", strip_urls_flag=True)
        sent = m.call_args[0][1]["text"]
        self.assertNotIn("example.com", sent)
        self.assertNotIn("<a", sent)

    def test_send_html_visible_length_ignores_markup(self):
        text = "<b>" + ("x" * 4000) + "</b><!--" + ("y" * 500) + "-->"
        self.assertGreater(len(text), 4096)
        with mock.patch.object(telegram, "call",
                               return_value={"ok": True}) as m:
            telegram.send_text("111111111", text, parse_mode="HTML")
        self.assertEqual(m.call_count, 1)
        params = m.call_args[0][1]
        self.assertEqual(params["parse_mode"], "HTML")
        self.assertNotIn("<!--", params["text"])

    def test_send_html_non_parse_400_does_not_retry(self):
        body = json.dumps({"ok": False, "error_code": 400,
                           "description": "Bad Request: message text "
                           "is empty"}).encode()
        err = urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None)
        err.read = lambda: body
        with mock.patch.object(telegram, "call", side_effect=err) as m:
            with self.assertRaises(urllib.error.HTTPError):
                telegram.send_text("111111111", "<b>hi</b>",
                                   parse_mode="HTML")
        self.assertEqual(m.call_count, 1)

    def test_send_protect_content(self):
        with mock.patch.object(telegram, "call",
                               return_value={"ok": True}) as m:
            telegram.send_text("111111111", "private summary",
                               protect_content=True)
        self.assertTrue(m.call_args[0][1]["protect_content"])

    def test_edit_html_success(self):
        with mock.patch.object(telegram, "call",
                               return_value={"ok": True}) as m:
            telegram.edit_text("111111111", 5, "<b>new</b>",
                               parse_mode="HTML")
        params = m.call_args[0][1]
        self.assertEqual(params["parse_mode"], "HTML")
        self.assertEqual(params["link_preview_options"],
                         {"is_disabled": True})

    def test_edit_overlong_refused_before_network(self):
        with mock.patch.object(telegram, "call",
                               return_value={"ok": True}) as m:
            with self.assertRaises(ValueError):
                telegram.edit_text("111111111", 5, "x" * 4097)
        m.assert_not_called()

    def test_edit_parse_failure_retries_plain(self):
        with mock.patch.object(
                telegram, "call",
                side_effect=[_parse_error(), {"ok": True}]) as m:
            telegram.edit_text("111111111", 5, "<b>new</b>",
                               parse_mode="HTML")
        self.assertEqual(m.call_count, 2)
        second = m.call_args_list[1][0][1]
        self.assertNotIn("parse_mode", second)
        self.assertEqual(second["text"], "new")
        self.assertEqual(second["link_preview_options"],
                         {"is_disabled": True})

    def test_photo_caption_parse_failure_retries_plain(self):
        with mock.patch.object(
                telegram, "call_multipart",
                side_effect=[_parse_error(), {"ok": True}]) as m:
            result = telegram.send_photo(
                "111111111", "pic.png", caption="<b>cap</b>",
                parse_mode="HTML")
        self.assertTrue(result["ok"])
        first_fields = m.call_args_list[0][0][1]
        second_fields = m.call_args_list[1][0][1]
        self.assertEqual(first_fields["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", second_fields)
        self.assertEqual(second_fields["caption"], "cap")


class TestMenusAndCommands(unittest.TestCase):
    def test_menus_exist_and_callbacks_short(self):
        self.assertTrue({"main", "study", "app", "admin"} <= set(
            telegram.MENUS))
        for menu in telegram.MENUS.values():
            for row in menu["inline_keyboard"]:
                for button in row:
                    self.assertTrue(button["text"])
                    data = button["callback_data"]
                    self.assertTrue(data.startswith("cmd:"))
                    self.assertLessEqual(len(data.encode("utf-8")), 64)

    def test_bot_commands_valid(self):
        import re
        names = [c["command"] for c in telegram.BOT_COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        for command in telegram.BOT_COMMANDS:
            self.assertRegex(command["command"], r"^[a-z0-9_]{1,32}$")
            self.assertTrue(command["description"])
            self.assertLessEqual(len(command["description"]), 256)
        self.assertIn("status", names)
        self.assertIn("chats", names)
        for deferred in ("sendto", "askchat", "restartbridge",
                         "forget", "feed", "ideas", "doc", "image"):
            self.assertNotIn(deferred, names)

    def test_set_commands_payload_scoped_to_owner(self):
        with mock.patch.dict(os.environ,
                             {"BRIDGE_CHAT_ID": "111111111"}), \
                mock.patch.object(telegram, "call",
                                  return_value={"ok": True}) as m:
            result = telegram.set_commands()
        self.assertTrue(result["ok"])
        method, params = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual(method, "setMyCommands")
        self.assertEqual(params["commands"], telegram.BOT_COMMANDS)
        self.assertEqual(params["scope"],
                         {"type": "chat", "chat_id": 111111111})

    def test_cli_set_commands(self):
        with mock.patch.dict(os.environ,
                             {"BRIDGE_CHAT_ID": "111111111"}), \
                mock.patch.object(telegram, "call",
                                  return_value={"ok": True,
                                                "result": True}):
            self.assertEqual(telegram.main(["set_commands"]), 0)


class TestSendDocument(TelegramTestCase):
    def _mock_upload(self):
        body = json.dumps(
            {"ok": True, "result": {"message_id": 6}}).encode()
        response = mock.MagicMock()
        response.read.side_effect = [body, b""]
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        opener = mock.MagicMock()
        opener.open.return_value = response
        return mock.patch.object(urllib.request, "build_opener",
                                 return_value=opener)

    def test_document_inside_allowed_dir_ok(self):
        path = os.path.join(self.send_dir, "report.txt")
        with open(path, "w") as f:
            f.write("report body")
        with self._mock_upload():
            result = telegram.send_document("111111111", path,
                                            caption="report")
        self.assertTrue(result["ok"])

    def test_document_deleted_after_successful_send(self):
        path = os.path.join(self.send_dir, "report.txt")
        with open(path, "w") as f:
            f.write("report body")
        with self._mock_upload():
            result = telegram.send_document("111111111", path)
        self.assertTrue(result["ok"])
        self.assertFalse(os.path.exists(path))

    def test_document_symlink_refused(self):
        target = os.path.join(self.send_dir, "target.txt")
        with open(target, "w") as f:
            f.write("target")
        link = os.path.join(self.send_dir, "link.txt")
        os.symlink(target, link)
        with self.assertRaises(ValueError):
            telegram.send_document("111111111", link)
        self.assertTrue(os.path.exists(target))

    def test_document_outside_allowed_dir_refused(self):
        outside = os.path.join(self.tmp.name, "outside.txt")
        with open(outside, "w") as f:
            f.write("outside")
        with self.assertRaises(ValueError):
            telegram.send_document("111111111", outside)

    def test_document_oversize_refused(self):
        path = os.path.join(self.send_dir, "big.bin")
        with open(path, "wb") as f:
            f.truncate(telegram.MAX_DOCUMENT_BYTES + 1)
        with self.assertRaises(ValueError):
            telegram.send_document("111111111", path)

    def test_cli_send_document_arbitrary_chat_refused(self):
        with mock.patch.dict(os.environ,
                             {"BRIDGE_CHAT_ID": "111111111"}), \
                mock.patch.object(telegram, "send_document",
                                  return_value={"ok": True}) as m:
            code = telegram.main(
                ["send_document", "999999999", "report.txt"])
        self.assertEqual(code, 1)
        m.assert_not_called()


class TestSendVoice(TelegramTestCase):
    def _write_ogg(self, name="note.ogg", data=b"OggS-fake-opus"):
        path = os.path.join(self.send_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _mock_upload_capture(self):
        body = json.dumps(
            {"ok": True, "result": {"message_id": 7}}).encode()
        response = mock.MagicMock()
        response.read.side_effect = [body, b""]
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        opener = mock.MagicMock()
        opener.open.return_value = response
        return mock.patch.object(urllib.request, "build_opener",
                                 return_value=opener), opener

    def test_voice_inside_allowed_dir_ok(self):
        path = self._write_ogg()
        patcher, opener = self._mock_upload_capture()
        with patcher:
            result = telegram.send_voice("111111111", path,
                                         caption="hi", reply_to=3)
        self.assertTrue(result["ok"])
        request = opener.open.call_args[0][0]
        self.assertIn(b'name="voice"', request.data)
        self.assertIn(b'filename="note.ogg"', request.data)
        self.assertIn(b'name="reply_to_message_id"', request.data)
        self.assertFalse(os.path.exists(path))

    def test_voice_non_ogg_refused(self):
        path = self._write_ogg(data=b"not-ogg-data")
        with self.assertRaises(ValueError):
            telegram.send_voice("111111111", path)

    def test_voice_symlink_refused(self):
        target = self._write_ogg("target.ogg")
        link = os.path.join(self.send_dir, "link.ogg")
        os.symlink(target, link)
        with self.assertRaises(ValueError):
            telegram.send_voice("111111111", link)
        self.assertTrue(os.path.exists(target))

    def test_voice_outside_allowed_dir_refused(self):
        outside = os.path.join(self.tmp.name, "outside.ogg")
        with open(outside, "wb") as f:
            f.write(b"OggS-fake")
        with self.assertRaises(ValueError):
            telegram.send_voice("111111111", outside)

    def test_voice_oversize_refused(self):
        path = os.path.join(self.send_dir, "big.ogg")
        with open(path, "wb") as f:
            f.write(b"OggS")
            f.truncate(telegram.MAX_VOICE_BYTES + 1)
        with self.assertRaises(ValueError):
            telegram.send_voice("111111111", path)

    def test_cli_send_voice_arbitrary_chat_refused(self):
        with mock.patch.dict(os.environ,
                             {"BRIDGE_CHAT_ID": "111111111"}), \
                mock.patch.object(telegram, "send_voice",
                                  return_value={"ok": True}) as m:
            code = telegram.main(
                ["send_voice", "999999999", "note.ogg"])
        self.assertEqual(code, 1)
        m.assert_not_called()


class TestVoiceReply(TelegramTestCase):
    def setUp(self):
        super().setUp()
        self.voices_dir = os.path.join(self.tmp.name, "voices")
        os.makedirs(self.voices_dir)
        for name in telegram.PIPER_VOICES.values():
            with open(os.path.join(self.voices_dir, name), "wb") as f:
                f.write(b"fake-model")
        self.patch_voices = mock.patch.dict(
            os.environ, {"PIPER_VOICES_DIR": self.voices_dir})
        self.patch_voices.start()

    def tearDown(self):
        self.patch_voices.stop()
        super().tearDown()

    def _fake_run(self, fail_piper=False):
        calls = []

        def run(cmd, input=None, capture_output=True, timeout=None):
            calls.append((cmd, input))
            result = mock.MagicMock()
            result.returncode = 0
            result.stderr = b""
            if "-m" in cmd and "piper" in cmd:
                if fail_piper:
                    result.returncode = 1
                    result.stderr = b"piper boom"
                    return result
                with open(cmd[-1], "wb") as f:
                    f.write(b"RIFF-fake-wav")
            else:
                with open(cmd[-1], "wb") as f:
                    f.write(b"OggS-fake-opus")
            return result

        return run, calls

    def test_voice_reply_fa_synthesizes_and_cleans_up(self):
        run, calls = self._fake_run()
        with mock.patch.object(telegram.subprocess, "run",
                               side_effect=run), \
                mock.patch.object(
                    telegram, "call_multipart",
                    return_value={"ok": True,
                                  "result": {"message_id": 8}}) as m:
            result = telegram.voice_reply(
                "111111111", "fa", "سلام دنیا")
        self.assertTrue(result["ok"])
        method, fields, file_field = m.call_args[0][:3]
        self.assertEqual(method, "sendVoice")
        self.assertEqual(file_field, "voice")
        self.assertEqual(fields["chat_id"], "111111111")
        piper_cmd, piper_input = calls[0]
        self.assertIn("fa_IR-ganji-medium.onnx", piper_cmd[4])
        self.assertEqual(piper_input, "سلام دنیا".encode("utf-8"))
        self.assertEqual(os.listdir(self.send_dir), [])

    def test_voice_reply_en_uses_english_voice(self):
        run, calls = self._fake_run()
        with mock.patch.object(telegram.subprocess, "run",
                               side_effect=run), \
                mock.patch.object(
                    telegram, "call_multipart",
                    return_value={"ok": True}):
            telegram.voice_reply("111111111", "en", "hello")
        self.assertIn("en_US-lessac-medium.onnx", calls[0][0][4])

    def test_voice_reply_bad_lang_refused(self):
        with self.assertRaises(ValueError):
            telegram.voice_reply("111111111", "de", "hallo")

    def test_voice_reply_empty_and_overlong_refused(self):
        with self.assertRaises(ValueError):
            telegram.voice_reply("111111111", "fa", "   ")
        with self.assertRaises(ValueError):
            telegram.voice_reply(
                "111111111", "fa",
                "x" * (telegram.MAX_VOICE_TEXT_CHARS + 1))

    def test_voice_reply_piper_failure_cleans_up(self):
        run, _calls = self._fake_run(fail_piper=True)
        with mock.patch.object(telegram.subprocess, "run",
                               side_effect=run):
            with self.assertRaises(RuntimeError):
                telegram.voice_reply("111111111", "fa", "سلام")
        self.assertEqual(os.listdir(self.send_dir), [])

    def test_cli_voice_reply_arbitrary_chat_refused(self):
        with mock.patch.dict(os.environ,
                             {"BRIDGE_CHAT_ID": "111111111"}), \
                mock.patch.object(telegram, "voice_reply",
                                  return_value={"ok": True}) as m:
            code = telegram.main(
                ["voice_reply", "999999999", "hi", "--lang", "en"])
        self.assertEqual(code, 1)
        m.assert_not_called()


class TestBridgeStatus(unittest.TestCase):
    def _runtime(self, d):
        with open(os.path.join(d, "poller-state.json"), "w") as f:
            json.dump({"beat_at": 1000.0, "cycles": 7,
                       "errors": 2, "pid": 4242}, f)
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump({"offset": 123}, f)
        with open(os.path.join(d, "seen.json"), "w") as f:
            json.dump({"last_update_id": 120}, f)
        with open(os.path.join(d, "pending.json"), "w") as f:
            json.dump({"entries": {
                "1": {"payload": {"chat_id": 111111111,
                                  "text": "SECRET TEXT"}},
                "2": {"dropped": True,
                      "payload": {"chat_id": 111111111,
                                  "text": "DROP SECRET"}}}}, f)
        with open(os.path.join(d, "handled.txt"), "w") as f:
            f.write("111111111:10\n111111111:11\n")
        with open(os.path.join(d, "scan-failures.count"), "w") as f:
            f.write("3\n")
        with open(os.path.join(d, "inbox.jsonl"), "w") as f:
            f.write('{"text":"SECRET TEXT"}\n')

    def test_bridge_status_counts_and_redaction(self):
        with tempfile.TemporaryDirectory() as d:
            self._runtime(d)
            status = telegram.bridge_status(d, now=1005.0)
        self.assertEqual(status["heartbeat_age_secs"], 5)
        self.assertTrue(status["poller_alive"])
        self.assertEqual(status["cycles"], 7)
        self.assertEqual(status["errors"], 2)
        self.assertNotIn("offset", status)
        self.assertNotIn("seen_last_update_id", status)
        self.assertEqual(status["pending_count"], 2)
        self.assertEqual(status["dropped_count"], 1)
        self.assertEqual(status["handled_count"], 2)
        self.assertEqual(status["scan_failure_count"], 3)
        self.assertGreater(status["inbox_bytes"], 0)
        rendered = json.dumps(status)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("111111111", rendered)
        self.assertNotIn("TESTTOKEN", rendered)

    def test_cli_bridge_status(self):
        with tempfile.TemporaryDirectory() as d:
            self._runtime(d)
            with mock.patch.object(sys, "stdout", io.StringIO()) as out:
                code = telegram.main(
                    ["bridge_status", "--runtime-dir", d])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["pending_count"], 2)
        self.assertNotIn("SECRET", out.getvalue())


class TestUiStateLedgerSettings(unittest.TestCase):
    def _typed_source(self, d, message_id=11):
        with open(os.path.join(d, "config.json"), "w") as fh:
            json.dump({"chat_id": 111111111}, fh)
        typed = {"update_id": 2, "message": {
            "message_id": message_id, "date": 1001,
            "from": {"id": 111111111},
            "chat": {"id": 111111111, "type": "private"},
            "text": "remind me"}}
        with open(os.path.join(d, "inbox.jsonl"), "w") as fh:
            fh.write(json.dumps(typed) + "\n")
        return f"111111111:{message_id}"

    def test_ui_consume_once_and_replay_refused(self):
        with tempfile.TemporaryDirectory() as d:
            entry = telegram.ui_put(
                "quiz", {"action": "reveal", "text": "hello"},
                111111111, 42, runtime_dir=d, now=1000.0)
            self.assertEqual(len(entry["id"]), 32)
            # Omitting the binding never validates.
            self.assertIsNone(telegram.ui_consume(
                entry["id"], kind="quiz",
                payload_hash=entry["payload_hash"],
                runtime_dir=d, now=1001.0))
            self.assertIsNone(telegram.ui_consume(
                entry["id"], kind="quiz", chat_id=111111111,
                payload_hash=entry["payload_hash"],
                runtime_dir=d, now=1001.0))
            consumed = telegram.ui_consume(
                entry["id"], kind="quiz", chat_id=111111111,
                message_id=42, payload_hash=entry["payload_hash"],
                runtime_dir=d, now=1001.0)
            self.assertIsNotNone(consumed)
            self.assertTrue(consumed["consumed"])
            replay = telegram.ui_consume(
                entry["id"], kind="quiz", chat_id=111111111,
                message_id=42, payload_hash=entry["payload_hash"],
                runtime_dir=d, now=1002.0)
            self.assertIsNone(replay)

    def test_ui_expiry_and_binding_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            entry = telegram.ui_put(
                "quiz", {"answer": "x"}, 111111111, 7,
                ttl_secs=10, runtime_dir=d, now=1000.0)
            self.assertIsNone(telegram.ui_consume(
                entry["id"], kind="quiz", chat_id=999999999,
                message_id=7, payload_hash=entry["payload_hash"],
                runtime_dir=d, now=1001.0))
            self.assertIsNone(telegram.ui_consume(
                entry["id"], kind="quiz", chat_id=111111111,
                message_id=7, payload_hash=entry["payload_hash"],
                runtime_dir=d, now=1011.0))

    def test_action_ledger_duplicate_prevention(self):
        with tempfile.TemporaryDirectory() as d:
            payload = {"action": "done", "task": "fake-task"}
            self.assertTrue(telegram.action_reserve(
                "entry-1", payload, runtime_dir=d, now=1000.0))
            self.assertFalse(telegram.action_reserve(
                "entry-1", payload, runtime_dir=d, now=1001.0))
            # A generic reserve is not a confirm_begin action:
            # commit must fail, and commit without any reserve
            # fails too.
            self.assertFalse(telegram.action_commit(
                "entry-1", payload=payload, runtime_dir=d,
                now=1002.0))
            self.assertFalse(telegram.action_commit(
                "confirm:missing", payload=payload, runtime_dir=d,
                now=1003.0))

    def test_confirm_begin_replay_hash_and_commit_failed(self):
        with tempfile.TemporaryDirectory() as d:
            payload = {"action": "done", "task_id": "fake-1",
                       "source_turn_class": "typed"}
            entry = telegram.ui_put(
                "confirm", payload, 111111111,
                runtime_dir=d, now=1000.0,
                source_key=self._typed_source(d))
            telegram.ui_bind(entry["id"], 42, runtime_dir=d,
                             now=1000.5)
            self.assertIsNone(telegram.confirm_begin(
                entry["id"], chat_id=111111111, message_id=42,
                payload_hash="wrong", runtime_dir=d, now=1001.0,
                turn_class="callback"))
            begun = telegram.confirm_begin(
                entry["id"], chat_id=111111111, message_id=42,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1002.0, turn_class="callback")
            self.assertTrue(begun["begun"])
            self.assertEqual(begun["payload"], payload)
            self.assertEqual(begun["action_id"],
                             "confirm:" + entry["id"])
            self.assertIsNone(telegram.confirm_begin(
                entry["id"], chat_id=111111111, message_id=42,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1003.0, turn_class="callback"))
            self.assertTrue(telegram.action_commit(
                begun["action_id"], payload=payload, runtime_dir=d,
                now=1004.0, result="failed"))
            self.assertFalse(telegram.action_commit(
                begun["action_id"], payload=payload, runtime_dir=d,
                now=1005.0, result="committed"))

    def test_confirm_begin_reserve_without_commit_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            payload = {"action": "note", "text": "hello"}
            entry = telegram.ui_put(
                "confirm", payload, 111111111,
                runtime_dir=d, now=1000.0,
                source_key=self._typed_source(d))
            telegram.ui_bind(entry["id"], 9, runtime_dir=d,
                             now=1000.5)
            self.assertTrue(telegram.action_reserve(
                "confirm:" + entry["id"], payload, runtime_dir=d,
                now=1001.0, turn_class="callback"))
            begun = telegram.confirm_begin(
                entry["id"], chat_id=111111111, message_id=9,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1002.0, turn_class="callback")
            self.assertFalse(begun["begun"])
            self.assertTrue(begun["unknown"])
            self.assertEqual(begun["state"], "reserved")

    def test_media_turn_cannot_reserve_or_confirm(self):
        with tempfile.TemporaryDirectory() as d:
            payload = {"action": "note", "text": "hello",
                       "source_turn_class": "media_readonly"}
            with self.assertRaises(ValueError):
                telegram.action_reserve(
                    "entry-media", payload, runtime_dir=d,
                    now=1000.0, turn_class="media_readonly")
            entry = telegram.ui_put(
                "confirm", {"action": "note", "text": "hello"},
                111111111, runtime_dir=d, now=1000.0,
                source_key=self._typed_source(d))
            telegram.ui_bind(entry["id"], 3, runtime_dir=d,
                             now=1000.5)
            state_path = os.path.join(d, "ui-state.json")
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
            stored = state["entries"][entry["id"]]
            stored["data"]["source_turn_class"] = "media_readonly"
            stored["payload_hash"] = telegram._payload_hash(
                stored["data"])
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            begun = telegram.confirm_begin(
                entry["id"], chat_id=111111111, message_id=3,
                payload_hash=stored["payload_hash"], runtime_dir=d,
                now=1001.0, turn_class="callback")
            self.assertFalse(begun["begun"])
            self.assertEqual(begun["reason"], "media_readonly")

    def test_confirm_full_flow_differing_message_ids(self):
        # Real flow: the owner's source message is 100, the
        # preview carrying the buttons is sent afterwards and
        # gets id 200, and the callback names 200. The entry
        # must bind to 200, never to the source message.
        with tempfile.TemporaryDirectory() as d:
            payload = {"action": "note", "text": "hello",
                       "source_turn_class": "typed"}
            with self.assertRaises(ValueError):
                telegram.ui_put("confirm", payload, 111111111, 100,
                                runtime_dir=d, now=1000.0,
                                source_key=self._typed_source(d))
            entry = telegram.ui_put(
                "confirm", payload, 111111111,
                runtime_dir=d, now=1000.0,
                source_key=self._typed_source(d))
            self.assertFalse(entry["bound"])
            # Unbound: no confirm validates yet, with or
            # without a message id.
            self.assertIsNone(telegram.confirm_begin(
                entry["id"], chat_id=111111111, message_id=200,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1001.0, turn_class="callback"))
            self.assertIsNone(telegram.confirm_begin(
                entry["id"], chat_id=111111111,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1001.0, turn_class="callback"))
            bound = telegram.ui_bind(entry["id"], 200,
                                     chat_id=111111111,
                                     runtime_dir=d, now=1002.0)
            self.assertTrue(bound["bound"])
            self.assertEqual(bound["message_id"], 200)
            # The source message id does not confirm, and
            # omitting either id does not bypass the binding.
            self.assertIsNone(telegram.confirm_begin(
                entry["id"], chat_id=111111111, message_id=100,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1003.0, turn_class="callback"))
            self.assertIsNone(telegram.confirm_begin(
                entry["id"], chat_id=111111111,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1003.0, turn_class="callback"))
            self.assertIsNone(telegram.confirm_begin(
                entry["id"], message_id=200,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1003.0, turn_class="callback"))
            begun = telegram.confirm_begin(
                entry["id"], chat_id=111111111, message_id=200,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1004.0, turn_class="callback")
            self.assertTrue(begun["begun"])
            self.assertEqual(begun["message_id"], 200)
            self.assertEqual(begun["payload"], payload)

    def test_ui_bind_one_way_and_guards(self):
        with tempfile.TemporaryDirectory() as d:
            entry = telegram.ui_put(
                "confirm", {"action": "note"}, 111111111,
                runtime_dir=d, now=1000.0,
                source_key=self._typed_source(d))
            self.assertIsNone(telegram.ui_bind(
                "missing", 200, runtime_dir=d, now=1001.0))
            self.assertIsNone(telegram.ui_bind(
                entry["id"], 200, chat_id=999999999,
                runtime_dir=d, now=1001.0))
            bound = telegram.ui_bind(entry["id"], 200,
                                     runtime_dir=d, now=1001.0)
            self.assertEqual(bound["message_id"], 200)
            # Same id again is a no-op; a different id never
            # moves the binding.
            again = telegram.ui_bind(entry["id"], 200,
                                     runtime_dir=d, now=1002.0)
            self.assertEqual(again["message_id"], 200)
            self.assertIsNone(telegram.ui_bind(
                entry["id"], 300, runtime_dir=d, now=1002.0))
            kept = telegram.ui_get(entry["id"], runtime_dir=d,
                                   now=1003.0)
            self.assertEqual(kept["message_id"], 200)

    def test_confirm_rejects_entry_with_bound_flag_cleared(self):
        # A stored message id alone is not a binding: an entry
        # whose bound flag is false never confirms, even when
        # the callback names that stored id.
        with tempfile.TemporaryDirectory() as d:
            entry = telegram.ui_put(
                "confirm", {"action": "note"}, 111111111,
                runtime_dir=d, now=1000.0,
                source_key=self._typed_source(d))
            telegram.ui_bind(entry["id"], 200, runtime_dir=d,
                             now=1000.5)
            state_path = os.path.join(d, "ui-state.json")
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
            state["entries"][entry["id"]]["bound"] = False
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            self.assertIsNone(telegram.confirm_begin(
                entry["id"], chat_id=111111111, message_id=200,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1001.0, turn_class="callback"))

    def test_ui_consume_refuses_confirm_entries(self):
        with tempfile.TemporaryDirectory() as d:
            payload = {"action": "note", "text": "hello"}
            entry = telegram.ui_put(
                "confirm", payload, 111111111,
                runtime_dir=d, now=1000.0,
                source_key=self._typed_source(d))
            telegram.ui_bind(entry["id"], 55, runtime_dir=d,
                             now=1000.5)
            # A confirm entry never consumes through ui_consume,
            # and the refusal leaves it live for confirm_begin.
            self.assertIsNone(telegram.ui_consume(
                entry["id"], kind="confirm", chat_id=111111111,
                message_id=55, payload_hash=entry["payload_hash"],
                runtime_dir=d, now=1001.0))
            begun = telegram.confirm_begin(
                entry["id"], chat_id=111111111, message_id=55,
                payload_hash=entry["payload_hash"], runtime_dir=d,
                now=1002.0, turn_class="callback")
            self.assertTrue(begun["begun"])

    def test_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(telegram.settings_get(d),
                             {"mode": "short", "locked": False})
            updated = telegram.settings_set(
                mode="study", locked=True, runtime_dir=d)
            self.assertEqual(updated, {"mode": "study", "locked": True})
            self.assertEqual(telegram.settings_get(d), updated)


    def test_confirm_cap_replaces_oldest(self):
        with tempfile.TemporaryDirectory() as d:
            ids = []
            for i in range(3):
                entry = telegram.ui_put(
                    "confirm", {"n": i}, 111111111,
                    runtime_dir=d, now=1000.0 + i,
                    source_key=self._typed_source(d))
                ids.append(entry["id"])
            fourth = telegram.ui_put(
                "confirm", {"n": 3}, 111111111,
                runtime_dir=d, now=1003.0,
                source_key=self._typed_source(d))
            self.assertEqual(fourth.get("replaced"), [ids[0]])
            self.assertIsNone(telegram.ui_get(
                ids[0], runtime_dir=d, now=1004.0))
            self.assertIsNotNone(telegram.ui_get(
                ids[1], runtime_dir=d, now=1004.0))

    def test_daily_confirm_cap_survives_gc(self):
        with tempfile.TemporaryDirectory() as d:
            old_cap = telegram.DAILY_CONFIRM_CAP
            telegram.DAILY_CONFIRM_CAP = 2
            base = 1795608000.0
            try:
                source = self._typed_source(d)
                telegram.ui_put(
                    "confirm", {"n": 0}, 111111111,
                    ttl_secs=10, runtime_dir=d, now=base,
                    source_key=source)
                telegram.ui_put(
                    "confirm", {"n": 1}, 111111111,
                    ttl_secs=10, runtime_dir=d, now=base + 3600,
                    source_key=source)
                with self.assertRaises(ValueError):
                    telegram.ui_put(
                        "confirm", {"n": 2}, 111111111,
                        ttl_secs=10, runtime_dir=d,
                        now=base + 7200, source_key=source)
            finally:
                telegram.DAILY_CONFIRM_CAP = old_cap

    def test_daily_quiz_cap_survives_gc(self):
        with tempfile.TemporaryDirectory() as d:
            old_cap = telegram.DAILY_QUIZ_CAP
            telegram.DAILY_QUIZ_CAP = 2
            base = 1795608000.0
            try:
                telegram.ui_put(
                    "quiz", {"n": 0}, 111111111, 7,
                    ttl_secs=10, runtime_dir=d, now=base)
                telegram.ui_put(
                    "quiz", {"n": 1}, 111111111, 8,
                    ttl_secs=10, runtime_dir=d, now=base + 3600)
                with self.assertRaises(ValueError):
                    telegram.ui_put(
                        "quiz", {"n": 2}, 111111111, 9,
                        ttl_secs=10, runtime_dir=d,
                        now=base + 7200)
            finally:
                telegram.DAILY_QUIZ_CAP = old_cap

    def test_daily_counts_survive_other_ui_writes(self):
        with tempfile.TemporaryDirectory() as d:
            old_confirm_cap = telegram.DAILY_CONFIRM_CAP
            old_quiz_cap = telegram.DAILY_QUIZ_CAP
            telegram.DAILY_CONFIRM_CAP = 1
            telegram.DAILY_QUIZ_CAP = 1
            base = 1795608000.0
            try:
                source = self._typed_source(d)
                confirm = telegram.ui_put(
                    "confirm", {"n": 0}, 111111111,
                    ttl_secs=10, runtime_dir=d, now=base,
                    source_key=source)
                telegram.ui_bind(confirm["id"], 42,
                                 runtime_dir=d, now=base + 1)
                quiz = telegram.ui_put(
                    "quiz", {"n": 0}, 111111111, 7,
                    ttl_secs=10, runtime_dir=d, now=base)
                telegram.ui_consume(
                    quiz["id"], kind="quiz", chat_id=111111111,
                    message_id=7,
                    payload_hash=quiz["payload_hash"],
                    runtime_dir=d, now=base + 1)
                with self.assertRaises(ValueError):
                    telegram.ui_put(
                        "confirm", {"n": 1}, 111111111,
                        ttl_secs=10, runtime_dir=d,
                        now=base + 3600, source_key=source)
                with self.assertRaises(ValueError):
                    telegram.ui_put(
                        "quiz", {"n": 1}, 111111111, 8,
                        ttl_secs=10, runtime_dir=d,
                        now=base + 3600)
            finally:
                telegram.DAILY_CONFIRM_CAP = old_confirm_cap
                telegram.DAILY_QUIZ_CAP = old_quiz_cap

    def test_daily_counts_malformed_values_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            base = 1795608000.0
            key = telegram._daily_count_key("quiz", base)
            state_path = os.path.join(d, "ui-state.json")
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"entries": {}, "daily_counts": {
                    key: "bad", "quiz:not-a-date": 99,
                    "confirm:2026-11-25": True}}, f)
            entry = telegram.ui_put(
                "quiz", {"n": 0}, 111111111, 7,
                runtime_dir=d, now=base)
            self.assertIsNotNone(entry)
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
            self.assertEqual(state["daily_counts"], {key: 1})

    def test_confirm_requires_source_key(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                telegram.ui_put(
                    "confirm", {"n": 0}, 111111111,
                    runtime_dir=d, now=1000.0)
            with self.assertRaises(ValueError):
                telegram.ui_put(
                    "confirm", {"n": 0}, 111111111,
                    runtime_dir=d, now=1000.0, source_key="  ")

    def test_source_turn_derived_from_inbox(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w") as fh:
                json.dump({"chat_id": 111111111}, fh)
            photo = {"update_id": 1, "message": {
                "message_id": 10, "date": 1000,
                "from": {"id": 111111111},
                "chat": {"id": 111111111, "type": "private"},
                "photo": [{"file_id": "f1"}]}}
            typed = {"update_id": 2, "message": {
                "message_id": 11, "date": 1001,
                "from": {"id": 111111111},
                "chat": {"id": 111111111, "type": "private"},
                "text": "remind me"}}
            with open(os.path.join(d, "inbox.jsonl"), "w") as fh:
                fh.write(json.dumps(photo) + "\n")
                fh.write(json.dumps(typed) + "\n")
            with self.assertRaises(ValueError):
                telegram.ui_put(
                    "confirm", {"n": 1}, 111111111,
                    runtime_dir=d, now=1002.0,
                    source_key="111111111:10")
            entry = telegram.ui_put(
                "confirm", {"n": 2}, 111111111,
                runtime_dir=d, now=1002.0,
                source_key="111111111:11")
            self.assertEqual(entry["source_turn_class"], "typed")
            self.assertEqual(
                entry["data"]["source_turn_class"], "typed")
            with self.assertRaises(ValueError):
                telegram.ui_put(
                    "confirm", {"n": 3}, 111111111,
                    runtime_dir=d, now=1003.0,
                    source_key="111111111:99")

    def test_sanitizer_expandable_and_code_language(self):
        out = telegram.sanitize_html(
            '<blockquote expandable>long</blockquote>'
            '<pre><code class="language-python">x=1</code></pre>'
            '<code class="evil">y</code>')
        self.assertIn("<blockquote expandable>", out)
        self.assertIn('class="language-python"', out)
        self.assertNotIn("evil", out)


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
