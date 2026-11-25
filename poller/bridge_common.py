#!/usr/bin/env python3
"""Shared helpers for the Muse-Telegram bridge.

Single source of truth for update classification, safe file paths,
token redaction, offset parsing, and atomic JSON writes. Used by
poller.py, scan_inbox.py, and telegram.py so every component agrees
on what a message is, who the owner is, and where files may go.

This module has no Telegram or Muse dependencies and no side
effects on import, so the test suite can import it directly.
"""

import hashlib
import json
import os
import re
import time

# --- Limits -----------------------------------------------------------
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024   # 50 MB per downloaded file
MAX_PHOTO_BYTES = 10 * 1024 * 1024      # 10 MB per sent photo
MAX_MESSAGE_CHARS = 4096                # Telegram sendMessage limit
MAX_INBOX_LINES = 2000                  # compact the inbox above this
MAX_HANDLED_LINES = 1000                # prune handled.txt to this
MAX_MESSAGES_PER_WAKE = 20              # cap on one wake payload
MAX_MESSAGE_AGE_SECS = 24 * 3600        # ignore older messages when
                                        # recovering from a lost seen marker
MAX_RETRY_ATTEMPTS = 4                  # re-wakes before a pending
                                        # message is dropped
PENDING_TTL_SECS = 24 * 3600            # drop pending older than this
ACK_TTL_SECS = 30 * 60                  # an agent ack postpones
                                        # redelivery for this long
MAX_RECENT_UPDATE_IDS = 2000            # recent ids kept in seen.json
                                        # for epoch-reset handling
OFFSET_SILENCE_DROP_SECS = 7 * 24 * 3600  # after this long with no
                                        # updates, drop a stale poll
                                        # offset once (defensive
                                        # epoch-reset mitigation)
LOCK_STALE_SECS = 24 * 3600             # remove orphaned lock files
                                        # older than this
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
GROUP_CHAT_TYPES = {"group", "supergroup"}

# Magic bytes for the image formats send_photo accepts. The
# extension is only a first filter; the bytes decide, so a text
# file renamed to .png can never be uploaded as a photo.
IMAGE_MAGIC = (
    (b"\xff\xd8\xff", None),                 # JPEG
    (b"\x89PNG\r\n\x1a\n", None),            # PNG
    (b"GIF87a", None),                       # GIF87a
    (b"GIF89a", None),                       # GIF89a
    (b"RIFF", b"WEBP"),                      # WebP (WEBP at offset 8)
)

# Message kinds the bridge handles. Anything else a message carries
# (sticker, video, location, ...) is reported as "unsupported" so the
# agent can reply "can't handle that yet" instead of staying silent.
SUPPORTED_KINDS = ("message", "photo", "document", "voice", "callback")

# Keys that mark a message as an unsupported type, in the order they
# are reported. (text/photo/document/voice are handled above.)
UNSUPPORTED_KEYS = (
    "sticker", "video", "video_note", "audio", "animation",
    "location", "venue", "contact", "poll", "dice", "game",
    "invoice", "successful_payment", "passport_data",
)

# Deterministic command gate table (v9). The scan parses typed
# slash commands against this table plus settings, so unknown
# and locked-blocked commands never reach tool use.
READ_ONLY_COMMANDS = {
    "start", "menu", "help", "status", "health", "today",
    "schedule", "study", "goals", "goal", "chats", "chat",
    "search", "tasks", "reminders", "privacy",
}
CONFIRM_COMMANDS = {
    "remind", "note", "progress", "done", "cancelreminder",
}
EPHEMERAL_COMMANDS = {"quiz"}
SETTINGS_COMMANDS = {"lock", "mode", "unlock"}
KNOWN_COMMANDS = (READ_ONLY_COMMANDS | CONFIRM_COMMANDS
                  | EPHEMERAL_COMMANDS | SETTINGS_COMMANDS)


def _gate_class(command):
    if command in READ_ONLY_COMMANDS:
        return "read_only"
    if command in CONFIRM_COMMANDS:
        return "confirm"
    if command in EPHEMERAL_COMMANDS:
        return "ephemeral"
    if command in SETTINGS_COMMANDS:
        return "settings"
    return "unknown"


def parse_typed_command(text, locked=False):
    """Parse a typed slash command against the gate table.

    Returns (parsed_command, args, command_class, gate_class).
    Only call this for typed_text payloads; captions, forwards,
    transcripts, and documents are data and never reach here.
    Locked mode is a write-pause: read-only commands and typed
    /unlock still run, writes/settings/quiz are blocked."""
    if not isinstance(text, str) or not text.startswith("/"):
        return None, "", "message", "message"
    parts = text[1:].split(None, 1)
    raw = parts[0] if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""
    command = raw.split("@", 1)[0].lower()
    if not command or not re.fullmatch(r"[a-z0-9_]+", command):
        return command, args, "command_unknown", "unknown"
    if command not in KNOWN_COMMANDS:
        return command, args, "command_unknown", "unknown"
    gate = _gate_class(command)
    if locked and gate != "read_only" and command != "unlock":
        return command, args, "command_blocked", "blocked"
    return command, args, "command", gate


def apply_command_gate(payload, locked=False):
    """Stamp parsed_command/args/command_class on a typed-text
    payload in place. Non-command payloads are left untouched."""
    if not isinstance(payload, dict):
        return payload
    if payload.get("origin") != "typed_text" \
            or payload.get("kind") != "message":
        return payload
    text = payload.get("text")
    if not isinstance(text, str) or not text.startswith("/"):
        payload["command_class"] = "message"
        return payload
    command, args, command_class, gate = parse_typed_command(
        text, locked=locked)
    payload["parsed_command"] = command
    payload["args"] = args
    payload["command_class"] = command_class
    payload["gate_class"] = gate
    return payload


def _dict(value):
    """Return value when it is a dict, else an empty dict. Every
    nested Telegram object goes through this, so a valid-JSON
    update with the wrong shape (a list where an object belongs)
    classifies as not-owner/unsupported instead of crashing the
    scan or the poller."""
    return value if isinstance(value, dict) else {}


def _chat_target(chat, owner_id, allow_groups, missing_ok=False):
    """Validate a chat object for the owner. Returns
    (chat_id, chat_type), or (None, None) when the chat is not
    allowed. Private chats must be the owner's own chat (when an
    id is present); group/supergroup chats are allowed only with
    allow_groups and only ever for the owner sender (checked by
    the caller). Channel chats are never bridge chats."""
    chat = _dict(chat)
    chat_type = chat.get("type")
    raw_id = chat.get("id")
    chat_id = raw_id if isinstance(raw_id, int) \
        and not isinstance(raw_id, bool) else None
    if chat_type == "private":
        if chat_id is not None and chat_id != owner_id:
            return None, None
        return owner_id, "private"
    if chat_type in GROUP_CHAT_TYPES:
        if not allow_groups or chat_id is None:
            return None, None
        return chat_id, chat_type
    if chat_type is None and missing_ok:
        # Inline callbacks can carry no message/chat at all; the
        # sender check has already passed, so treat them as the
        # owner's private chat (the pre-v5 behaviour).
        if chat_id is None or chat_id == owner_id:
            return owner_id, "private"
    return None, None


def classify(update, owner_id, allow_groups=False):
    """Classify one Telegram update for the owner.

    Returns (kind, payload) where kind is one of "message", "photo",
    "document", "voice", "callback", or "unsupported". Returns
    (None, None) when the update is not from the owner (strangers,
    other chats, and — unless allow_groups is set — group chats).

    The owner check uses the SENDER (from.id), not chat.id, so a
    group member cannot pass by sharing the bot's chat. Private
    chats are always allowed; group/supergroup chats only with
    allow_groups, and callbacks check their chat type too. The
    payload carries chat_id/chat_type (the reply target), so
    group replies, handled keys, and the typing keeper all aim
    at the chat the message actually came from.
    """
    if not isinstance(update, dict):
        return None, None
    uid = update.get("update_id")
    if not isinstance(uid, int) or isinstance(uid, bool):
        return None, None

    cb = update.get("callback_query")
    if isinstance(cb, dict):
        sender = _dict(cb.get("from"))
        if sender.get("id") != owner_id:
            return None, None
        data = cb.get("data")
        if not isinstance(data, str) or not data:
            return None, None
        cb_msg = _dict(cb.get("message"))
        chat_id, chat_type = _chat_target(
            cb_msg.get("chat"), owner_id, allow_groups,
            missing_ok="chat" not in cb_msg)
        if chat_id is None:
            return None, None
        cb_id = cb.get("id", "")
        return "callback", {
            "kind": "callback", "update_id": uid,
            "callback_query_id": cb_id if isinstance(cb_id, str) else "",
            "data": data,
            "message_id": cb_msg.get("message_id", 0),
            "date": cb_msg.get("date", 0),
            "chat_id": chat_id, "chat_type": chat_type,
            "origin": "callback", "command_class": "callback",
            "turn_class": "callback",
        }

    msg = update.get("message")
    if not isinstance(msg, dict):
        return None, None
    sender = _dict(msg.get("from"))
    if sender.get("id") != owner_id:
        return None, None
    chat_id, chat_type = _chat_target(
        msg.get("chat"), owner_id, allow_groups)
    if chat_id is None:
        return None, None

    message_id = msg.get("message_id", 0)
    date = msg.get("date", 0)
    base = {"update_id": uid, "message_id": message_id, "date": date,
            "chat_id": chat_id, "chat_type": chat_type}
    forwarded = any(key in msg for key in (
        "forward_origin", "forward_from", "forward_from_chat",
        "forward_sender_name", "forward_date", "forward_signature"))

    text = msg.get("text")
    if isinstance(text, str) and text:
        if forwarded:
            return "message", dict(
                base, kind="message", text=text, origin="forwarded",
                command_class="data", turn_class="data_readonly")
        payload = dict(base, kind="message", text=text,
                       origin="typed_text", command_class="message",
                       turn_class="typed")
        # The poller stamps origin at claim time; the scan
        # re-applies the gate with live settings (locked mode).
        return "message", apply_command_gate(payload)
    photo = msg.get("photo")
    if isinstance(photo, list) and photo:
        sizes = [p for p in photo if isinstance(p, dict)]
        if sizes:
            largest = sizes[-1]
            caption = msg.get("caption", "")
            caption = caption if isinstance(caption, str) else ""
            return "photo", dict(
                base, kind="photo",
                file_id=largest.get("file_id", ""),
                caption=caption,
                origin="caption" if caption else "document_content",
                command_class="data", turn_class="media_readonly")
    document = _dict(msg.get("document"))
    if document:
        caption = msg.get("caption", "")
        caption = caption if isinstance(caption, str) else ""
        return "document", dict(
            base, kind="document",
            file_id=document.get("file_id", ""),
            file_name=document.get("file_name", ""),
            mime_type=document.get("mime_type", ""),
            file_size=document.get("file_size", 0),
            caption=caption,
            origin="caption" if caption else "document_content",
            command_class="data", turn_class="media_readonly")
    voice = _dict(msg.get("voice"))
    if voice:
        return "voice", dict(
            base, kind="voice",
            file_id=voice.get("file_id", ""),
            duration=voice.get("duration", 0),
            origin="voice", command_class="data",
            turn_class="media_readonly")
    for key in UNSUPPORTED_KEYS:
        if key in msg:
            return "unsupported", dict(
                base, kind="unsupported", unsupported_type=key,
                origin="document_content", command_class="data",
                turn_class="media_readonly")
    return "unsupported", dict(
        base, kind="unsupported", unsupported_type="unknown",
        origin="document_content", command_class="data",
        turn_class="media_readonly")


def parse_offset(value):
    """Parse an offset value strictly. Returns an int or None.

    Accepts ints and digit strings. Rejects None, bools, floats,
    and non-numeric strings instead of raising, so a corrupt state
    file can never crash the poller or reset the offset to 0.

    Note: str.isdigit() alone is not enough — "²".isdigit() is
    True but int("²") raises, and very long digit strings hit
    Python's int-conversion limit. Both are guarded here.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def parse_chat_id(value):
    """Parse an owner chat id strictly. Returns a positive int or
    None. Accepts ints and digit strings (a config file may hold
    "12345" as a string), and rejects 0, negatives, bools, and
    anything else. This is the ONE shared validation used by the
    poller, the hook, and setup.sh, so the three can never
    disagree about whether an id is valid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        value = value.strip()
        if value.isascii() and value.isdigit():
            try:
                parsed = int(value)
            except ValueError:
                return None
            return parsed if parsed > 0 else None
    return None


def parse_timestamp(value):
    """Parse an epoch timestamp (int/float, not bool). Returns a
    float or None. Used for received_at/ack times, where a
    wrong-shaped value must mean "unknown", never a crash."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts if ts >= 0 else None
    return None


def load_json_file(path):
    """Load a JSON file that must hold an object. Returns the dict
    or None on any error (missing, unreadable, invalid JSON, a
    JSON value that is not an object, or a deeply nested document
    that exhausts the parser's recursion). Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write_json(path, data, mode=0o600):
    """Write JSON atomically: temp file in the same directory,
    fsync, os.replace, restrictive permissions. Raises OSError
    on failure; never leaves a truncated file behind. The temp
    file is created with the restrictive mode from the start
    (os.open), never chmodded after the fact, so no other user
    can read it in between."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(
        directory, ".%s.tmp.%d.%d" % (os.path.basename(path), os.getpid(),
                                      time.time_ns()))
    fd = None
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_inbox_lines(path):
    """Read inbox.jsonl as raw byte lines (without trailing
    newlines). Returns a list of bytes; a missing or unreadable
    file reads as empty. Never raises.

    Binary on purpose: a truncated UTF-8 byte at the end of the
    file (a write cut mid-character by SIGKILL or a power cut)
    must never make the whole inbox unreadable. Callers decode
    each line separately with decode_inbox_line()."""
    try:
        with open(path, "rb") as f:
            return [line.rstrip(b"\n") for line in f]
    except OSError:
        return []


def decode_inbox_line(raw):
    """Decode one inbox line, tolerating bad bytes: undecodable
    bytes become U+FFFD, so one corrupt line can never break a
    whole scan — it is skipped by the JSON parse instead."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def redact(text, secrets):
    """Replace every non-empty secret in text with ***. Used so a
    bot token (or its surrogate) can never leak into a log line
    through an exception message that embeds the request URL."""
    if not isinstance(text, str):
        text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def sanitize_filename(name, default="file"):
    """Turn an untrusted file name into a safe bare file name:
    strip directories, replace unsafe characters, drop leading
    dots, cap the length. Never returns an empty string or a
    name that can escape its directory."""
    base = os.path.basename(str(name or "")).strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    base = base.strip(".")
    if not base or base in (".", ".."):
        return default
    if len(base) > 100:
        stem, dot, ext = base.rpartition(".")
        if dot and len(ext) <= 10:
            base = stem[: 100 - len(ext) - 1] + "." + ext
        else:
            base = base[:100]
    return base


def safe_download_path(base_dir, message_id, file_unique_id, original_name):
    """Build a validated download destination under base_dir.

    The file name is generated, never taken from the sender:
    <message_id>_<file_unique_id>_<sanitized original name>.
    The resolved real path must stay under base_dir (symlink and
    traversal safe). Raises ValueError otherwise. Returns the
    absolute destination path.
    """
    base_real = os.path.realpath(base_dir)
    safe_name = sanitize_filename(original_name, default="download")
    parts = [str(message_id)]
    if file_unique_id:
        parts.append(re.sub(r"[^A-Za-z0-9_-]+", "_", str(file_unique_id)))
    parts.append(safe_name)
    filename = "_".join(parts)
    dest = os.path.join(base_real, filename)
    dest_real = os.path.realpath(dest)
    if os.path.commonpath([dest_real, base_real]) != base_real:
        raise ValueError("download destination escapes the download dir")
    return dest_real


def validate_inside(base_dir, path):
    """Resolve path and require it to stay under base_dir.
    Returns the real path. Raises ValueError otherwise. The base
    directory itself counts as inside (with or without a trailing
    slash on either side): realpath normalizes both, and the
    equality case is handled before commonpath so a trailing
    slash can never turn the base into an "escape"."""
    base_real = os.path.realpath(base_dir)
    real = os.path.realpath(path)
    if real == base_real:
        return real
    try:
        common = os.path.commonpath([real, base_real])
    except ValueError:
        raise ValueError(
            "path %r is outside %r" % (path, base_dir)) from None
    if common != base_real:
        raise ValueError("path %r is outside %r" % (path, base_dir))
    return real


def is_image_file(path):
    """True when path is really an image: the extension must be
    an image extension AND the first bytes must match a known
    image signature. Checking the extension alone lets a text
    file renamed to .png through; the magic bytes are the real
    boundary. Returns False for missing or unreadable files."""
    if os.path.splitext(str(path))[1].lower() not in IMAGE_EXTENSIONS:
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    for magic, magic_at_8 in IMAGE_MAGIC:
        if head.startswith(magic):
            if magic_at_8 is None:
                return True
            return head[8:8 + len(magic_at_8)] == magic_at_8
    return False


def escape_multipart_filename(name):
    """Strip CR, LF, quotes, and backslashes from a file name
    before it goes into a multipart Content-Disposition header,
    so a crafted name cannot inject extra headers or parts."""
    clean = re.sub(r'["\r\n\\]', "", str(name))
    clean = clean.strip()
    return clean or "file"


def split_text(text, limit=MAX_MESSAGE_CHARS):
    """Split text into chunks of at most `limit` characters,
    preferring breaks at newlines, then spaces. Returns a list
    of non-empty chunks (a single chunk when the text fits).

    Only the single separator character at the cut is dropped;
    the rest of the leading whitespace is preserved, so a code
    block split across chunks keeps its indentation."""
    if len(text) <= limit:
        return [text] if text else [""]
    chunks = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut_nl = window.rfind("\n")
        cut_sp = window.rfind(" ")
        if cut_nl > 0:
            cut = cut_nl
            drop = 1  # the newline itself
        elif cut_sp > 0:
            cut = cut_sp
            drop = 1  # the space itself
        else:
            cut = limit
            drop = 0  # mid-word: drop nothing
        chunks.append(rest[:cut])
        rest = rest[cut + drop:]
        if not rest:
            break
    if rest:
        chunks.append(rest)
    return chunks


def parse_retry_after_body(body):
    """Extract Telegram's retry_after seconds from a 429 error
    body (JSON with parameters.retry_after). Returns an int or
    None when the body does not carry one."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    params = data.get("parameters") or {}
    value = params.get("retry_after")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def lock_name(key):
    """Derive a safe lock-file stem from an arbitrary key (a
    hash, so unsanitized input can never shape a path)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class HandledSet:
    """Cached view of handled.txt: the file is re-read only when
    its identity changes, so a keeper polling twice a second
    does not re-read a growing file on every check.

    The cache key is (mtime_ns, size, ino), not mtime alone: on
    filesystems with coarse timestamps two writes can share one
    mtime, and a same-mtime append would otherwise stay
    invisible forever."""

    def __init__(self, path):
        self.path = path
        self._stat = None
        self._keys = set()

    def _current_stat(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def contains(self, key):
        stat = self._current_stat()
        if stat is None:
            return False
        if stat != self._stat:
            try:
                with open(self.path, encoding="utf-8",
                          errors="replace") as f:
                    self._keys = {line.strip() for line in f if line.strip()}
            except OSError:
                self._keys = set()
            self._stat = stat
        return key in self._keys


def acquire_singleton(lock_dir, key):
    """Hold a non-blocking flock on a lock file under lock_dir,
    named by a hash of key. Returns the open file object holding
    the lock, or None when another instance already holds it.
    The caller must keep the returned object alive for as long
    as the singleton must be held. No /tmp pidfiles: the lock
    lives under the runtime dir and cannot be symlinked away."""
    import fcntl

    os.makedirs(lock_dir, exist_ok=True)
    path = os.path.join(lock_dir, "lock-" + lock_name(key))
    f = open(path, "a", encoding="utf-8")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.write(str(os.getpid()))
    f.flush()
    return f


def cleanup_stale_locks(lock_dir, max_age_secs=LOCK_STALE_SECS):
    """Remove orphaned lock files: a lock file is stale when it
    is older than max_age_secs AND no process holds its flock
    (verified by taking the lock ourselves first, so a live
    keeper's lock is never removed). Returns the number of files
    removed. Best-effort: any error is ignored."""
    import fcntl

    removed = 0
    try:
        names = os.listdir(lock_dir)
    except OSError:
        return 0
    now = time.time()
    for name in names:
        if not name.startswith("lock-"):
            continue
        path = os.path.join(lock_dir, name)
        try:
            if now - os.path.getmtime(path) < max_age_secs:
                continue
        except OSError:
            continue
        try:
            with open(path, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # We hold the lock: nobody else is using it.
        except (BlockingIOError, OSError):
            continue
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


def append_handled(handled_path, key):
    """Append one handled key to handled.txt under an exclusive
    flock, skipping duplicates. Every writer of handled.txt —
    the agent (via `telegram.py mark_handled`) and the poller's
    prune — must go through this lock, or a prune's os.replace
    can lose a concurrent append. Returns True when the key was
    newly added, False when it was already present."""
    import fcntl

    key = str(key).strip()
    if not key or "\n" in key:
        raise ValueError("invalid handled key")
    directory = os.path.dirname(os.path.abspath(handled_path))
    os.makedirs(directory, exist_ok=True)
    lock_path = handled_path + ".lock"
    lock_file = open(lock_path, "a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            with open(handled_path, encoding="utf-8",
                      errors="replace") as f:
                existing = {line.strip() for line in f if line.strip()}
        except OSError:
            existing = set()
        if key in existing:
            return False
        with open(handled_path, "a", encoding="utf-8") as f:
            f.write(key + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    finally:
        lock_file.close()


def load_acks(ack_path):
    """Read the ack file into {key: latest_epoch}. Lines are
    '<key> <epoch>'; malformed lines are ignored and bad bytes
    are replaced, so a corrupt ack file can only lose acks (the
    message is then redelivered), never crash the scan."""
    acks = {}
    try:
        with open(ack_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) != 2:
                    continue
                key, raw_ts = parts
                try:
                    ts = float(raw_ts)
                except ValueError:
                    continue
                if ts >= 0 and ts > acks.get(key, -1):
                    acks[key] = ts
    except OSError:
        return {}
    return acks


def append_ack(ack_path, key, now=None):
    """Record an in-progress ack for one handled key. The agent
    acks a message when it STARTS work; while the ack is fresh
    (ACK_TTL_SECS) the scan postpones redelivery, so a long task
    is not duplicated by the 10-minute retry. The file keeps the
    latest ack per key (bounded), rewritten atomically under the
    same flock discipline as handled.txt. Returns True."""
    import fcntl

    key = str(key).strip()
    if not key or any(ch.isspace() for ch in key):
        raise ValueError("invalid ack key")
    ts = time.time() if now is None else float(now)
    directory = os.path.dirname(os.path.abspath(ack_path))
    os.makedirs(directory, exist_ok=True)
    lock_file = open(ack_path + ".lock", "a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        acks = load_acks(ack_path)
        acks[key] = ts
        # Bound the file: keep the 1000 most recent acks.
        items = sorted(acks.items(), key=lambda kv: kv[1])[-1000:]
        tmp_path = ack_path + ".tmp.%d" % os.getpid()
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for k, v in items:
                    f.write("%s %.3f\n" % (k, v))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, ack_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    finally:
        lock_file.close()
