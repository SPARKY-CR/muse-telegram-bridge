#!/usr/bin/env python3
"""Scan the Telegram inbox and decide what the hook should wake for.

This is the hook's brain, kept in Python so it is testable. It reads
inbox.jsonl (claimed updates from the poller) and produces the wake
payload, while maintaining two markers:

  seen.json    — every update at or below this id has been woken
                 (or was never wake-worthy). Only moves forward.
  pending.json — woken messages not yet confirmed handled, with
                 their full payload and the time they were woken.
                 A message stays here until its key appears in
                 handled.txt, so a failed wake is retried instead
                 of lost (duplicate wakes are safer than losses).

Rules (see the code review):
- A corrupt seen.json fails closed: the inbox's max update id is
  used as the seen marker, so history is never replayed and only
  genuinely new messages are delivered. A MISSING seen.json with
  a non-empty inbox does the same, except messages younger than
  24 hours are still delivered (a first run must not lose the
  message that triggered it).
- A corrupt pending.json falls back to pending.json.bak (kept on
  every write); if both are unreadable, pending is empty and the
  messages are re-scanned from the inbox when possible.
- Both markers are written atomically (temp file + rename).
- Classification is shared with the poller via bridge_common, so
  the two can never disagree about a message type. Unsupported
  types (sticker, video, ...) are woken as kind "unsupported" so
  the agent can reply "can't handle that yet".
- Retries are capped: a pending message is re-woken with
  exponential backoff, at most MAX_RETRY_ATTEMPTS times and
  within PENDING_TTL_SECS. Past that it is dropped and listed in
  the payload's "dropped" field, so the agent can tell the owner
  instead of waking forever. Re-woken payloads carry
  "redelivery": true and "attempt": n. A message is only dropped
  once its backoff has elapsed — never minutes after its latest
  wake while the agent may still be working on it. Dropped
  entries carry message_id, date, a text excerpt, and a drop_key;
  the entry STAYS pending and the notice is redelivered after
  its retry interval (not on every scan) until the agent marks
  the drop key handled, so a failed wake cannot lose the "your
  message was dropped" notice itself.
- An agent ack (acks file, written when work starts) postpones
  redelivery while it is fresh (ACK_TTL_SECS), so long-running
  work is not duplicated by the retry; an expired ack lets the
  retry proceed.
- Messages already in handled.txt are never re-woken, even when
  the seen marker was lost (the v2->v3 upgrade case): the marker
  advances past them silently. When recovering from a lost
  marker, freshness is judged by the inbox line's received_at
  (stamped by the poller) for EVERY payload type, callbacks
  included; legacy lines without received_at fall back to the
  payload date (for a callback, the button message's date), so
  an old tap is no longer exempt from the 24h cutoff.
- Update ids are normally monotonic. The epoch-reset path is
  defensive and unconfirmed against real Telegram behaviour:
  the seen marker keeps recent_update_ids and an epoch boundary
  so a fresh update below the marker that arrived after the
  boundary and was never processed is delivered once, while old
  history below the marker stays skipped. If Telegram never
  reuses lower ids, this path never activates.
- The inbox is read as bytes and decoded per line, so one
  truncated UTF-8 byte can never fail the whole scan. Valid JSON
  with the wrong shape and deeply nested lines are skipped the
  same way, never fatal.
- At most MAX_MESSAGES_PER_WAKE messages are delivered per scan,
  so one scan can never flood the agent with a replayed inbox.
- The owner check uses the sender (from.id) and requires a private
  chat unless --allow-groups is set (group/supergroup chats then
  wake only for the owner's own messages and taps).
- Handled keys are per update, not per message: callbacks are
  keyed by callback_query_id, so two taps on one message do not
  share a key and defeat the retry guard. Keys use the payload's
  chat_id, so group chats cannot collide with the private chat.

Usage:
  scan_inbox.py --inbox inbox.jsonl --seen seen.json \
    --pending pending.json --handled handled.txt --chat-id N \
    [--retry-after 600] [--allow-groups] [--dry-run]

Prints one JSON object to stdout:
  {"new_messages": [...], "max_update_id": N, "seen": S,
   "pending": P, "dropped": [...], "warnings": [...]}
Exits 0 on success, 1 on a fatal error (the hook then stays silent).
A second scan running at the same time exits 0 with an empty
payload (the hook serializes scans, but defense in depth).
"""

import argparse
import fcntl
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_common  # noqa: E402

DEFAULT_RETRY_AFTER = 600  # re-wake an unhandled message after 10 min


def _seen_state_from(path):
    """The seen state from a seen file: (value, recent_ids,
    epoch_boundary_at), or None when the file is missing,
    corrupt, or holds no valid id. Legacy files (pre-v5) carry
    only last_update_id: recent_ids is empty and the epoch
    boundary falls back to the file's mtime, so updates received
    after the marker was last written can still be recognised
    as an epoch reset without replaying older history."""
    seen = bridge_common.load_json_file(path)
    if seen is None:
        return None
    value = bridge_common.parse_offset(seen.get("last_update_id"))
    if value is None:
        return None
    recent = []
    raw_recent = seen.get("recent_update_ids")
    if isinstance(raw_recent, list):
        for item in raw_recent:
            parsed = bridge_common.parse_offset(item)
            if parsed is not None and parsed not in recent:
                recent.append(parsed)
    boundary = bridge_common.parse_timestamp(
        seen.get("epoch_boundary_at"))
    if boundary is None:
        try:
            boundary = os.path.getmtime(path)
        except OSError:
            boundary = 0.0
    return value, recent, boundary


def load_seen(seen_path, inbox_max):
    """Read the seen marker. Returns (value, warnings, missing,
    recent_ids, epoch_boundary_at, valid).

    - Valid file: its last_update_id (valid=True).
    - Corrupt file, or a file without a valid id: fall back to
      seen.json.bak (kept on every write), so the marker never
      moves backwards just because the inbox lost a line.
    - Corrupt file and corrupt/missing .bak: fail closed to the
      inbox max id (skip history, deliver only new messages);
      valid=False, so no epoch-reset delivery happens either.
    - Missing file with a non-empty inbox: fail closed to the
      inbox max id as well — a deleted marker must not replay up
      to 2000 old messages. The caller still delivers messages
      younger than MAX_MESSAGE_AGE_SECS (a first run).
    - Missing file with an empty inbox: 0 (a genuine first run).
    """
    state = _seen_state_from(seen_path)
    if state is not None:
        value, recent, boundary = state
        return value, [], False, recent, boundary, True
    if os.path.exists(seen_path):
        bak_state = _seen_state_from(seen_path + ".bak")
        if bak_state is not None:
            value, recent, boundary = bak_state
            return (value, ["seen file corrupt; using .bak copy"],
                    False, recent, boundary, True)
        return (inbox_max, ["seen file corrupt; using inbox max id"],
                False, [], 0.0, False)
    if inbox_max > 0:
        return (inbox_max,
                ["seen file missing; using inbox max id (only "
                 "messages younger than 24h will be delivered)"],
                True, [], 0.0, False)
    return 0, [], True, [], 0.0, False


def save_seen(seen_path, value, dry_run, recent_ids=None,
              epoch_boundary_at=None):
    """Write seen.json atomically (last_update_id plus the
    recent-id/epoch state), then refresh the .bak copy to the
    same content, so a later corruption can fall back to the
    latest known-good marker (not one scan behind it). Skipped
    on dry runs."""
    if dry_run:
        return
    recent = list(recent_ids or [])[
        -bridge_common.MAX_RECENT_UPDATE_IDS:]
    bridge_common.atomic_write_json(seen_path, {
        "last_update_id": value,
        "recent_update_ids": recent,
        "epoch_boundary_at": epoch_boundary_at or 0,
    })
    try:
        shutil.copyfile(seen_path, seen_path + ".bak")
    except OSError:
        pass


def load_pending(pending_path):
    """Read pending.json (falling back to the .bak copy). Returns
    a dict of update_id -> raw entry. Never raises. Entries are
    validated by _sanitize_pending before use, so a wrong-shaped
    entry is skipped with a warning instead of crashing every
    later scan."""
    for path in (pending_path, pending_path + ".bak"):
        data = bridge_common.load_json_file(path)
        if data is None:
            continue
        entries = data.get("entries")
        if isinstance(entries, dict):
            out = {}
            for key, entry in entries.items():
                uid = bridge_common.parse_offset(key)
                if uid is not None:
                    out[uid] = entry
            return out
    return {}


def _pending_payload(entry):
    """The payload dict of a pending entry, or None when the
    entry or payload has the wrong shape (valid JSON, wrong
    type — e.g. a string payload from a corrupt pending.json)."""
    if not isinstance(entry, dict):
        return None
    payload = entry.get("payload")
    return payload if isinstance(payload, dict) else None


def _pending_attempts(entry):
    parsed = bridge_common.parse_offset(
        entry.get("attempts") if isinstance(entry, dict) else None)
    return parsed if parsed is not None and parsed > 0 else 1


def _pending_time(entry, key, default=0.0):
    parsed = bridge_common.parse_timestamp(
        entry.get(key) if isinstance(entry, dict) else None)
    return parsed if parsed is not None else default


def _pending_retry_after(entry, default):
    parsed = bridge_common.parse_timestamp(
        entry.get("retry_after") if isinstance(entry, dict) else None)
    if parsed is None or parsed <= 0:
        parsed = bridge_common.parse_offset(
            entry.get("retry_after")
            if isinstance(entry, dict) else None)
    return parsed if parsed is not None and parsed > 0 else default


def _sanitize_pending(pending, warnings, retry_after):
    """Drop pending entries whose shape cannot be used safely,
    normalising the numeric fields the retry logic compares.
    One bad entry (a string payload, a list woken_at, ...) is
    skipped with a warning; it can never break the whole scan
    permanently."""
    for uid in list(pending):
        entry = pending[uid]
        if _pending_payload(entry) is None:
            warnings.append(f"skipped malformed pending entry {uid}")
            del pending[uid]
            continue
        entry["attempts"] = _pending_attempts(entry)
        entry["woken_at"] = _pending_time(entry, "woken_at", 0.0)
        entry["first_woken_at"] = _pending_time(
            entry, "first_woken_at", entry["woken_at"])
        if entry.get("dropped"):
            entry["retry_after"] = _pending_retry_after(
                entry, retry_after)
            notified = bridge_common.parse_timestamp(
                entry.get("dropped_notified_at"))
            if notified is None:
                entry.pop("dropped_notified_at", None)
            else:
                entry["dropped_notified_at"] = notified
    return pending


def save_pending(pending_path, entries, dry_run):
    """Write pending.json atomically, keeping the previous copy
    as .bak. Skipped on a dry run."""
    if dry_run:
        return
    if os.path.exists(pending_path):
        try:
            shutil.copyfile(pending_path, pending_path + ".bak")
        except OSError:
            pass
    bridge_common.atomic_write_json(
        pending_path, {"entries": {str(k): v for k, v in entries.items()}})


def load_handled(handled_path):
    """Read handled.txt into a set of 'chat_id:message_id' keys.
    Bad bytes are replaced, never fatal: a truncated byte in this
    file must not crash the whole scan (same class as item A)."""
    try:
        with open(handled_path, encoding="utf-8",
                  errors="replace") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


def _payload_chat_id(chat_id, payload):
    """The chat a payload belongs to: its own chat_id when it
    carries one (group mode), else the owner's chat (private
    payloads and pre-v5 pending entries without a chat_id)."""
    if not isinstance(payload, dict):
        return chat_id
    raw = payload.get("chat_id")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw != 0:
        return raw
    return chat_id


def handled_key(chat_id, payload):
    """The handled.txt key for a payload. Messages are keyed by
    message_id; callbacks are keyed by callback_query_id, so two
    taps on the same message get distinct keys and one handled
    tap cannot confirm (or a retry guard cannot lose) the other.
    The chat part is the payload's chat, so a group message and
    a private message with the same message_id never share a key."""
    target = _payload_chat_id(chat_id, payload)
    if not isinstance(payload, dict):
        return f"{target}:0"
    if payload.get("kind") == "callback":
        cb_id = payload.get("callback_query_id") or ""
        return f"{target}:cb:{cb_id}"
    return f"{target}:{payload.get('message_id', 0)}"


def drop_key(chat_id, payload, uid):
    """The handled.txt key that confirms a dropped notice was
    delivered to the owner: '<chat_id>:drop:<update_id>'. The
    dropped entry stays pending until this key appears."""
    return f"{_payload_chat_id(chat_id, payload)}:drop:{uid}"


def _event_time(upd, payload):
    """When an update actually arrived, for the 24h recovery
    cutoff: the poller's received_at stamp when present (every
    payload type, callbacks included — a tap's arrival time, not
    its button message's date). Legacy lines without the stamp
    fall back to the payload date; for a callback that is the
    button message's date, so an old tap on an old button is
    still cut off instead of exempt (item B)."""
    received = bridge_common.parse_timestamp(upd.get("received_at"))
    if received is not None:
        return received
    return bridge_common.parse_timestamp(payload.get("date"))


def retry_wait_seconds(retry_after, attempts):
    """Exponential backoff for a re-wake: retry_after, then
    2x, 4x, ... capped at one hour, so a stuck message does not
    wake every 10 minutes forever."""
    return min(retry_after * (2 ** max(attempts - 1, 0)), 3600)


def _dropped_entry(uid, entry, attempts, reason, chat_id=0):
    """A dropped pending message, with enough of the original
    payload for the agent to tell the owner what was lost: the
    message id, its date, and a short excerpt of the text (or
    the button data for a callback). The old entry carried only
    update_id/attempts/reason, so the agent could never describe
    the message (item C). drop_key is the handled.txt key the
    agent marks once the owner has been told; until then the
    notice is redelivered after its retry interval."""
    payload = _pending_payload(entry) or {}
    out = {"update_id": uid, "attempts": attempts, "reason": reason,
           "drop_key": drop_key(chat_id, payload, uid)}
    if "message_id" in payload:
        out["message_id"] = payload["message_id"]
    if "date" in payload:
        out["date"] = payload["date"]
    if "chat_id" in payload:
        out["chat_id"] = payload["chat_id"]
    text = (payload.get("text") or payload.get("data")
            or payload.get("caption") or "")
    if text:
        out["text"] = str(text)[:200]
    return out


def _mark_dropped(entry, now, reason, retry_after=DEFAULT_RETRY_AFTER):
    """Mark a pending entry dropped (in place). The entry is
    kept, not deleted: its notice must survive a failed wake
    and be redelivered after retry_after until the drop key is
    handled. dropped_notified_at stamps this delivery, so an
    immediate rescan does not duplicate the notice."""
    entry["dropped"] = True
    entry["dropped_at"] = now
    entry["drop_reason"] = reason
    entry["dropped_notified_at"] = now
    entry["retry_after"] = retry_after


def _dropped_notice_due(entry, now, retry_after):
    """True when a dropped entry's notice should be delivered
    again: never notified yet (legacy entry), or the stored
    retry interval has elapsed since dropped_notified_at."""
    interval = _pending_retry_after(entry, retry_after)
    notified = bridge_common.parse_timestamp(
        entry.get("dropped_notified_at")
        if isinstance(entry, dict) else None)
    if notified is None:
        return True, interval
    return now - notified >= interval, interval


def _settings_locked(pending_path, settings_path=None):
    """Locked flag from settings.json (write-pause). Missing or
    malformed settings fail open to unlocked, matching the
    helper's settings_get defaults."""
    path = settings_path or os.environ.get("TELEGRAM_SETTINGS_FILE")
    if not path:
        path = os.path.join(
            os.path.dirname(os.path.abspath(pending_path)),
            "settings.json")
    data = bridge_common.load_json_file(path) or {}
    locked = data.get("locked")
    return locked if isinstance(locked, bool) else False


def scan(inbox_path, seen_path, pending_path, handled_path, chat_id,
         retry_after=DEFAULT_RETRY_AFTER, allow_groups=False,
         dry_run=False, ack_path=None, settings_path=None):
    """Run one scan. Returns (payload_dict, warnings). Writes the
    markers unless dry_run is set."""
    warnings = []
    now = time.time()
    handled = load_handled(handled_path)
    acks = bridge_common.load_acks(ack_path or handled_path + ".ack")

    # First pass: read the inbox, find the max update id. The
    # inbox is read as bytes and each line is decoded separately
    # with replacement: one truncated UTF-8 byte (a write cut
    # mid-character) must never fail the whole scan (item A) —
    # the bad line is skipped instead of crashing the hook.
    inbox_max = 0
    updates = []
    malformed = 0
    for raw in bridge_common.read_inbox_lines(inbox_path):
        line = bridge_common.decode_inbox_line(raw).strip()
        if not line:
            continue
        try:
            upd = json.loads(line)
        except (ValueError, RecursionError):
            # Invalid JSON, or a deeply nested line that exhausts
            # the parser: skip the line, never fail the scan.
            malformed += 1
            continue
        if not isinstance(upd, dict):
            malformed += 1
            continue
        uid = upd.get("update_id")
        if isinstance(uid, int) and not isinstance(uid, bool):
            if uid > inbox_max:
                inbox_max = uid
            updates.append(upd)
        else:
            malformed += 1
    if malformed:
        warnings.append(
            f"skipped {malformed} malformed inbox line(s)")

    (seen, seen_warnings, seen_missing, recent_ids,
     epoch_boundary_at, seen_valid) = load_seen(seen_path, inbox_max)
    warnings.extend(seen_warnings)
    if not epoch_boundary_at:
        epoch_boundary_at = now
    pending = _sanitize_pending(
        load_pending(pending_path), warnings, retry_after)
    locked = _settings_locked(pending_path, settings_path)

    # Confirm pending messages the agent has handled since the
    # last scan; drop them so they are never re-woken. A dropped
    # entry clears when its drop key is handled (the owner was
    # told), or when the original message turns out handled.
    for uid in list(pending):
        entry = pending[uid]
        payload = _pending_payload(entry)
        if payload is None:
            warnings.append(f"skipped malformed pending entry {uid}")
            del pending[uid]
            continue
        if handled_key(chat_id, payload) in handled:
            del pending[uid]
        elif (entry.get("dropped")
                and drop_key(chat_id, payload, uid) in handled):
            del pending[uid]

    new_messages = []
    dropped = []
    # Recent ids processed under the current marker epoch. A
    # fresh update BELOW the marker (Telegram reused lower ids
    # after a long silence) is delivered once when it arrived
    # after the epoch boundary and is not in this set; the set
    # is what stops the next scan from delivering it again.
    recent_list = list(recent_ids)
    recent_set = set(recent_list)

    def remember(uid):
        if uid not in recent_set:
            recent_set.add(uid)
            recent_list.append(uid)

    # The marker only advances past updates this scan actually
    # processed. When recovering from a lost marker it starts at
    # 0 (not at inbox_max), so the per-wake cap can hold it back
    # and the next scan picks up the undelivered messages.
    max_seen = 0 if seen_missing else seen
    # When the seen marker was lost, only messages younger than
    # MAX_MESSAGE_AGE_SECS are delivered; older history stays
    # skipped (fail closed), and the marker still advances.
    recover_cutoff = now - bridge_common.MAX_MESSAGE_AGE_SECS
    for upd in updates:
        uid = upd.get("update_id", 0)
        below_seen = uid <= seen and not seen_missing
        if below_seen and (not seen_valid or uid in recent_set):
            # Corrupt-marker fail-closed, or already processed in
            # this epoch: never reconsider it.
            continue
        kind, payload = bridge_common.classify(
            upd, chat_id, allow_groups=allow_groups)
        if kind is None:
            # Not from the owner (or a group chat): never
            # wake-worthy, so the marker advances past it.
            if uid > max_seen:
                max_seen = uid
            remember(uid)
            continue
        received = bridge_common.parse_timestamp(upd.get("received_at"))
        if received is not None:
            payload["received_at"] = received
        # Deterministic pre-router: typed slash commands are
        # parsed against the gate table and live settings, so
        # unknown/locked-blocked commands carry a fixed-reply
        # class and never reach tool use.
        bridge_common.apply_command_gate(payload, locked=locked)
        if seen_missing and uid <= seen:
            # Recovering from a lost marker: deliver only fresh
            # updates, but still advance past the old ones. The
            # cutoff uses received_at for every payload type —
            # callbacks included (item B): an old tap is cut off
            # by when it ARRIVED, not exempted because Telegram
            # gives it no tap timestamp.
            event_time = _event_time(upd, payload)
            if event_time is None or event_time < recover_cutoff:
                if uid > max_seen:
                    max_seen = uid
                remember(uid)
                continue
        elif below_seen:
            # Epoch reset: the id is below the marker, but the
            # update arrived after the marker's epoch boundary
            # and is still fresh — Telegram reused lower ids.
            # Deliver it once instead of ignoring it forever.
            # Without a received_at stamp there is no proof it is
            # new, so legacy lines stay skipped (fail closed).
            if (received is None or received < recover_cutoff
                    or received <= epoch_boundary_at):
                continue
            warnings.append(
                f"update {uid} below seen marker {seen} arrived "
                "after the epoch boundary; delivering as epoch reset")
        if handled_key(chat_id, payload) in handled:
            # Already answered before the marker was lost (the
            # v2->v3 upgrade case): never re-wake it (item B).
            if uid > max_seen:
                max_seen = uid
            remember(uid)
            continue
        if uid in pending:
            # Already woken and still awaiting the agent; the
            # retry check below decides whether to re-wake it.
            if uid > max_seen:
                max_seen = uid
            remember(uid)
            continue
        if len(new_messages) >= bridge_common.MAX_MESSAGES_PER_WAKE:
            # Per-wake cap reached: stop here and do NOT advance
            # the marker past the undelivered messages, so the
            # next scan picks them up.
            break
        new_messages.append(payload)
        pending[uid] = {"payload": payload, "woken_at": now,
                        "first_woken_at": now, "attempts": 1}
        if uid > max_seen:
            max_seen = uid
        remember(uid)

    # Retry: a pending message the agent never confirmed (the
    # wake failed, or the turn was dropped) is woken again once
    # its backoff has elapsed — but only up to MAX_RETRY_ATTEMPTS
    # and within PENDING_TTL_SECS. Past that it is dropped and
    # reported, so one stuck message cannot wake forever.
    for uid, entry in list(pending.items()):
        if uid in {m.get("update_id") for m in new_messages}:
            continue
        payload = _pending_payload(entry)
        if payload is None:
            warnings.append(f"skipped malformed pending entry {uid}")
            del pending[uid]
            continue
        hkey = handled_key(chat_id, payload)
        if hkey in handled:
            del pending[uid]
            continue
        attempts = _pending_attempts(entry)
        if entry.get("dropped"):
            # The notice itself must survive a failed wake:
            # redeliver it after its retry interval until the
            # agent marks the drop key handled (the owner was
            # told). An immediate rescan must not duplicate it.
            if drop_key(chat_id, payload, uid) in handled:
                del pending[uid]
                continue
            due, interval = _dropped_notice_due(
                entry, now, retry_after)
            if due:
                entry["dropped_notified_at"] = now
                entry["retry_after"] = interval
                dropped.append(_dropped_entry(
                    uid, entry, attempts,
                    entry.get("drop_reason", "dropped"), chat_id))
            continue
        woken_at = _pending_time(entry, "woken_at", 0.0)
        first_woken = _pending_time(
            entry, "first_woken_at", woken_at)
        # Ack: the agent marked work in progress for this wake.
        # While the ack is fresh, postpone redelivery (and the
        # drop) so long-running work is not duplicated; once it
        # expires, the retry proceeds at once.
        ack_at = acks.get(hkey)
        ack_current = ack_at is not None and ack_at >= woken_at
        if ack_current and now - ack_at < bridge_common.ACK_TTL_SECS:
            continue
        ack_expired = ack_current
        # TTL first: a message older than the TTL is dropped even
        # if its backoff has not elapsed yet (a fresh ack above
        # already postponed this while work is active).
        if now - first_woken > bridge_common.PENDING_TTL_SECS:
            _mark_dropped(entry, now, "pending TTL expired", retry_after)
            dropped.append(_dropped_entry(uid, entry, attempts,
                                          "pending TTL expired",
                                          chat_id))
            warnings.append(
                f"dropping unhandled update {uid} after {attempts} "
                "attempts; telling the owner via the payload")
            continue
        wait = retry_wait_seconds(retry_after, attempts)
        if not ack_expired and now - woken_at < wait:
            # The backoff has not elapsed: the agent may still be
            # working on the last wake. Do NOT drop yet — the old
            # code checked the cap here and dropped a message
            # minutes after its latest wake (item C).
            continue
        if attempts >= bridge_common.MAX_RETRY_ATTEMPTS:
            _mark_dropped(entry, now, "retry cap reached", retry_after)
            dropped.append(_dropped_entry(uid, entry, attempts,
                                          "retry cap reached",
                                          chat_id))
            warnings.append(
                f"dropping unhandled update {uid} after {attempts} "
                "attempts; telling the owner via the payload")
            continue
        if len(new_messages) >= bridge_common.MAX_MESSAGES_PER_WAKE:
            continue
        redelivered = dict(payload)
        bridge_common.apply_command_gate(redelivered, locked=locked)
        redelivered["redelivery"] = True
        redelivered["attempt"] = attempts + 1
        new_messages.append(redelivered)
        pending[uid]["woken_at"] = now
        pending[uid]["attempts"] = attempts + 1
        warnings.append(f"re-waking unhandled update {uid} "
                        f"(attempt {attempts + 1})")

    new_seen = max_seen
    if not dry_run:
        # Pending first: if its write fails, the seen marker must
        # NOT advance, or the woken messages would be stranded
        # past it with no retry record.
        try:
            save_pending(pending_path, pending, dry_run)
        except OSError as exc:
            warnings.append(f"could not write pending file: {exc}")
            new_seen = seen
        else:
            try:
                save_seen(seen_path, new_seen, dry_run,
                          recent_ids=recent_list,
                          epoch_boundary_at=epoch_boundary_at)
            except OSError as exc:
                warnings.append(f"could not write seen file: {exc}")

    payload = {"new_messages": new_messages,
               "max_update_id": inbox_max,
               "seen": new_seen,
               "pending": len(pending),
               "dropped": dropped,
               "warnings": warnings}
    return payload, warnings


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Scan the Telegram inbox")
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--seen", required=True)
    parser.add_argument("--pending", required=True)
    parser.add_argument("--handled", required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--retry-after", type=float,
                        default=DEFAULT_RETRY_AFTER)
    parser.add_argument("--ack", default=None,
                        help="Ack file (default: <handled>.ack)")
    parser.add_argument("--settings", default=None,
                        help="Settings file for the command gate "
                             "(default: settings.json next to "
                             "the pending file)")
    parser.add_argument("--allow-groups", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.chat_id <= 0:
        print("invalid --chat-id (must be a positive integer)",
              file=sys.stderr)
        return 2
    # One scan at a time: two concurrent scans would read the
    # same seen marker and wake twice for one message. The hook
    # already serializes runs, but this guards direct invocations
    # too. A contended scan exits 0 with an empty payload.
    lock_path = args.pending + ".lock"
    lock_file = None
    try:
        lock_file = open(lock_path, "a", encoding="utf-8")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Only a contended lock means "another scan is running":
        # any other open error (e.g. the lock path is a directory)
        # is a real failure and must exit 1, not silently report
        # an empty payload (review item, low).
        if lock_file is not None:
            lock_file.close()
        print(json.dumps({"new_messages": [], "max_update_id": 0,
                          "seen": 0, "pending": 0, "dropped": [],
                          "warnings": ["another scan is running"]}))
        return 0
    except OSError as exc:
        if lock_file is not None:
            lock_file.close()
        print(f"scan lock failed: {exc}", file=sys.stderr)
        return 1
    try:
        payload, _warnings = scan(
            args.inbox, args.seen, args.pending, args.handled,
            args.chat_id, retry_after=args.retry_after,
            allow_groups=args.allow_groups, dry_run=args.dry_run,
            ack_path=args.ack, settings_path=args.settings)
    except Exception as exc:  # noqa: BLE001 - fail closed, stay silent
        print(f"scan failed: {exc}", file=sys.stderr)
        return 1
    finally:
        lock_file.close()
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
