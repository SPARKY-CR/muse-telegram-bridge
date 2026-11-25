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
                                      [--menu NAME]
  telegram.py send_photo <chat_id> <photo_path> [caption]
  telegram.py chat_action <chat_id> [action]
  telegram.py react <chat_id> <message_id> <emoji>
  telegram.py answer_callback <callback_query_id> [text]
  telegram.py get_file <file_id>
  telegram.py download <file_id> <dest_dir_or_path> [--max-bytes N]
  telegram.py edit_message <chat_id> <message_id> <text>
  telegram.py mark_handled <chat_id:message_id|chat_id:cb:callback_id>
  telegram.py ack <chat_id:message_id|chat_id:cb:callback_id>
  telegram.py setwebhook <url> [--secret TOKEN]
  telegram.py deletewebhook [--drop-pending]
  telegram.py getwebhookinfo
"""

import argparse
import fcntl
import json
import mimetypes
import os
import sys
import tempfile
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

# Predefined inline keyboards, so callers never hand-write JSON
# (and never hit shell-quoting bugs like an apostrophe in a label).
MENUS = {
    "main": {"inline_keyboard": [
        [{"text": "📅 Schedule", "callback_data": "cmd:schedule"},
         {"text": "📊 Bot status", "callback_data": "cmd:status"}],
        [{"text": "❓ Help", "callback_data": "cmd:help"}],
    ]},
}


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


def call_multipart(method, fields, file_field, file_path, timeout=120):
    """Call a Bot API method with a file upload (multipart/form-data).

    fields is a dict of plain form fields; file_field is the name of
    the file part (e.g. "photo") and file_path the local file to send.

    The file must be an image inside an allow-listed directory and
    under the size cap, so a prompt-injected agent cannot upload
    arbitrary files from anywhere on disk. The file name is escaped
    before it goes into the multipart header.
    """
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
            "send_photo refused: %r is outside the allowed directories %s"
            % (file_path, allowed))
    if not bridge_common.is_image_file(real):
        raise ValueError(
            "send_photo refused: %r is not an image file" % file_path)
    size = os.path.getsize(real)
    if size > bridge_common.MAX_PHOTO_BYTES:
        raise ValueError(
            "send_photo refused: %d bytes exceeds the %d byte cap"
            % (size, bridge_common.MAX_PHOTO_BYTES))

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


def send_text(chat_id, text, reply_to=None, markup=None):
    """Send text, splitting it at the 4096-character limit. Only
    the first chunk carries the reply reference. Returns the list
    of sendMessage results. If a chunk after the first fails, a
    PartialSendError is raised (the earlier chunks already went
    out); a failure of the first chunk raises the original error,
    since nothing was delivered."""
    chunks = bridge_common.split_text(text)
    results = []
    for i, chunk in enumerate(chunks):
        params = {"chat_id": chat_id, "text": chunk}
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


def edit_text(chat_id, message_id, text, markup=None):
    """Edit a sent message. Editing to identical text is a
    Telegram error ('message is not modified'); it is treated as
    success here and reported with unchanged=True."""
    params = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if markup:
        params["reply_markup"] = markup
    try:
        return call("editMessageText", params)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - fall through to re-raise
            body = ""
        if exc.code == 400 and "message is not modified" in body:
            return {"ok": True, "result": {"message_id": message_id,
                                           "unchanged": True}}
        raise


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

    p_photo = sub.add_parser("send_photo", help="Send a photo from a file")
    p_photo.add_argument("chat_id")
    p_photo.add_argument("photo_path")
    p_photo.add_argument("caption", nargs="?", default=None)

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
                                    reply_to=args.reply_to, markup=markup)
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
            fields = {"chat_id": args.chat_id}
            if args.caption:
                fields["caption"] = args.caption
            result = call_multipart("sendPhoto", fields, "photo",
                                    args.photo_path)
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
                               markup=markup)
        elif args.command == "mark_handled":
            added = mark_handled(args.key,
                                 handled_path=args.handled_file)
            result = {"ok": True, "result": {"key": args.key,
                                             "added": added}}
        elif args.command == "ack":
            mark_ack(args.key, ack_path=args.ack_file)
            result = {"ok": True, "result": {"key": args.key,
                                             "acked": True}}
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
