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

Usage:
  telegram.py getme
  telegram.py getupdates [--offset N] [--timeout SECONDS]
  telegram.py send <chat_id> <text>
  telegram.py send_photo <chat_id> <photo_path> [caption]
  telegram.py chat_action <chat_id> [action]
  telegram.py react <chat_id> <message_id> <emoji>
  telegram.py answer_callback <callback_query_id> [text]
  telegram.py get_file <file_id>
  telegram.py download <file_id> <dest_path>
  telegram.py edit_message <chat_id> <message_id> <text>
  telegram.py setwebhook <url> [--secret TOKEN]
  telegram.py deletewebhook [--drop-pending]
  telegram.py getwebhookinfo
"""

import argparse
import json
import os
import sys
import urllib.request

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
    host = urllib.request.urlparse(url).hostname
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(f"refusing non-Telegram host: {host}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


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
    with urllib.request.build_opener(NoRedirect()).open(
        request, timeout=timeout
    ) as response:
        return read_json_response(response)


def call_multipart(method, fields, file_field, file_path, timeout=120):
    """Call a Bot API method with a file upload (multipart/form-data).

    fields is a dict of plain form fields; file_field is the name of
    the file part (e.g. "photo") and file_path the local file to send.
    """
    import mimetypes
    import uuid

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
    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(file_path)
    body += f"--{boundary}\r\n".encode("utf-8")
    body += (f'Content-Disposition: form-data; name="{file_field}"; '
             f'filename="{filename}"\r\n').encode("utf-8")
    body += (f"Content-Type: {mime or 'application/octet-stream'}\r\n\r\n"
             ).encode("utf-8")
    with open(file_path, "rb") as f:
        body += f.read()
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        url, data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.build_opener(NoRedirect()).open(
        request, timeout=timeout
    ) as response:
        return read_json_response(response)


def download_file(file_id, dest_path, timeout=120):
    """Download a Telegram file by its file_id to a local path.

    Resolves the file path with getFile, then downloads the bytes
    from the file endpoint (same raw-surrogate URL rule as API calls).
    Returns the downloaded byte count.
    """
    info = call("getFile", {"file_id": file_id})
    if not info.get("ok"):
        raise RuntimeError(f"getFile failed: {info}")
    file_path = info["result"].get("file_path")
    if not file_path:
        raise RuntimeError(f"getFile returned no path: {info}")
    token = get_token()
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    check_url(url)
    request = urllib.request.Request(url, method="GET")
    total = 0
    with urllib.request.build_opener(NoRedirect()).open(
        request, timeout=timeout
    ) as response:
        with open(dest_path, "wb") as f:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description="Telegram Bot API helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("getme", help="Verify the bot token (getMe)")

    p_updates = sub.add_parser("getupdates", help="Poll for new updates")
    p_updates.add_argument("--offset", type=int, default=None)
    p_updates.add_argument("--timeout", type=int, default=0,
                           help="Long-poll seconds (0-50)")

    p_send = sub.add_parser("send", help="Send a text message")
    p_send.add_argument("chat_id")
    p_send.add_argument("text")
    p_send.add_argument("--reply-to", type=int, default=None,
                        help="Reply to this message id")
    p_send.add_argument("--markup", default=None,
                        help="JSON reply_markup (e.g. inline keyboard)")

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
    p_dl.add_argument("dest_path")

    p_edit = sub.add_parser("edit_message", help="Edit a sent message")
    p_edit.add_argument("chat_id")
    p_edit.add_argument("message_id", type=int)
    p_edit.add_argument("text")
    p_edit.add_argument("--markup", default=None,
                        help="JSON reply_markup (e.g. inline keyboard)")

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
            params = {}
            if args.offset is not None:
                params["offset"] = args.offset
            if args.timeout:
                params["timeout"] = args.timeout
            result = call("getUpdates", params or None,
                          timeout=60 if args.timeout else 30)
        elif args.command == "send":
            params = {"chat_id": args.chat_id, "text": args.text}
            if args.reply_to is not None:
                params["reply_to_message_id"] = args.reply_to
            if args.markup:
                params["reply_markup"] = json.loads(args.markup)
            result = call("sendMessage", params)
        elif args.command == "send_photo":
            fields = {"chat_id": args.chat_id}
            if args.caption:
                fields["caption"] = args.caption
            result = call_multipart("sendPhoto", fields, "photo",
                                    args.photo_path)
        elif args.command == "chat_action":
            result = call("sendChatAction",
                          {"chat_id": args.chat_id, "action": args.action})
        elif args.command == "react":
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
            total = download_file(args.file_id, args.dest_path)
            result = {"ok": True, "result": {"path": args.dest_path,
                                             "bytes": total}}
        elif args.command == "edit_message":
            params = {"chat_id": args.chat_id,
                      "message_id": args.message_id,
                      "text": args.text}
            if args.markup:
                params["reply_markup"] = json.loads(args.markup)
            result = call("editMessageText", params)
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
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
