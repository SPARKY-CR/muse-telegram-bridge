#!/usr/bin/env python3
"""Telegram Bot API helper for the Muse-Telegram bridge.

Calls api.telegram.org with the bot token. The token is resolved in
two ways, in order:

1. Muse Secure Vault (when running inside a Muse VM): the token is
   stored in the `custom.telegram` connection and exchanged for a
   short-lived `hsurr:` surrogate. The real token is never read,
   printed, or logged here; only the surrogate passes through, and
   it is never printed either.
2. Environment variable: TELEGRAM_BOT_TOKEN. Used when the Muse
   credential helpers are not available (standalone use).

IMPORTANT (Muse path): the surrogate must be placed in the URL path
UNENCODED. url_with_surrogate_path_segment() percent-encodes it
(hsurr%3A...), which Sentinel does not recognise, so the request
reaches Telegram with a malformed token and fails with 404. Placing
the raw surrogate works: Sentinel replaces the literal hsurr: form
with the real token.

Safety rules enforced here (see the code review):
- The token is redacted out of every exception message before it
  can reach a log line (some URL errors embed the request URL).
- `download` writes only under the download directory, with a
  generated safe file name, a size cap, and a temp-file + rename
  (no sender-controlled path can escape the directory).
- `send_photo` only sends image files from allow-listed
  directories (TELEGRAM_SEND_PATHS, default: the bridge's
  runtime/outbox), with a size cap, and the file name is escaped
  in the multipart header. Images are verified by magic bytes,
  not by extension.
- `send` splits messages over 4096 characters, reads text from
  stdin with `-` (so secrets never appear in `ps`), and sets
  allow_sending_without_reply when replying to a deleted message.
  A failure after the first chunk reports parts_sent, so a retry
  resumes instead of duplicating.
- `mark_handled` appends a handled key under a lock, so it can
  never be lost by the poller's prune of handled.txt.
- `send`/`send_photo` (and the other chat-targeting commands)
  refuse any chat_id that is not the configured owner chat, an
  explicitly allowed chat, or a recent bridge chat (one with a
  pending payload), so a prompt-injected agent cannot message
  arbitrary chats.
- `ack` records that work on a message has started; while the
  ack is fresh the scan postpones redelivery instead of
  duplicating long-running work.

Usage:
  telegram.py getme
  telegram.py getupdates [--offset N] [--timeout SECONDS] [--force]
  telegram.py send <chat_id> <text|-> [--reply-to ID] [--markup JSON]
                                      [--menu NAME] [--parse-mode HTML]
  telegram.py send_photo <chat_id> <photo_path> [caption]
                                      [--parse-mode HTML]
  telegram.py send_document <chat_id> <document_path> [caption]
                                      [--parse-mode HTML]
  telegram.py chat_action <chat_id> [action]
  telegram.py react <chat_id> <message_id> <emoji>
  telegram.py answer_callback <callback_query_id> [text]
  telegram.py get_file <file_id>
  telegram.py download <file_id> <dest_dir_or_path> [--max-bytes N]
  telegram.py edit_message <chat_id> <message_id> <text>
                                      [--parse-mode HTML]
  telegram.py mark_handled <chat_id:message_id|chat_id:cb:callback_id>
  telegram.py ack <chat_id:message_id|chat_id:cb:callback_id>
  telegram.py set_commands
  telegram.py bridge_status [--runtime-dir DIR]
  telegram.py setwebhook <url> [--secret TOKEN]
  telegram.py deletewebhook [--drop-pending]
  telegram.py getwebhookinfo
"""

import argparse
import fcntl
import hashlib
import html
import html.parser
import json
import mimetypes
import os
import re
import secrets
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "poller"))
import bridge_common  # noqa: E402

# Muse Secure Vault helpers, when available (running inside a Muse VM).
try:
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import (  # noqa: E402
        DynamicCredentialError,
        dynamic_credential_entry,
        ensure_allowed_url,
        read_json_response,
    )
    HAVE_MUSE_CREDENTIALS = True
except ImportError:
    HAVE_MUSE_CREDENTIALS = False
    DynamicCredentialError = RuntimeError

    def read_json_response(response):
        return json.loads(response.read().decode("utf-8"))

CREDENTIAL = "custom.telegram"
ALLOWED_HOSTS = ["api.telegram.org"]

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bridge_runtime_dir():
    """The bridge's runtime dir: BRIDGE_DIR when the hook set it
    (a custom runtime location), else the repo's runtime/
    (the standalone default). The hook exports BRIDGE_DIR before
    waking the agent, so every path below follows the runtime
    the hook actually scans — never a different directory
    (review item C2c)."""
    return os.environ.get("BRIDGE_DIR") or os.path.join(REPO_DIR, "runtime")

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024  # 50 MB per sent document

# Predefined inline keyboards, so callers never hand-write JSON
# (and never hit shell-quoting bugs like an apostrophe in a label).
# Callback data stays short (Telegram caps it at 64 bytes); any
# state behind a button lives server-side, never in the callback.
MENUS = {
    "main": {"inline_keyboard": [
        [{"text": "📅 امروز", "callback_data": "cmd:today"},
         {"text": "📊 وضعیت", "callback_data": "cmd:status"}],
        [{"text": "🎓 مطالعه", "callback_data": "cmd:study"},
         {"text": "🧩 اپ", "callback_data": "cmd:app"}],
        [{"text": "❓ راهنما", "callback_data": "cmd:help"}],
    ]},
    "study": {"inline_keyboard": [
        [{"text": "📚 مطالعه", "callback_data": "cmd:study"},
         {"text": "🧠 آزمون", "callback_data": "cmd:quiz"}],
        [{"text": "📅 برنامه", "callback_data": "cmd:schedule"}],
        [{"text": "🏠 اصلی", "callback_data": "cmd:main"}],
    ]},
    "app": {"inline_keyboard": [
        [{"text": "💬 چت‌ها", "callback_data": "cmd:chats"},
         {"text": "🎯 هدف‌ها", "callback_data": "cmd:goals"}],
        [{"text": "✅ کارها", "callback_data": "cmd:tasks"},
         {"text": "⏰ یادآوری‌ها", "callback_data": "cmd:reminders"}],
        [{"text": "🏠 اصلی", "callback_data": "cmd:main"}],
    ]},
    "admin": {"inline_keyboard": [
        [{"text": "🩺 سلامت", "callback_data": "cmd:health"},
         {"text": "📊 وضعیت", "callback_data": "cmd:status"}],
        [{"text": "🔒 حریم خصوصی", "callback_data": "cmd:privacy"}],
        [{"text": "🏠 اصلی", "callback_data": "cmd:main"}],
    ]},
}

# Bot command menu registered with setMyCommands, scoped to the
# owner's chat. Only the revised Phase 1/2 surface is listed;
# deferred external/destructive commands are absent on purpose.
BOT_COMMANDS = [
    {"command": "start", "description": "شروع و منوی اصلی"},
    {"command": "menu", "description": "منوی اصلی"},
    {"command": "help", "description": "راهنمای فرمان‌ها"},
    {"command": "status", "description": "وضعیت پل و مصرف"},
    {"command": "health", "description": "سلامت پل با شمارش‌ها"},
    {"command": "today", "description": "برنامه و کارهای امروز"},
    {"command": "schedule", "description": "برنامه‌ی امروز"},
    {"command": "study", "description": "وضعیت مطالعه و مرور"},
    {"command": "goals", "description": "هدف‌های فعال"},
    {"command": "goal", "description": "جزئیات یک هدف"},
    {"command": "chats", "description": "فهرست چت‌های اپ"},
    {"command": "chat", "description": "خلاصه‌ی یک چت اپ"},
    {"command": "search", "description": "جستجو در حافظه و چت‌ها"},
    {"command": "tasks", "description": "کارهای باز"},
    {"command": "reminders", "description": "یادآوری‌های فعال"},
    {"command": "privacy", "description": "حریم خصوصی و داده‌های پل"},
    {"command": "remind", "description": "ساخت یادآوری"},
    {"command": "quiz", "description": "آزمون ساده‌ی مطالعه"},
    {"command": "note", "description": "ثبت نکته با پیش‌نمایش"},
    {"command": "progress", "description": "ثبت پیشرفت با پیش‌نمایش"},
    {"command": "done", "description": "بستن کار با تأیید"},
    {"command": "cancelreminder", "description": "لغو یادآوری با تأیید"},
    {"command": "lock", "description": "قفل‌کردن فرمان‌های نوشتنی"},
    {"command": "mode", "description": "حالت پاسخ: کوتاه، عمیق، مطالعه، کد"},
    {"command": "unlock", "description": "بازکردن قفل فرمان‌ها"},
]


def get_token():
    """Return the bot token (Muse surrogate or env var), never printed."""
    if HAVE_MUSE_CREDENTIALS:
        entry = dynamic_credential_entry(CREDENTIAL)
        return str(entry["surrogate"]).strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "no bot token: set TELEGRAM_BOT_TOKEN or run inside a Muse VM "
            "with the custom.telegram connection")
    return token


def check_url(url):
    """Allow only api.telegram.org, via Muse's checker when available."""
    if HAVE_MUSE_CREDENTIALS:
        ensure_allowed_url(url, ALLOWED_HOSTS)
        return
    host = urllib.parse.urlparse(url).hostname
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(f"refusing non-Telegram host: {host}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def _redacted_error(exc, token):
    """Re-raise an exception with the token scrubbed from its
    message. HTTPError is preserved as-is (its str() is only
    'HTTP Error <code>: <reason>' and carries no URL); every other
    error becomes a RuntimeError with a redacted message, because
    URL errors embed the full request URL, token included. The
    new error is raised `from None`: chaining (`from exc`) would
    keep the original traceback — URL and token included —
    attached to it."""
    if isinstance(exc, urllib.error.HTTPError):
        raise exc
    raise RuntimeError(bridge_common.redact(str(exc), [token])) from None


def call(method, params=None, timeout=30):
    """Call a Telegram Bot API method (POST JSON when params are given)."""
    token = get_token()
    # Raw token/surrogate, unencoded: Sentinel only replaces the literal
    # hsurr: form in the path. (See module docstring for why the
    # percent-encoded form from url_with_surrogate_path_segment fails
    # with 404.)
    url = f"https://api.telegram.org/bot{token}/{method}"
    check_url(url)
    data = None
    headers = {}
    if params:
        data = json.dumps(params).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            return read_json_response(response)
    except Exception as exc:  # noqa: BLE001 - redacted re-raise
        _redacted_error(exc, token)


def http_retry_after(exc):
    """Return Telegram's retry_after seconds for a 429 HTTPError,
    or None. Reads the error body once."""
    if not isinstance(exc, urllib.error.HTTPError) or exc.code != 429:
        return None
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - no body, no retry hint
        return None
    return bridge_common.parse_retry_after_body(body)


class _HTMLTextExtractor(html.parser.HTMLParser):
    """Collect visible text from Telegram HTML for plain fallback."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li", "blockquote", "pre"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def strip_html_tags(text):
    """Return the visible plain text of an HTML message: tags
    removed, entities unescaped, block breaks kept as newlines.
    Used when HTML is too long to send safely or Telegram rejects
    its entities, so the reply still arrives as plain text."""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(str(text))
        parser.close()
    except Exception:  # noqa: BLE001 - fallback must never fail
        return str(text)
    return "".join(parser.parts).strip("\n")


def _http_error_body(exc):
    """Best-effort text of an HTTPError body (read once)."""
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - unreadable body
        return ""


def is_parse_entity_error(exc):
    """True only for Telegram's explicit entity-parse rejection.
    Other 400s are real errors and must not trigger a plain-text
    retry that could duplicate or mask them."""
    if not isinstance(exc, urllib.error.HTTPError) or exc.code != 400:
        return False
    body = _http_error_body(exc).lower()
    return ("can't parse entities" in body
            or "cannot parse entities" in body)


_ALLOWED_HTML_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "blockquote", "tg-spoiler",
}


class _HTMLSanitizer(html.parser.HTMLParser):
    """Allow-list sanitizer for Telegram HTML.

    Only Telegram formatting tags survive; text is escaped and
    the output is balanced. Anchor tags are dropped entirely
    (their visible text is kept), so no clickable HTML link can
    leave the helper. Everything else (scripts, event handlers,
    unknown tags) is dropped while its visible text is kept as
    escaped text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []

    def _close_through(self, tag):
        if tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            self.parts.append("\n")
            return
        if tag not in _ALLOWED_HTML_TAGS:
            return
        self.parts.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("p", "div", "li"):
            self.parts.append("\n")
            return
        if tag in _ALLOWED_HTML_TAGS:
            self._close_through(tag)

    def handle_data(self, data):
        self.parts.append(html.escape(data, quote=False))

    def close(self):
        super().close()
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")


def sanitize_html(text):
    """Return Telegram-safe HTML for text (allow-listed tags,
    no anchor tags, escaped text, balanced tags)."""
    parser = _HTMLSanitizer()
    try:
        parser.feed(str(text))
        parser.close()
    except Exception:  # noqa: BLE001 - sanitizer must never fail open
        return html.escape(str(text), quote=False)
    return "".join(parser.parts)


_BARE_URL_RE = re.compile(
    r"(?:https?://|www\.|t\.me/)[^\s<>\"]+", re.IGNORECASE)


def strip_urls(text):
    """Replace bare URLs with a neutral placeholder.

    Telegram auto-links bare URLs even when link previews are
    disabled and anchor tags are stripped, so data-driven
    replies (chat/search summaries, media turns) strip them in
    code instead of relying on the worker to remember."""
    return _BARE_URL_RE.sub("[link removed]", str(text))


def visible_html_length(text):
    """Visible character count of HTML after tags/entities are
    resolved — the length Telegram's 4096 limit applies to."""
    return len(strip_html_tags(text))


def _parse_any_chat_id(value):
    """Parse a chat id that may be negative (group chats).
    Returns a non-zero int or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value != 0 else None
    if isinstance(value, str):
        text = value.strip()
        digits = text[1:] if text.startswith("-") else text
        if digits.isascii() and digits.isdigit():
            try:
                parsed = int(text)
            except ValueError:
                return None
            return parsed if parsed != 0 else None
    return None


def _configured_owner_chat_id():
    """The owner's chat id from the environment or the bridge
    config (the same config the hook and poller read)."""
    for env_name in ("BRIDGE_CHAT_ID", "TELEGRAM_OWNER_CHAT_ID"):
        parsed = bridge_common.parse_chat_id(os.environ.get(env_name))
        if parsed:
            return parsed
    candidates = []
    for env_name in ("TELEGRAM_CONFIG_FILE", "BRIDGE_CONFIG"):
        if os.environ.get(env_name):
            candidates.append(os.environ[env_name])
    candidates.append(os.path.join(_bridge_runtime_dir(), "config.json"))
    for path in candidates:
        config = bridge_common.load_json_file(path)
        if config:
            parsed = bridge_common.parse_chat_id(config.get("chat_id"))
            if parsed:
                return parsed
    return None


def _recent_bridge_chat_ids():
    """Chat ids with a pending bridge payload (recent bridge
    chats, e.g. a group chat awaiting its reply). Read from
    pending.json (and its .bak); anything unreadable simply
    contributes nothing."""
    pending_path = os.environ.get(
        "TELEGRAM_PENDING_FILE",
        os.path.join(_bridge_runtime_dir(), "pending.json"))
    chats = set()
    for path in (pending_path, pending_path + ".bak"):
        data = bridge_common.load_json_file(path)
        if not data:
            continue
        entries = data.get("entries")
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            if isinstance(payload, dict):
                parsed = _parse_any_chat_id(payload.get("chat_id"))
                if parsed is not None:
                    chats.add(parsed)
    return chats


def allowed_chat_ids():
    """Chats the CLI may message: the configured owner chat,
    TELEGRAM_ALLOWED_CHAT_IDS (comma/space separated), and
    recent bridge chats with pending payloads."""
    allowed = set()
    owner = _configured_owner_chat_id()
    if owner:
        allowed.add(owner)
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    for part in raw.replace(",", " ").split():
        parsed = _parse_any_chat_id(part)
        if parsed is not None:
            allowed.add(parsed)
    allowed |= _recent_bridge_chat_ids()
    return allowed


def require_allowed_chat(chat_id, command):
    """Refuse a chat-targeting command aimed outside the
    allowed chats. Returns the parsed chat id. This is the
    prompt-injection boundary: message/file contents can ask
    the agent to message a stranger, but the helper will not."""
    parsed = _parse_any_chat_id(chat_id)
    if parsed is None or parsed not in allowed_chat_ids():
        raise ValueError(
            f"{command} refused: chat_id {chat_id!r} is not the "
            "configured owner chat or a recent bridge chat")
    return parsed


def _send_dirs():
    """Directories send_photo may read from (colon-separated env,
    default: the bridge's outbox directory only)."""
    raw = os.environ.get("TELEGRAM_SEND_PATHS", "")
    if raw.strip():
        return [d for d in raw.split(os.pathsep) if d.strip()]
    return [os.path.join(_bridge_runtime_dir(), "outbox")]


def call_multipart(method, fields, file_field, file_path, timeout=120,
                   max_bytes=None, require_image=None):
    """Call a Bot API method with a file upload (multipart/form-data).

    fields is a dict of plain form fields; file_field is the name of
    the file part (e.g. "photo" or "document") and file_path the
    local file to send.

    The file must be inside an allow-listed send/outbox directory
    and under the size cap, so a prompt-injected agent cannot upload
    arbitrary files from anywhere on disk. Photos additionally must
    really be images (magic bytes, not extension). The file name is
    escaped before it goes into the multipart header.
    """
    if require_image is None:
        require_image = file_field == "photo"
    if max_bytes is None:
        max_bytes = (bridge_common.MAX_PHOTO_BYTES if require_image
                     else MAX_DOCUMENT_BYTES)
    command = "send_photo" if require_image else "send_document"
    allowed = _send_dirs()
    real = None
    for base in allowed:
        try:
            real = bridge_common.validate_inside(base, file_path)
            break
        except ValueError:
            continue
    if real is None:
        raise ValueError(
            "%s refused: %r is outside the allowed directories %s"
            % (command, file_path, allowed))
    if not os.path.isfile(real):
        raise ValueError(
            "%s refused: %r is not a regular file" % (command, file_path))
    if require_image and not bridge_common.is_image_file(real):
        raise ValueError(
            "send_photo refused: %r is not an image file" % file_path)
    size = os.path.getsize(real)
    if size > max_bytes:
        raise ValueError(
            "%s refused: %d bytes exceeds the %d byte cap"
            % (command, size, max_bytes))

    token = get_token()
    url = f"https://api.telegram.org/bot{token}/{method}"
    check_url(url)
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode("utf-8")
        body += (f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                 ).encode("utf-8")
        body += str(value).encode("utf-8") + b"\r\n"
    filename = bridge_common.escape_multipart_filename(
        os.path.basename(real))
    mime, _ = mimetypes.guess_type(real)
    body += f"--{boundary}\r\n".encode("utf-8")
    body += (f'Content-Disposition: form-data; name="{file_field}"; '
             f'filename="{filename}"\r\n').encode("utf-8")
    body += (f"Content-Type: {mime or 'application/octet-stream'}\r\n\r\n"
             ).encode("utf-8")
    with open(real, "rb") as f:
        body += f.read()
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        url, data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            return read_json_response(response)
    except Exception as exc:  # noqa: BLE001 - redacted re-raise
        _redacted_error(exc, token)


def download_dir():
    """The directory downloads are confined to."""
    return os.environ.get(
        "TELEGRAM_DOWNLOAD_DIR",
        os.path.join(_bridge_runtime_dir(), "downloads"))


def download_file(file_id, dest, timeout=120,
                  max_bytes=bridge_common.MAX_DOWNLOAD_BYTES):
    """Download a Telegram file by its file_id.

    `dest` is a directory (a safe file name is generated inside it)
    or a full file path (its directory must be the download dir and
    its name is sanitized). The download is confined to the download
    directory, capped at max_bytes, written to a temp file with
    O_EXCL, then renamed into place. Returns (final_path, bytes).

    The destination is validated BEFORE any network call, so an
    invalid destination never costs a getFile round trip.
    """
    base = os.path.realpath(download_dir())
    os.makedirs(base, exist_ok=True)
    if os.path.isdir(dest) or dest.endswith(os.sep):
        # A directory destination (trailing slash or not) must
        # be the download dir or inside it — never silently
        # reinterpreted when it points somewhere else.
        dest_real = os.path.realpath(dest)
        if dest_real != base:
            bridge_common.validate_inside(base, dest_real)
    else:
        parent = os.path.dirname(os.path.abspath(dest)) or base
        bridge_common.validate_inside(base, parent)
    info = call("getFile", {"file_id": file_id})
    if not info.get("ok"):
        raise RuntimeError(f"getFile failed: {info}")
    result = info.get("result") or {}
    file_path = result.get("file_path")
    if not file_path:
        raise RuntimeError(f"getFile returned no path: {info}")
    file_unique_id = result.get("file_unique_id", "")
    if os.path.isdir(dest) or dest.endswith(os.sep):
        final = bridge_common.safe_download_path(
            base, "file", file_unique_id,
            os.path.basename(file_path))
    else:
        final = bridge_common.safe_download_path(
            base, "file", file_unique_id,
            os.path.basename(dest))
    token = get_token()
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    check_url(url)
    request = urllib.request.Request(url, method="GET")
    total = 0
    fd, tmp_path = tempfile.mkstemp(prefix=".dl-", dir=base)
    try:
        try:
            response_cm = urllib.request.build_opener(NoRedirect()).open(
                request, timeout=timeout)
        except Exception as exc:
            _redacted_error(exc, token)
        try:
            with response_cm as response:
                with os.fdopen(fd, "wb") as f:
                    fd = None
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(
                                "download exceeds the %d byte cap" % max_bytes)
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
        except ValueError:
            # Our own validation error (size cap). It never
            # contains the token, so it propagates unchanged.
            raise
        except Exception as exc:
            _redacted_error(exc, token)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, final)
    finally:
        # Clean up the temp file unless it was renamed into place.
        if os.path.exists(tmp_path):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return final, total


class PartialSendError(RuntimeError):
    """A split send that delivered some chunks and then failed.
    Carries parts_sent (how many chunks reached Telegram) and
    the results so far, so the caller can resume after the last
    delivered chunk instead of re-sending (and duplicating) the
    whole message."""

    def __init__(self, message, parts_sent, parts_total, results):
        super().__init__(message)
        self.parts_sent = parts_sent
        self.parts_total = parts_total
        self.results = results


def _send_chunks(chat_id, chunks, reply_to=None, markup=None,
                 parse_mode=None, protect_content=False):
    """Send pre-split chunks. Only the first chunk carries the
    reply reference and markup. A failure after the first chunk
    raises PartialSendError; a first-chunk failure raises the
    original error, since nothing was delivered."""
    results = []
    for i, chunk in enumerate(chunks):
        params = {"chat_id": chat_id, "text": chunk,
                  "link_preview_options": {"is_disabled": True}}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if protect_content:
            params["protect_content"] = True
        if reply_to is not None and i == 0:
            params["reply_to_message_id"] = reply_to
            # If the original message was deleted, the reply must
            # not fail the whole send.
            params["allow_sending_without_reply"] = True
        if markup and i == 0:
            params["reply_markup"] = markup
        try:
            results.append(call("sendMessage", params))
        except Exception as exc:  # noqa: BLE001 - partial state
            if i == 0:
                raise
            raise PartialSendError(
                f"send failed at chunk {i + 1}/{len(chunks)}: {exc}",
                parts_sent=i, parts_total=len(chunks),
                results=results) from None
    return results


def send_text(chat_id, text, reply_to=None, markup=None,
              parse_mode=None, protect_content=False,
              strip_urls_flag=False):
    """Send text, splitting plain text at the 4096-character
    limit. HTML is sanitized first and sent only when its
    visible length fits in one message, so a tag is never split
    across chunks: longer visible text falls back to stripped
    plain text, and only Telegram's explicit can't-parse-
    entities rejection retries once as stripped plain text.
    With strip_urls_flag, bare URLs are removed before any
    sanitizing or splitting, so no chunk can carry one."""
    if parse_mode not in (None, "HTML"):
        raise ValueError(f"unsupported parse_mode: {parse_mode!r}")
    if strip_urls_flag:
        text = strip_urls(text)
    if parse_mode == "HTML":
        sanitized = sanitize_html(text)
        plain = strip_html_tags(sanitized)
        if len(plain) > bridge_common.MAX_MESSAGE_CHARS:
            return _send_chunks(
                chat_id, bridge_common.split_text(plain),
                reply_to=reply_to, markup=markup,
                protect_content=protect_content)
        try:
            return _send_chunks(chat_id, [sanitized],
                                reply_to=reply_to, markup=markup,
                                parse_mode="HTML",
                                protect_content=protect_content)
        except urllib.error.HTTPError as exc:
            if not is_parse_entity_error(exc):
                raise
            return _send_chunks(
                chat_id, bridge_common.split_text(plain),
                reply_to=reply_to, markup=markup,
                protect_content=protect_content)
    return _send_chunks(chat_id, bridge_common.split_text(text),
                        reply_to=reply_to, markup=markup,
                        protect_content=protect_content)


def _caption_fields(chat_id, caption=None, parse_mode=None):
    fields = {"chat_id": chat_id}
    if caption:
        fields["caption"] = (sanitize_html(caption)
                             if parse_mode == "HTML" else caption)
        if parse_mode:
            fields["parse_mode"] = parse_mode
    return fields


def send_photo(chat_id, photo_path, caption=None, parse_mode=None):
    """Send a photo from the send/outbox allow-list. An HTML
    caption rejected for entity parsing retries once as plain
    stripped text, so the photo still arrives."""
    if parse_mode not in (None, "HTML"):
        raise ValueError(f"unsupported parse_mode: {parse_mode!r}")
    fields = _caption_fields(chat_id, caption, parse_mode)
    try:
        return call_multipart("sendPhoto", fields, "photo", photo_path)
    except urllib.error.HTTPError as exc:
        if parse_mode != "HTML" or not caption \
                or not is_parse_entity_error(exc):
            raise
        plain_fields = _caption_fields(
            chat_id, strip_html_tags(caption))
        return call_multipart("sendPhoto", plain_fields, "photo",
                              photo_path)


def _validate_document_path(document_path):
    """Resolve a document path under the send/outbox allow-list.
    Symlinks are refused outright, the resolved path must stay
    under an allow-listed directory (realpath prefix), and the
    target must be a regular file under the 50 MB cap."""
    if os.path.islink(document_path):
        raise ValueError(
            "send_document refused: %r is a symlink" % document_path)
    allowed = _send_dirs()
    real = None
    for base in allowed:
        try:
            real = bridge_common.validate_inside(base, document_path)
            break
        except ValueError:
            continue
    if real is None:
        raise ValueError(
            "send_document refused: %r is outside the allowed "
            "directories %s" % (document_path, allowed))
    try:
        mode = os.lstat(real).st_mode
    except OSError as exc:
        raise ValueError(
            "send_document refused: cannot stat %r" % document_path
        ) from exc
    if not stat.S_ISREG(mode):
        raise ValueError(
            "send_document refused: %r is not a regular file"
            % document_path)
    if os.path.getsize(real) > MAX_DOCUMENT_BYTES:
        raise ValueError(
            "send_document refused: %d bytes exceeds the %d byte cap"
            % (os.path.getsize(real), MAX_DOCUMENT_BYTES))
    return real


def send_document(chat_id, document_path, caption=None,
                  parse_mode=None):
    """Send a document from the send/outbox allow-list (50 MB
    cap, regular files only, no symlinks). The outbox copy is
    deleted after a successful send so generated files do not
    linger. An HTML caption rejected for entity parsing retries
    once as plain stripped text."""
    if parse_mode not in (None, "HTML"):
        raise ValueError(f"unsupported parse_mode: {parse_mode!r}")
    real = _validate_document_path(document_path)
    fields = _caption_fields(chat_id, caption, parse_mode)
    try:
        result = call_multipart(
            "sendDocument", fields, "document", real,
            max_bytes=MAX_DOCUMENT_BYTES, require_image=False)
    except urllib.error.HTTPError as exc:
        if parse_mode != "HTML" or not caption \
                or not is_parse_entity_error(exc):
            raise
        plain_fields = _caption_fields(
            chat_id, strip_html_tags(caption))
        result = call_multipart(
            "sendDocument", plain_fields, "document", real,
            max_bytes=MAX_DOCUMENT_BYTES, require_image=False)
    if isinstance(result, dict) and result.get("ok"):
        try:
            os.unlink(real)
        except OSError:
            pass
    return result


def set_commands():
    """Register the bot command menu (setMyCommands), scoped to
    the owner's chat so the menu is not a public surface."""
    owner = _configured_owner_chat_id()
    if not owner:
        raise ValueError(
            "set_commands refused: no configured owner chat id")
    return call("setMyCommands", {
        "commands": BOT_COMMANDS,
        "scope": {"type": "chat", "chat_id": owner},
    })


def mark_handled(key, handled_path=None):
    """Mark one message handled (see bridge_common.append_handled).
    The path defaults to runtime/handled.txt (or
    TELEGRAM_HANDLED_FILE). Returns True when the key was newly
    added, False when it was already present."""
    path = handled_path or os.environ.get(
        "TELEGRAM_HANDLED_FILE",
        os.path.join(_bridge_runtime_dir(), "handled.txt"))
    return bridge_common.append_handled(path, key)


def mark_ack(key, ack_path=None):
    """Record that work on a message has started (see
    bridge_common.append_ack). While the ack is fresh the scan
    postpones redelivery. The path defaults to the handled
    file's ".ack" sibling (or TELEGRAM_ACK_FILE)."""
    if ack_path:
        path = ack_path
    elif os.environ.get("TELEGRAM_ACK_FILE"):
        path = os.environ["TELEGRAM_ACK_FILE"]
    else:
        handled = os.environ.get(
            "TELEGRAM_HANDLED_FILE",
            os.path.join(_bridge_runtime_dir(), "handled.txt"))
        path = handled + ".ack"
    return bridge_common.append_ack(path, key)


def edit_text(chat_id, message_id, text, markup=None,
              parse_mode=None, strip_urls_flag=False):
    """Edit a sent message. Editing to identical text is a
    Telegram error ('message is not modified'); it is treated as
    success here and reported with unchanged=True. Text over the
    4096-character limit is refused before any network call:
    the worker sends a new message instead of growing one edit
    past Telegram's limit. An HTML entity rejection retries once
    as stripped plain text."""
    if parse_mode not in (None, "HTML"):
        raise ValueError(f"unsupported parse_mode: {parse_mode!r}")
    if strip_urls_flag:
        text = strip_urls(text)
    send_value = text
    plain = text
    if parse_mode == "HTML":
        send_value = sanitize_html(text)
        plain = strip_html_tags(send_value)
    if len(plain) > bridge_common.MAX_MESSAGE_CHARS:
        raise ValueError(
            "edit_message refused: text exceeds the %d character "
            "limit; send a new message instead"
            % bridge_common.MAX_MESSAGE_CHARS)
    params = {"chat_id": chat_id, "message_id": message_id,
              "text": send_value,
              "link_preview_options": {"is_disabled": True}}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if markup:
        params["reply_markup"] = markup
    try:
        return call("editMessageText", params)
    except urllib.error.HTTPError as exc:
        body = _http_error_body(exc)
        if exc.code == 400 and "message is not modified" in body:
            return {"ok": True, "result": {"message_id": message_id,
                                           "unchanged": True}}
        if parse_mode == "HTML" and exc.code == 400 \
                and ("can't parse entities" in body.lower()
                     or "cannot parse entities" in body.lower()):
            plain_params = {"chat_id": chat_id,
                            "message_id": message_id,
                            "text": plain,
                            "link_preview_options": {
                                "is_disabled": True}}
            if markup:
                plain_params["reply_markup"] = markup
            return call("editMessageText", plain_params)
        raise


def _count_handled(handled_path):
    try:
        with open(handled_path, encoding="utf-8",
                  errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _read_scan_failure_count(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            parsed = bridge_common.parse_offset(f.read().strip())
            return parsed if parsed is not None else 0
    except OSError:
        return 0


def bridge_status(runtime_dir=None, now=None):
    """Local bridge health counts as a JSON-safe dict.

    Reads only runtime marker files and returns counts/ages:
    heartbeat age, cycles, errors, pending and dropped counts,
    handled count, scan-failure count, and inbox size. It never
    returns offsets, seen markers, message text, chat IDs, file
    contents, or credentials."""
    runtime = runtime_dir or _bridge_runtime_dir()
    current = time.time() if now is None else float(now)
    heartbeat = bridge_common.load_json_file(
        os.path.join(runtime, "poller-state.json")) or {}
    beat_at = bridge_common.parse_timestamp(heartbeat.get("beat_at"))
    heartbeat_age = None
    if beat_at is not None:
        heartbeat_age = max(0, int(current - beat_at))
    cycles = bridge_common.parse_offset(heartbeat.get("cycles"))
    errors = bridge_common.parse_offset(heartbeat.get("errors"))
    pending_data = bridge_common.load_json_file(
        os.path.join(runtime, "pending.json")) or {}
    entries = pending_data.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    pending_count = 0
    dropped_count = 0
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        pending_count += 1
        if entry.get("dropped"):
            dropped_count += 1
    inbox_path = os.path.join(runtime, "inbox.jsonl")
    try:
        inbox_bytes = os.path.getsize(inbox_path)
    except OSError:
        inbox_bytes = 0
    return {
        "heartbeat_age_secs": heartbeat_age,
        "poller_alive": heartbeat_age is not None
        and heartbeat_age <= 120,
        "cycles": cycles if cycles is not None else 0,
        "errors": errors if errors is not None else 0,
        "pending_count": pending_count,
        "dropped_count": dropped_count,
        "handled_count": _count_handled(
            os.path.join(runtime, "handled.txt")),
        "scan_failure_count": _read_scan_failure_count(
            os.path.join(runtime, "scan-failures.count")),
        "inbox_bytes": inbox_bytes,
    }


def _payload_hash(data):
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _runtime_file(name, runtime_dir=None, env_name=None):
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return os.path.join(runtime_dir or _bridge_runtime_dir(), name)


def _write_json_locked(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    lock_file = open(path + ".lock", "a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        bridge_common.atomic_write_json(path, data)
    finally:
        lock_file.close()


def _load_entries(path):
    data = bridge_common.load_json_file(path) or {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _gc_ui_entries(entries, now):
    kept = {}
    for entry_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        expires = bridge_common.parse_timestamp(entry.get("expires_at"))
        if expires is not None and expires <= now:
            continue
        kept[entry_id] = entry
    return kept


def _entry_bound(entry):
    """True when the entry is bound to the message carrying its
    buttons. Entries written before ui_bind existed have no
    bound flag; they always carried a message id, so a stored
    int id counts as bound for them."""
    if not isinstance(entry, dict):
        return False
    message_id = entry.get("message_id")
    if not isinstance(message_id, int) \
            or isinstance(message_id, bool):
        return False
    if "bound" in entry:
        return entry.get("bound") is True
    return True


def _ui_entry_valid(entry, kind, chat_id, message_id, payload_hash,
                    now, require_bound=False):
    if not isinstance(entry, dict) or entry.get("consumed"):
        return False
    expires = bridge_common.parse_timestamp(entry.get("expires_at"))
    if expires is None or expires <= now:
        return False
    if kind is not None and entry.get("kind") != kind:
        return False
    if chat_id is not None:
        parsed = _parse_any_chat_id(chat_id)
        if parsed is None or entry.get("chat_id") != parsed:
            return False
    if require_bound:
        # The callback must name the exact chat and message the
        # entry was bound to after its preview was sent. A
        # missing id or an unbound entry never validates:
        # omitting the binding must not silently bypass it.
        if chat_id is None:
            return False
        if not _entry_bound(entry):
            return False
        if not isinstance(message_id, int) \
                or isinstance(message_id, bool):
            return False
        if entry.get("message_id") != message_id:
            return False
    elif message_id is not None \
            and entry.get("message_id") != message_id:
        return False
    if payload_hash is not None \
            and entry.get("payload_hash") != payload_hash:
        return False
    return True


def ui_put(kind, data, chat_id, message_id=None, ttl_secs=1800,
           runtime_dir=None, now=None):
    """Store one short-lived UI-state entry and return it.

    The id is 128 random bits. The entry binds kind, chat,
    payload hash, expiry, and a consumed flag, so a callback
    can be validated and consumed exactly once.

    The message binding is two-phase: the buttons need the
    entry id, so the entry is created before the message that
    carries them exists. Confirm entries therefore start
    unbound and are bound with ui_bind once the preview send
    returns its message id; binding a confirm at creation
    time could only name some other message (for example the
    owner's source message), never the preview. Other kinds
    may still bind at creation when their message already
    exists."""
    if not isinstance(kind, str) or not kind:
        raise ValueError("ui_put refused: kind is required")
    parsed_chat = _parse_any_chat_id(chat_id)
    if parsed_chat is None:
        raise ValueError("ui_put refused: invalid chat binding")
    if message_id is not None and (
            not isinstance(message_id, int)
            or isinstance(message_id, bool) or message_id < 0):
        raise ValueError("ui_put refused: invalid message binding")
    if kind == "confirm" and message_id is not None:
        raise ValueError(
            "ui_put refused: confirm entries bind with ui_bind "
            "after the preview is sent")
    current = time.time() if now is None else float(now)
    entry_id = secrets.token_hex(16)
    entry = {
        "id": entry_id,
        "kind": kind,
        "chat_id": parsed_chat,
        "message_id": message_id,
        "bound": message_id is not None,
        "created_at": current,
        "expires_at": current + float(ttl_secs),
        "consumed": False,
        "payload_hash": _payload_hash(data),
        "data": data,
    }
    path = _runtime_file("ui-state.json", runtime_dir,
                         "TELEGRAM_UI_STATE_FILE")
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    lock_file = open(path + ".lock", "a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        entries = _gc_ui_entries(_load_entries(path), current)
        entries[entry_id] = entry
        bridge_common.atomic_write_json(path, {"entries": entries})
    finally:
        lock_file.close()
    return dict(entry)


def ui_bind(entry_id, message_id, chat_id=None, runtime_dir=None,
            now=None):
    """Bind one UI-state entry to the message carrying its
    buttons, and return the bound entry.

    Call this right after sending the preview message, with
    the message id that send returned. Binding is one-way: a
    live unbound entry binds once, rebinding to the same id
    is an idempotent no-op, and rebinding to a different id,
    or binding a missing, expired, or consumed entry, returns
    None and changes nothing."""
    if not isinstance(entry_id, str) or not entry_id.strip() \
            or any(ch.isspace() for ch in entry_id):
        raise ValueError("ui_bind refused: invalid entry id")
    if not isinstance(message_id, int) \
            or isinstance(message_id, bool) or message_id < 0:
        raise ValueError("ui_bind refused: invalid message binding")
    entry_id = entry_id.strip()
    current = time.time() if now is None else float(now)
    path = _runtime_file("ui-state.json", runtime_dir,
                         "TELEGRAM_UI_STATE_FILE")
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    lock_file = open(path + ".lock", "a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        entries = _gc_ui_entries(_load_entries(path), current)
        entry = entries.get(entry_id)
        if not isinstance(entry, dict) or entry.get("consumed"):
            bridge_common.atomic_write_json(path, {"entries": entries})
            return None
        if chat_id is not None:
            parsed = _parse_any_chat_id(chat_id)
            if parsed is None or entry.get("chat_id") != parsed:
                bridge_common.atomic_write_json(
                    path, {"entries": entries})
                return None
        if _entry_bound(entry):
            bridge_common.atomic_write_json(path, {"entries": entries})
            if entry.get("message_id") == message_id:
                return dict(entry)
            return None
        entry = dict(entry)
        entry["message_id"] = message_id
        entry["bound"] = True
        entry["bound_at"] = current
        entries[entry_id] = entry
        bridge_common.atomic_write_json(path, {"entries": entries})
        return dict(entry)
    finally:
        lock_file.close()


def ui_get(entry_id, kind=None, chat_id=None, message_id=None,
           payload_hash=None, runtime_dir=None, now=None):
    """Return a live UI-state entry copy, or None when it is
    missing, expired, consumed, or bound to another callback."""
    current = time.time() if now is None else float(now)
    path = _runtime_file("ui-state.json", runtime_dir,
                         "TELEGRAM_UI_STATE_FILE")
    entry = _load_entries(path).get(entry_id)
    if not _ui_entry_valid(entry, kind, chat_id, message_id,
                           payload_hash, current):
        return None
    return dict(entry)


def ui_consume(entry_id, kind=None, chat_id=None, message_id=None,
               payload_hash=None, runtime_dir=None, now=None):
    """Atomically consume one non-confirmation UI-state entry.
    The first valid consume returns the entry; replays,
    expired entries, and binding/hash mismatches return None.
    The entry must be bound, and the caller must name the
    callback's chat and message: a consume that omits the
    binding never validates. Confirmation entries are refused
    here entirely; they execute only through confirm_begin,
    which reserves the action ledger."""
    current = time.time() if now is None else float(now)
    path = _runtime_file("ui-state.json", runtime_dir,
                         "TELEGRAM_UI_STATE_FILE")
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    lock_file = open(path + ".lock", "a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        entries = _gc_ui_entries(_load_entries(path), current)
        entry = entries.get(entry_id)
        if isinstance(entry, dict) \
                and entry.get("kind") == "confirm":
            bridge_common.atomic_write_json(path, {"entries": entries})
            return None
        if chat_id is None or not _ui_entry_valid(
                entry, kind, chat_id, message_id, payload_hash,
                current, require_bound=True):
            bridge_common.atomic_write_json(path, {"entries": entries})
            return None
        entry = dict(entry)
        entry["consumed"] = True
        entry["consumed_at"] = current
        entries[entry_id] = entry
        bridge_common.atomic_write_json(path, {"entries": entries})
        return dict(entry)
    finally:
        lock_file.close()


READONLY_TURN_CLASSES = {"media_readonly", "data_readonly"}


def action_reserve(action_key, payload, runtime_dir=None, now=None,
                   turn_class=None):
    """Reserve one action key with its payload hash. Returns
    True only for the first reservation; duplicates return
    False so a confirmed write cannot execute twice. Read-only
    media/data turns can never reserve a write."""
    if turn_class in READONLY_TURN_CLASSES:
        raise ValueError(
            "action_reserve refused: read-only turn cannot write")
    key = str(action_key).strip()
    if not key or any(ch.isspace() for ch in key):
        raise ValueError("action_reserve refused: invalid key")
    current = time.time() if now is None else float(now)
    path = _runtime_file("action-ledger.json", runtime_dir,
                         "TELEGRAM_ACTION_LEDGER_FILE")
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    lock_file = open(path + ".lock", "a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        entries = _load_entries(path)
        if key in entries:
            return False
        entries[key] = {
            "payload_hash": _payload_hash(payload),
            "state": "reserved",
            "source": "action_reserve",
            "turn_class": turn_class,
            "created_at": current,
        }
        bridge_common.atomic_write_json(path, {"entries": entries})
        return True
    finally:
        lock_file.close()


def action_commit(action_key, payload_hash=None, payload=None,
                  runtime_dir=None, now=None, result="committed"):
    """Commit a reserved action with result committed or failed.
    Returns True only when a reserved entry with the matching
    payload hash transitions; missing, mismatched, or
    already-finished actions return False. A definite tool
    error commits failed so no reservation dangles."""
    if result not in ("committed", "failed"):
        raise ValueError(
            f"action_commit refused: bad result {result!r}")
    key = str(action_key).strip()
    if payload is not None:
        payload_hash = _payload_hash(payload)
    current = time.time() if now is None else float(now)
    path = _runtime_file("action-ledger.json", runtime_dir,
                         "TELEGRAM_ACTION_LEDGER_FILE")
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    lock_file = open(path + ".lock", "a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        entries = _load_entries(path)
        entry = entries.get(key)
        if not isinstance(entry, dict) \
                or entry.get("state") != "reserved":
            return False
        # Only a confirm_begin reservation can commit: typed
        # text claiming confirmation creates no execution, and
        # a commit without a confirm_begin reserve fails.
        if entry.get("source") != "confirm_begin" \
                or not key.startswith("confirm:"):
            return False
        if payload_hash is not None \
                and entry.get("payload_hash") != payload_hash:
            return False
        entry = dict(entry)
        entry["state"] = result
        entry["result"] = result
        entry["committed_at" if result == "committed"
              else "failed_at"] = current
        entries[key] = entry
        bridge_common.atomic_write_json(path, {"entries": entries})
        return True
    finally:
        lock_file.close()


def confirm_begin(entry_id, kind="confirm", chat_id=None,
                  message_id=None, payload_hash=None,
                  runtime_dir=None, now=None, turn_class=None):
    """Begin one confirmed write exactly once.

    Validates the UI entry (kind, chat/message binding, payload
    hash, expiry, not consumed), reserves the ledger action,
    marks the entry consumed, and returns the exact stored
    payload with its action_id and payload hash. Replays and
    hash mismatches return None. A pre-existing reservation
    returns begun=False with its state (reserved means the
    outcome is unknown and must not be re-executed). Locked
    mode and read-only media/data turns refuse without
    consuming the entry.

    The chat and message binding is mandatory: the entry must
    have been bound with ui_bind to the preview message, and
    the callback's chat and message ids must match it. An
    unbound entry, or a call that omits either id, returns
    None instead of silently skipping the binding check."""
    if not isinstance(entry_id, str) or not entry_id.strip() \
            or any(ch.isspace() for ch in entry_id):
        return None
    entry_id = entry_id.strip()
    action_id = f"confirm:{entry_id}"
    current = time.time() if now is None else float(now)
    ui_path = _runtime_file("ui-state.json", runtime_dir,
                            "TELEGRAM_UI_STATE_FILE")
    ledger_path = _runtime_file(
        "action-ledger.json", runtime_dir,
        "TELEGRAM_ACTION_LEDGER_FILE")
    for path in (ui_path, ledger_path):
        os.makedirs(os.path.dirname(os.path.abspath(path)),
                    exist_ok=True)
    ui_lock = open(ui_path + ".lock", "a", encoding="utf-8")
    try:
        fcntl.flock(ui_lock, fcntl.LOCK_EX)
        entries = _gc_ui_entries(_load_entries(ui_path), current)
        entry = entries.get(entry_id)
        payload = entry.get("data") if isinstance(entry, dict) \
            else None
        if not _ui_entry_valid(entry, kind, chat_id, message_id,
                               payload_hash, current,
                               require_bound=True) \
                or _payload_hash(payload) != entry.get(
                    "payload_hash"):
            bridge_common.atomic_write_json(
                ui_path, {"entries": entries})
            return None
        source_turn = turn_class
        if isinstance(payload, dict) and payload.get(
                "source_turn_class") in READONLY_TURN_CLASSES:
            source_turn = payload.get("source_turn_class")
        if source_turn in READONLY_TURN_CLASSES:
            bridge_common.atomic_write_json(
                ui_path, {"entries": entries})
            return {"begun": False, "reason": "media_readonly",
                    "entry_id": entry_id, "action_id": action_id}
        if settings_get(runtime_dir).get("locked"):
            bridge_common.atomic_write_json(
                ui_path, {"entries": entries})
            return {"begun": False, "reason": "locked",
                    "entry_id": entry_id, "action_id": action_id}
        ledger_lock = open(ledger_path + ".lock", "a",
                           encoding="utf-8")
        try:
            fcntl.flock(ledger_lock, fcntl.LOCK_EX)
            ledger = _load_entries(ledger_path)
            existing = ledger.get(action_id)
            consumed = dict(entry)
            consumed["consumed"] = True
            consumed["consumed_at"] = current
            entries[entry_id] = consumed
            if isinstance(existing, dict):
                bridge_common.atomic_write_json(
                    ui_path, {"entries": entries})
                state = existing.get("state", "reserved")
                return {"begun": False, "entry_id": entry_id,
                        "action_id": action_id,
                        "payload": payload,
                        "payload_hash": entry.get("payload_hash"),
                        "state": state,
                        "unknown": state == "reserved"}
            ledger[action_id] = {
                "payload_hash": entry.get("payload_hash"),
                "state": "reserved",
                "source": "confirm_begin",
                "entry_id": entry_id,
                "kind": entry.get("kind"),
                "turn_class": source_turn,
                "created_at": current,
            }
            # Ledger first: a crash after this write leaves a
            # visible reservation (unknown outcome), never a
            # silent duplicate execution.
            bridge_common.atomic_write_json(
                ledger_path, {"entries": ledger})
            bridge_common.atomic_write_json(
                ui_path, {"entries": entries})
            return {"begun": True, "entry_id": entry_id,
                    "action_id": action_id, "payload": payload,
                    "payload_hash": entry.get("payload_hash"),
                    "kind": entry.get("kind"),
                    "chat_id": entry.get("chat_id"),
                    "message_id": entry.get("message_id")}
        finally:
            ledger_lock.close()
    finally:
        ui_lock.close()


SETTINGS_MODES = {"short", "deep", "study", "code"}


def settings_get(runtime_dir=None):
    """Runtime settings with safe defaults (mode short,
    locked false). Invalid stored values fall back to defaults."""
    path = _runtime_file("settings.json", runtime_dir,
                         "TELEGRAM_SETTINGS_FILE")
    data = bridge_common.load_json_file(path) or {}
    mode = data.get("mode") if data.get("mode") in SETTINGS_MODES \
        else "short"
    locked = data.get("locked") if isinstance(
        data.get("locked"), bool) else False
    return {"mode": mode, "locked": locked}


def settings_set(mode=None, locked=None, runtime_dir=None):
    """Update runtime settings under a lock with an atomic
    write. Returns the stored settings."""
    current = settings_get(runtime_dir)
    if mode is not None:
        if mode not in SETTINGS_MODES:
            raise ValueError(f"settings_set refused: bad mode {mode!r}")
        current["mode"] = mode
    if locked is not None:
        if not isinstance(locked, bool):
            raise ValueError("settings_set refused: locked must be bool")
        current["locked"] = locked
    path = _runtime_file("settings.json", runtime_dir,
                         "TELEGRAM_SETTINGS_FILE")
    _write_json_locked(path, current)
    return dict(current)


def poller_running(lock_path=None):
    """True when another process holds the poller lock (i.e. the
    poller is running). Used to guard `getupdates`, which would
    otherwise confirm and steal updates before the poller sees
    them."""
    path = lock_path or os.environ.get(
        "TELEGRAM_POLLER_LOCK",
        os.path.join(_bridge_runtime_dir(), "poller.lock"))
    if not os.path.exists(path):
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Telegram Bot API helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("getme", help="Verify the bot token (getMe)")

    p_updates = sub.add_parser("getupdates", help="Poll for new updates")
    p_updates.add_argument("--offset", type=int, default=None)
    p_updates.add_argument("--timeout", type=int, default=0,
                           help="Long-poll seconds (0-50)")
    p_updates.add_argument("--force", action="store_true",
                           help="Run even while the poller holds its lock "
                                "(updates may be stolen from the poller)")

    p_send = sub.add_parser("send", help="Send a text message")
    p_send.add_argument("chat_id")
    p_send.add_argument("text", help="Message text, or - to read stdin")
    p_send.add_argument("--reply-to", type=int, default=None,
                        help="Reply to this message id")
    p_send.add_argument("--markup", default=None,
                        help="JSON reply_markup (e.g. inline keyboard)")
    p_send.add_argument("--menu", default=None, choices=sorted(MENUS),
                        help="Attach a predefined inline keyboard")
    p_send.add_argument("--parse-mode", default=None, choices=["HTML"],
                        help="Format text as Telegram HTML")
    p_send.add_argument("--protect-content", action="store_true",
                        help="Prevent forwarding/saving the message")
    p_send.add_argument("--strip-urls", action="store_true",
                        help="Remove bare URLs from the text")

    p_photo = sub.add_parser("send_photo", help="Send a photo from a file")
    p_photo.add_argument("chat_id")
    p_photo.add_argument("photo_path")
    p_photo.add_argument("caption", nargs="?", default=None)
    p_photo.add_argument("--parse-mode", default=None, choices=["HTML"],
                         help="Format the caption as Telegram HTML")

    p_doc = sub.add_parser("send_document",
                           help="Send a document from a file")
    p_doc.add_argument("chat_id")
    p_doc.add_argument("document_path")
    p_doc.add_argument("caption", nargs="?", default=None)
    p_doc.add_argument("--parse-mode", default=None, choices=["HTML"],
                       help="Format the caption as Telegram HTML")

    p_action = sub.add_parser(
        "chat_action",
        help="Show a chat action such as typing (lasts about 5 seconds)")
    p_action.add_argument("chat_id")
    p_action.add_argument("action", nargs="?", default="typing",
                          help="typing, upload_photo, record_video, ...")

    p_react = sub.add_parser("react", help="React to a message with an emoji")
    p_react.add_argument("chat_id")
    p_react.add_argument("message_id", type=int)
    p_react.add_argument("emoji")

    p_cb = sub.add_parser("answer_callback",
                          help="Answer an inline button tap")
    p_cb.add_argument("callback_query_id")
    p_cb.add_argument("text", nargs="?", default=None,
                      help="Short notice shown to the user")

    p_getfile = sub.add_parser("get_file", help="Resolve a file_id to a path")
    p_getfile.add_argument("file_id")

    p_dl = sub.add_parser("download", help="Download a file by file_id")
    p_dl.add_argument("file_id")
    p_dl.add_argument("dest_path",
                      help="Destination directory (safe name generated) "
                           "or file path inside the download dir")
    p_dl.add_argument("--max-bytes", type=int,
                      default=bridge_common.MAX_DOWNLOAD_BYTES,
                      help="Abort above this many bytes")

    p_edit = sub.add_parser("edit_message", help="Edit a sent message")
    p_edit.add_argument("chat_id")
    p_edit.add_argument("message_id", type=int)
    p_edit.add_argument("text")
    p_edit.add_argument("--markup", default=None,
                        help="JSON reply_markup (e.g. inline keyboard)")
    p_edit.add_argument("--parse-mode", default=None, choices=["HTML"],
                        help="Format text as Telegram HTML")
    p_edit.add_argument("--strip-urls", action="store_true",
                        help="Remove bare URLs from the text")

    p_handled = sub.add_parser(
        "mark_handled",
        help="Mark a message handled (chat_id:message_id, or "
             "chat_id:cb:callback_query_id for a button tap)")
    p_handled.add_argument("key", help="Handled key, e.g. 123:456")
    p_handled.add_argument("--handled-file", default=None,
                           help="handled.txt path (default: "
                                "runtime/handled.txt)")

    p_ack = sub.add_parser(
        "ack",
        help="Mark work started on a message (postpones "
             "redelivery while the ack is fresh)")
    p_ack.add_argument("key", help="Handled key, e.g. 123:456")
    p_ack.add_argument("--ack-file", default=None,
                       help="ack file path (default: handled.txt.ack)")

    sub.add_parser("set_commands",
                   help="Register the bot command menu")

    p_status = sub.add_parser(
        "bridge_status",
        help="Print local bridge health counts as JSON")
    p_status.add_argument("--runtime-dir", default=None,
                          help="Bridge runtime directory")

    p_ui_put = sub.add_parser("ui_put", help="Store UI state")
    p_ui_put.add_argument("--kind", required=True)
    p_ui_put.add_argument("--chat-id", required=True)
    p_ui_put.add_argument("--message-id", type=int, default=None,
                          help="Bind now to an existing message "
                               "(not allowed for confirm entries; "
                               "they bind with ui_bind)")
    p_ui_put.add_argument("--data-json", required=True)
    p_ui_put.add_argument("--ttl-secs", type=float, default=1800)
    p_ui_put.add_argument("--runtime-dir", default=None)

    p_ui_bind = sub.add_parser(
        "ui_bind", help="Bind UI state to its sent message")
    p_ui_bind.add_argument("entry_id")
    p_ui_bind.add_argument("--message-id", type=int, required=True,
                           help="Message id the preview send returned")
    p_ui_bind.add_argument("--chat-id", default=None)
    p_ui_bind.add_argument("--runtime-dir", default=None)

    p_ui_consume = sub.add_parser("ui_consume",
                                  help="Consume UI state once")
    p_ui_consume.add_argument("entry_id")
    p_ui_consume.add_argument("--kind", default=None)
    p_ui_consume.add_argument("--chat-id", default=None)
    p_ui_consume.add_argument("--message-id", type=int, default=None)
    p_ui_consume.add_argument("--payload-hash", default=None)
    p_ui_consume.add_argument("--runtime-dir", default=None)

    p_reserve = sub.add_parser("action_reserve",
                               help="Reserve an action once")
    p_reserve.add_argument("action_key")
    p_reserve.add_argument("--payload-json", required=True)
    p_reserve.add_argument("--turn-class", default=None)
    p_reserve.add_argument("--runtime-dir", default=None)

    p_confirm = sub.add_parser(
        "confirm_begin",
        help="Consume a confirmation and reserve its action")
    p_confirm.add_argument("entry_id")
    p_confirm.add_argument("--kind", default="confirm")
    p_confirm.add_argument("--chat-id", default=None)
    p_confirm.add_argument("--message-id", type=int, default=None)
    p_confirm.add_argument("--payload-hash", default=None)
    p_confirm.add_argument("--turn-class", default=None)
    p_confirm.add_argument("--runtime-dir", default=None)

    p_commit = sub.add_parser("action_commit",
                              help="Commit a reserved action")
    p_commit.add_argument("action_key")
    p_commit.add_argument("--payload-hash", default=None)
    p_commit.add_argument("--result", default="committed",
                          choices=["committed", "failed"])
    p_commit.add_argument("--runtime-dir", default=None)

    p_settings_get = sub.add_parser("settings_get",
                                    help="Read bridge settings")
    p_settings_get.add_argument("--runtime-dir", default=None)

    p_settings_set = sub.add_parser("settings_set",
                                    help="Update bridge settings")
    p_settings_set.add_argument("--mode", default=None,
                                choices=sorted(SETTINGS_MODES))
    p_settings_set.add_argument("--locked", default=None,
                                choices=["true", "false"])
    p_settings_set.add_argument("--runtime-dir", default=None)

    p_setwh = sub.add_parser("setwebhook", help="Register a webhook URL")
    p_setwh.add_argument("url")
    p_setwh.add_argument("--secret", default=None,
                         help="Secret token Telegram sends in a header")

    p_delwh = sub.add_parser("deletewebhook", help="Remove the webhook")
    p_delwh.add_argument("--drop-pending", action="store_true",
                         help="Drop pending updates instead of delivering them")

    sub.add_parser("getwebhookinfo", help="Show webhook status")

    args = parser.parse_args(argv)

    try:
        if args.command == "getme":
            result = call("getMe")
        elif args.command == "getupdates":
            if poller_running() and not args.force:
                print("refusing getupdates: the poller is running and "
                      "holds its lock; this call would confirm and steal "
                      "updates before the poller sees them. Pass --force "
                      "to run it anyway.", file=sys.stderr)
                return 2
            params = {}
            if args.offset is not None:
                params["offset"] = args.offset
            if args.timeout:
                params["timeout"] = args.timeout
            result = call("getUpdates", params or None,
                          timeout=60 if args.timeout else 30)
        elif args.command == "send":
            require_allowed_chat(args.chat_id, "send")
            text = args.text
            if text == "-":
                text = sys.stdin.read()
            markup = None
            if args.menu:
                markup = MENUS[args.menu]
            elif args.markup:
                markup = json.loads(args.markup)
            try:
                results = send_text(args.chat_id, text,
                                    reply_to=args.reply_to, markup=markup,
                                    parse_mode=args.parse_mode,
                                    protect_content=args.protect_content,
                                    strip_urls_flag=args.strip_urls)
            except PartialSendError as exc:
                # Some chunks went out: report exactly how many,
                # so the caller resumes after them instead of
                # duplicating the whole message.
                print(json.dumps({
                    "ok": False,
                    "error": str(exc),
                    "parts_sent": exc.parts_sent,
                    "parts_total": exc.parts_total,
                    "result": [r.get("result") for r in exc.results],
                }, ensure_ascii=False, indent=2))
                return 1
            result = results[0] if len(results) == 1 else {
                "ok": all(r.get("ok") for r in results),
                "result": [r.get("result") for r in results],
                "parts": len(results),
                "parts_sent": len(results),
            }
        elif args.command == "send_photo":
            require_allowed_chat(args.chat_id, "send_photo")
            result = send_photo(args.chat_id, args.photo_path,
                                caption=args.caption,
                                parse_mode=args.parse_mode)
        elif args.command == "send_document":
            require_allowed_chat(args.chat_id, "send_document")
            result = send_document(args.chat_id, args.document_path,
                                   caption=args.caption,
                                   parse_mode=args.parse_mode)
        elif args.command == "chat_action":
            require_allowed_chat(args.chat_id, "chat_action")
            result = call("sendChatAction",
                          {"chat_id": args.chat_id, "action": args.action})
        elif args.command == "react":
            require_allowed_chat(args.chat_id, "react")
            result = call("setMessageReaction", {
                "chat_id": args.chat_id,
                "message_id": args.message_id,
                "reaction": [{"type": "emoji", "emoji": args.emoji}],
            })
        elif args.command == "answer_callback":
            params = {"callback_query_id": args.callback_query_id}
            if args.text:
                params["text"] = args.text
            result = call("answerCallbackQuery", params)
        elif args.command == "get_file":
            result = call("getFile", {"file_id": args.file_id})
        elif args.command == "download":
            final, total = download_file(
                args.file_id, args.dest_path, max_bytes=args.max_bytes)
            result = {"ok": True, "result": {"path": final,
                                             "bytes": total}}
        elif args.command == "edit_message":
            require_allowed_chat(args.chat_id, "edit_message")
            markup = json.loads(args.markup) if args.markup else None
            result = edit_text(args.chat_id, args.message_id, args.text,
                               markup=markup,
                               parse_mode=args.parse_mode,
                               strip_urls_flag=args.strip_urls)
        elif args.command == "mark_handled":
            added = mark_handled(args.key,
                                 handled_path=args.handled_file)
            result = {"ok": True, "result": {"key": args.key,
                                             "added": added}}
        elif args.command == "ack":
            mark_ack(args.key, ack_path=args.ack_file)
            result = {"ok": True, "result": {"key": args.key,
                                             "acked": True}}
        elif args.command == "set_commands":
            result = set_commands()
        elif args.command == "bridge_status":
            result = {"ok": True,
                      "result": bridge_status(args.runtime_dir)}
        elif args.command == "ui_put":
            entry = ui_put(args.kind, json.loads(args.data_json),
                           args.chat_id, args.message_id,
                           ttl_secs=args.ttl_secs,
                           runtime_dir=args.runtime_dir)
            result = {"ok": True, "result": {
                "id": entry["id"],
                "payload_hash": entry["payload_hash"],
                "expires_at": entry["expires_at"],
                "bound": entry["bound"],
            }}
        elif args.command == "ui_bind":
            entry = ui_bind(args.entry_id, args.message_id,
                            chat_id=args.chat_id,
                            runtime_dir=args.runtime_dir)
            result = {"ok": entry is not None, "result": entry}
        elif args.command == "ui_consume":
            entry = ui_consume(args.entry_id, kind=args.kind,
                               chat_id=args.chat_id,
                               message_id=args.message_id,
                               payload_hash=args.payload_hash,
                               runtime_dir=args.runtime_dir)
            result = {"ok": entry is not None, "result": entry}
        elif args.command == "action_reserve":
            reserved = action_reserve(
                args.action_key, json.loads(args.payload_json),
                runtime_dir=args.runtime_dir,
                turn_class=args.turn_class)
            result = {"ok": True, "result": {"reserved": reserved}}
        elif args.command == "confirm_begin":
            begun = confirm_begin(
                args.entry_id, kind=args.kind, chat_id=args.chat_id,
                message_id=args.message_id,
                payload_hash=args.payload_hash,
                runtime_dir=args.runtime_dir,
                turn_class=args.turn_class)
            result = {"ok": bool(begun and begun.get("begun")),
                      "result": begun}
        elif args.command == "action_commit":
            committed = action_commit(
                args.action_key, payload_hash=args.payload_hash,
                runtime_dir=args.runtime_dir, result=args.result)
            result = {"ok": True, "result": {"committed": committed}}
        elif args.command == "settings_get":
            result = {"ok": True,
                      "result": settings_get(args.runtime_dir)}
        elif args.command == "settings_set":
            locked = None
            if args.locked is not None:
                locked = args.locked == "true"
            result = {"ok": True, "result": settings_set(
                mode=args.mode, locked=locked,
                runtime_dir=args.runtime_dir)}
        elif args.command == "setwebhook":
            params = {"url": args.url}
            if args.secret:
                params["secret_token"] = args.secret
            result = call("setWebhook", params)
        elif args.command == "deletewebhook":
            params = {}
            if args.drop_pending:
                params["drop_pending_updates"] = True
            result = call("deleteWebhook", params or None)
        elif args.command == "getwebhookinfo":
            result = call("getWebhookInfo")
    except DynamicCredentialError as exc:
        print(f"credential error: {exc}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - body unreadable
            body = ""
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - no tracebacks to the user
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
