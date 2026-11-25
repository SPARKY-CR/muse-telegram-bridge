#!/usr/bin/env python3
"""Mutation check for the muse-telegram-bridge fixes.

For each critical/high fix, revert it in a COPY of the repo and
run the test that covers it: the test must go red. A fix whose
revert leaves the suite green is not actually tested.

Usage: python3 tests/mutation_check.py [--list]
Exit 0 when every mutation is caught, 1 otherwise.
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (name, file, old_text, new_text, test_selector)
MUTATIONS = [
    ("C1: hook lock fd inherited by the poller",
     "poller/hook-script.sh",
     '>> "$WEBHOOK_DIR/poller.log" 2>&1 9>&- &',
     '>> "$WEBHOOK_DIR/poller.log" 2>&1 &',
     "test_hook.TestHookSecondRun"),
    ("C2: missing bridge files fail loudly",
     "poller/hook-script.sh",
     'if [ ! -f "$POLLER_PY" ]; then\n  log "config" '
     '\'{"error":"poller.py not found"}\'',
     'if false; then\n  log "config" '
     '\'{"error":"poller.py not found"}\'',
     "test_hook.TestHookLocation.test_missing_poller_py_fails_loudly"),
    ("C2: BRIDGE_ROOT env var is honoured",
     "poller/hook-script.sh",
     'BRIDGE_ROOT="${BRIDGE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"',
     'BRIDGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"',
     "test_hook.TestHookLocation."
     "test_copied_hook_works_with_bridge_root"),
    ("H1: retry cap",
     "poller/scan_inbox.py",
     "        if attempts >= bridge_common.MAX_RETRY_ATTEMPTS:",
     "        if False:  # attempts >= bridge_common.MAX_RETRY_ATTEMPTS",
     "test_scan.TestRetryCap.test_retry_cap_drops_message"),
    ("H2: missing seen falls back to inbox max",
     "poller/scan_inbox.py",
     "    if inbox_max > 0:\n        return (inbox_max,",
     "    if inbox_max > 0:\n        return (0,",
     "test_scan.TestMissingSeen.test_missing_seen_skips_old_inbox"),
    ("M1: heartbeat thread",
     "poller/poller.py",
     "    hb_thread.start()",
     "    pass  # hb_thread.start()",
     "test_poller_subprocess.TestPollerSubprocess."
     "test_heartbeat_advances_during_429_backoff"),
    ("M2: SIGTERM raises SystemExit in a poll",
     "poller/poller.py",
     "    if _in_poll.is_set():\n        raise SystemExit(0)",
     "    if _in_poll.is_set():\n        pass",
     "test_poller_subprocess.TestPollerSubprocess."
     "test_sigterm_exits_within_3_seconds"),
    ("M3: inbox write rolls back on error",
     "poller/poller.py",
     "                    os.ftruncate(f.fileno(), size_before)",
     "                    pass  # os.ftruncate",
     "test_poller.TestAppendInbox."
     "test_failed_write_leaves_no_partial_line"),
    ("M4: digit-string chat id accepted",
     "poller/bridge_common.py",
     "    if isinstance(value, str):\n        value = value.strip()\n"
     "        if value.isascii() and value.isdigit():",
     "    if isinstance(value, str):\n        return None\n"
     "        if value.isascii() and value.isdigit():",
     "test_bridge_common.TestParseChatId.test_int_and_digit_string"),
    ("M7: image check by magic bytes",
     "poller/bridge_common.py",
     "    for magic, magic_at_8 in IMAGE_MAGIC:",
     "    for magic, magic_at_8 in []:",
     "test_bridge_common.TestSafePaths.test_is_image_file"),
    ("A: inbox lines decode with errors=replace",
     "poller/bridge_common.py",
     '        return raw.decode("utf-8", errors="replace")',
     '        return raw.decode("utf-8")',
     "test_scan_fuzz.TestScanFuzz."
     "test_truncated_utf8_tail_scans_clean"),
    ("B: fresh loop skips already-handled messages",
     "poller/scan_inbox.py",
     "        if handled_key(chat_id, payload) in handled:\n"
     "            # Already answered before the marker was lost (the",
     "        if False and handled_key(chat_id, payload) in handled:\n"
     "            # Already answered before the marker was lost (the",
     "test_scan.TestHandledSkip."
     "test_missing_seen_with_full_handled_wakes_nothing"),
    ("C: dropped-only wake counts dropped messages",
     "poller/hook-script.sh",
     "print(len(d['new_messages']) + len(d.get('dropped', [])))",
     "print(len(d['new_messages']))",
     "test_hook.TestDroppedOnlyWake."
     "test_wakes_when_only_dropped_messages"),
    ("C: drop only after the backoff has elapsed",
     "poller/scan_inbox.py",
     "        if not ack_expired and now - woken_at < wait:",
     "        if False:  # not ack_expired and now - woken_at < wait",
     "test_scan.TestDroppedEntries."
     "test_drop_only_after_backoff_elapsed"),
    ("D: heartbeat stops past the progress deadline",
     "poller/poller.py",
     "            if now <= deadline:",
     "            if True:  # now <= deadline",
     "test_poller_subprocess.TestPollerSubprocess."
     "test_heartbeat_goes_stale_when_main_loop_stuck"),
    ("L6: 409 detected by exc.code, not by string",
     "poller/poller.py",
     "                        and exc.code == 409):",
     "                        and exc.code == 408):",
     "test_poller_subprocess.TestPollerSubprocess."
     "test_409_backoff_keeps_poller_alive"),
    ("C2c: helper paths follow BRIDGE_DIR",
     "bin/telegram.py",
     '    return os.environ.get("BRIDGE_DIR") or os.path.join(REPO_DIR, "runtime")',
     '    return os.path.join(REPO_DIR, "runtime")',
     "test_full_flow.TestFullFlow.test_poller_to_keeper_full_flow"),
    ("B: received_at decides callback freshness",
     "poller/scan_inbox.py",
     '    received = bridge_common.parse_timestamp(upd.get("received_at"))\n'
     "    if received is not None:\n"
     "        return received",
     "    received = None\n"
     "    if received is not None:\n"
     "        return received",
     "test_scan.TestReceivedAtCutoff."
     "test_old_callback_cut_off_by_received_at"),
    ("B: callback handled keys stay distinct",
     "poller/scan_inbox.py",
     '        return f"{target}:cb:{cb_id}"',
     '        return f"{target}:{payload.get(\'message_id\', 0)}"',
     "test_scan.TestCallbackKeys."
     "test_two_callbacks_same_message_distinct_keys"),
    ("C: dropped entries stay pending until drop key handled",
     "poller/scan_inbox.py",
     '    entry["dropped"] = True',
     '    entry["dropped"] = False',
     "test_scan.TestDroppedRedelivery."
     "test_dropped_notice_survives_failed_wake"),
    ("C: drop key form",
     "poller/scan_inbox.py",
     '    return f"{_payload_chat_id(chat_id, payload)}:drop:{uid}"',
     '    return f"{_payload_chat_id(chat_id, payload)}:{uid}"',
     "test_scan.TestDroppedRedelivery."
     "test_dropped_notice_survives_failed_wake"),
    ("ack: fresh ack postpones redelivery",
     "poller/scan_inbox.py",
     "        if ack_current and now - ack_at < bridge_common.ACK_TTL_SECS:",
     "        if False and ack_current and now - ack_at < bridge_common.ACK_TTL_SECS:",
     "test_scan.TestAck.test_fresh_ack_postpones_redelivery"),
    ("groups: poller claims group updates with the flag",
     "poller/poller.py",
     "                    upd, chat_id, allow_groups=args.allow_groups)",
     "                    upd, chat_id, allow_groups=False)",
     "test_poller_subprocess.TestPollerSubprocess."
     "test_group_message_claimed_only_with_allow_groups"),
    ("groups: callback chat type is checked",
     "poller/bridge_common.py",
     'GROUP_CHAT_TYPES = {"group", "supergroup"}',
     "GROUP_CHAT_TYPES = set()",
     "test_bridge_common.TestClassify."
     "test_group_callback_checks_chat_type"),
    ("malformed: scan skips deeply nested lines",
     "poller/scan_inbox.py",
     "        except (ValueError, RecursionError):",
     "        except ValueError:",
     "test_scan_fuzz.TestScanFuzz."
     "test_wrong_shape_and_deep_lines_never_crash"),
    ("malformed: compaction guards non-dict lines",
     "poller/poller.py",
     "            if not isinstance(parsed, dict):",
     "            if False:",
     "test_poller.TestCompactInbox."
     "test_compaction_survives_wrong_shape_lines"),
    ("epoch: fresh lower id delivered",
     "poller/scan_inbox.py",
     "                    or received <= epoch_boundary_at):",
     "                    or True):",
     "test_scan.TestEpochReset.test_fresh_lower_id_delivered_once"),
    ("helper: arbitrary chats refused",
     "bin/telegram.py",
     "    if parsed is None or parsed not in allowed_chat_ids():",
     "    if parsed is None:",
     "test_telegram.TestChatRestriction."
     "test_send_to_arbitrary_chat_refused"),
    ("hook: HOME unset is safe",
     "poller/hook-script.sh",
     'if [ -z "$LEGACY_STATE_DIR" ] && [ -n "${HOME:-}" ]; then',
     'if [ -z "$LEGACY_STATE_DIR" ]; then',
     "test_hook.TestHookHomeUnset.test_hook_runs_without_home"),
    ("hook: scan failures notify after repeats",
     "poller/hook-script.sh",
     "SCAN_FAILURE_NOTIFY_AFTER=3",
     "SCAN_FAILURE_NOTIFY_AFTER=99",
     "test_hook.TestHookScanFailure."
     "test_scan_failure_logged_and_notified_once"),
    ("hook: stale threshold kills hung poller",
     "poller/hook-script.sh",
     'POLLER_STALE_SECS="${BRIDGE_POLLER_STALE_SECS:-120}"',
     'POLLER_STALE_SECS="${BRIDGE_POLLER_STALE_SECS:-9999}"',
     "test_hook.TestHookStuckPoller."
     "test_stuck_poller_killed_and_restarted"),
    ("groups: detached argv separates negative ids",
     "poller/poller.py",
     '"--runtime-dir", runtime_dir, "--", str(chat_id), key',
     '"--runtime-dir", runtime_dir, str(chat_id), key',
     "test_keeper_watchdog.TestGroupDetachedSignals"),
    ("dropped: notice waits for retry interval",
     "poller/scan_inbox.py",
     "return now - notified >= interval, interval",
     "return True, interval",
     "test_scan.TestDroppedRedelivery."
     "test_dropped_notice_survives_failed_wake"),
    ("malformed: pending payload must be a dict",
     "poller/scan_inbox.py",
     "return payload if isinstance(payload, dict) else None",
     "return payload or {}",
     "test_scan.TestMalformedPending."
     "test_string_payload_does_not_crash_scan"),
    ("hook: scan-failure wake exports config env",
     "poller/hook-script.sh",
     'export TELEGRAM_CONFIG_FILE="$CONFIG_FILE"',
     ': # TELEGRAM_CONFIG_FILE export removed',
     "test_hook.TestHookScanFailure."
     "test_scan_failure_wake_exports_custom_config_env"),
    ("epoch: compaction preserves fresh lower-id line",
     "poller/poller.py",
     "and received >= recover_cutoff:",
     "and False:",
     "test_poller.TestCompactInbox."
     "test_compaction_preserves_fresh_epoch_line"),
    ("epoch: offset dropped after long silence",
     "poller/poller.py",
     "return now - last_update_at > bridge_common.OFFSET_SILENCE_DROP_SECS",
     "return False",
     "test_poller_subprocess.TestPollerSubprocess."
     "test_offset_dropped_once_after_long_silence"),
]


def run_mutation(name, rel_path, old, new, selector):
    tmp = tempfile.mkdtemp(prefix="mutation-")
    try:
        copy = os.path.join(tmp, "bridge")
        shutil.copytree(REPO, copy,
                        ignore=shutil.ignore_patterns(
                            "__pycache__", "runtime"))
        path = os.path.join(copy, rel_path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if old not in content:
            return name, "SKIP (pattern not found)"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content.replace(old, new, 1))
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover",
             "-s", os.path.join(copy, "tests"),
             "-p", "test_*.py", "-k", selector],
            capture_output=True, text=True, timeout=300,
            cwd=copy)
        if result.returncode == 0:
            return name, "NOT CAUGHT (tests still pass!)"
        return name, "caught"
    except subprocess.TimeoutExpired:
        return name, "caught (test timed out = red)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if "--list" in sys.argv:
        for mutation in MUTATIONS:
            print(mutation[0])
        return 0
    failures = []
    for name, rel_path, old, new, selector in MUTATIONS:
        print(f"mutating: {name} ... ", end="", flush=True)
        _, outcome = run_mutation(name, rel_path, old, new, selector)
        print(outcome)
        if not outcome.startswith("caught"):
            failures.append((name, outcome))
    print()
    if failures:
        print("MUTATION CHECK FAILED:")
        for name, outcome in failures:
            print(f"  - {name}: {outcome}")
        return 1
    print(f"All {len(MUTATIONS)} mutations caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
