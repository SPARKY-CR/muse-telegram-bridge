"""Tests for poller/hook-script.sh (the review's items C1, C2, M4, L8):
the hook runs against a fake runtime with a stub poller, a real
inbox, and the real scan_inbox.py. This file is the one the v2
review said was missing entirely: hook-script.sh had zero tests,
and both critical bugs lived in it."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 12345

FAKE_RUNTIME = """\
log() { echo "LOG $1 $2" >> "$HOOK_EVENTS"; }
silent() { echo "SILENT $1" >> "$HOOK_EVENTS"; }
wake() {
  echo "WAKE $1" >> "$HOOK_EVENTS"
  printf '%s\\n' "$2" >> "$HOOK_WAKES"
  if [ -n "${HOOK_WAKE_ENV:-}" ]; then
    printf 'TELEGRAM_CONFIG_FILE=%s\\n' "${TELEGRAM_CONFIG_FILE:-}" >> "$HOOK_WAKE_ENV"
    printf 'TELEGRAM_HANDLED_FILE=%s\\n' "${TELEGRAM_HANDLED_FILE:-}" >> "$HOOK_WAKE_ENV"
    printf 'TELEGRAM_ACK_FILE=%s\\n' "${TELEGRAM_ACK_FILE:-}" >> "$HOOK_WAKE_ENV"
    printf 'BRIDGE_CHAT_ID=%s\\n' "${BRIDGE_CHAT_ID:-}" >> "$HOOK_WAKE_ENV"
  fi
}
"""

# Stub poller: takes the poller lock, writes a fresh heartbeat,
# records its argv and its open file descriptors, then sleeps.
# The fd list proves whether fd 9 (the hook lock) leaked in.
STUB_POLLER = """\
import fcntl, json, os, sys, time
args = sys.argv[1:]
rt = args[args.index('--runtime-dir') + 1]
log = args[args.index('--log') + 1]
with open(os.path.join(rt, 'stub-argv.json'), 'w') as f:
    json.dump(args, f)
lock = open(os.path.join(rt, 'poller.lock'), 'a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
with open(os.path.join(rt, 'poller-state.json'), 'w') as f:
    json.dump({'beat_at': time.time(), 'pid': os.getpid(),
               'started_at': time.time(), 'cycles': 0, 'errors': 0}, f)
fds = {}
for fd in os.listdir('/proc/self/fd'):
    try:
        fds[fd] = os.readlink(os.path.join('/proc/self/fd', fd))
    except OSError:
        pass
with open(os.path.join(rt, 'stub-fds.json'), 'w') as f:
    json.dump(fds, f)
with open(os.path.join(rt, 'stub.pid'), 'w') as f:
    f.write(str(os.getpid()))
time.sleep(60)
"""


def _msg(update_id):
    return {"update_id": update_id,
            "message": {"message_id": update_id,
                        "date": int(time.time()),
                        "from": {"id": OWNER, "is_bot": False},
                        "chat": {"id": OWNER, "type": "private"},
                        "text": "hello %d" % update_id}}


def _group_msg(update_id):
    upd = _msg(update_id)
    upd["message"]["chat"] = {"id": -100123, "type": "group"}
    return upd


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = os.path.join(self.tmp.name, "bridge")
        shutil.copytree(REPO, self.bridge,
                        ignore=shutil.ignore_patterns(
                            "tests", "__pycache__", "runtime"))
        self.runtime = os.path.join(self.bridge, "runtime")
        os.makedirs(self.runtime)
        # Stub the poller.
        with open(os.path.join(self.bridge, "poller", "poller.py"),
                  "w") as f:
            f.write(STUB_POLLER)
        # Config + inbox.
        with open(os.path.join(self.runtime, "config.json"), "w") as f:
            json.dump({"chat_id": OWNER}, f)
        with open(os.path.join(self.runtime, "inbox.jsonl"), "w") as f:
            f.write(json.dumps(_msg(1)) + "\n")
        open(os.path.join(self.runtime, "handled.txt"), "w").close()
        # Fake Muse hook runtime.
        self.events = os.path.join(self.tmp.name, "events.log")
        self.wakes = os.path.join(self.tmp.name, "wakes.log")
        self.fake_runtime = os.path.join(self.tmp.name, "runtime.sh")
        with open(self.fake_runtime, "w") as f:
            f.write(FAKE_RUNTIME)
        self.env = dict(os.environ)
        self.env.update({
            "HATCH_HOOK_RUNTIME": self.fake_runtime,
            "HOOK_EVENTS": self.events,
            "HOOK_WAKES": self.wakes,
            "BRIDGE_DIR": self.runtime,
            "BRIDGE_ROOT": self.bridge,
            # Isolate HOME: the hook migrates legacy markers from
            # $HOME/hooks/state/telegram-webhook when the new seen
            # is missing, and the real home holds the LIVE bridge's
            # markers — a test must never read them.
            "HOME": self.tmp.name,
        })
        self.hook = os.path.join(self.bridge, "poller", "hook-script.sh")

    def tearDown(self):
        pid_file = os.path.join(self.runtime, "stub.pid")
        if os.path.exists(pid_file):
            try:
                pid = int(open(pid_file).read().strip())
                os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.tmp.cleanup()

    def _run_hook(self, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", self.hook], capture_output=True, text=True,
            env=env, timeout=60)

    def _wait_for_stub(self, timeout=10):
        deadline = time.time() + timeout
        hb = os.path.join(self.runtime, "poller-state.json")
        while time.time() < deadline:
            if os.path.exists(hb):
                return True
            time.sleep(0.1)
        return False

    def _wake_payloads(self):
        if not os.path.exists(self.wakes):
            return []
        with open(self.wakes) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _events_text(self):
        if not os.path.exists(self.events):
            return ""
        with open(self.events) as f:
            return f.read()


class TestHookSecondRun(HookTestCase):
    def test_second_run_wakes_after_poller_restart(self):
        # C1: after the hook restarts the poller, the NEXT hook
        # run must still work — the poller must not inherit the
        # hook lock (fd 9) and block every later run.
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_stub())
        payloads = self._wake_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(
            [m["update_id"] for m in payloads[0]["new_messages"]], [1])
        # A second message arrives; the poller is still running.
        with open(os.path.join(self.runtime, "inbox.jsonl"), "a") as f:
            f.write(json.dumps(_msg(2)) + "\n")
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("another hook run is active",
                         self._events_text())
        payloads = self._wake_payloads()
        self.assertEqual(len(payloads), 2)
        self.assertEqual(
            [m["update_id"] for m in payloads[1]["new_messages"]], [2])

    def test_poller_does_not_hold_hook_lock(self):
        # C1, directly: no open fd of the poller started by the
        # hook may point at hook.lock.
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_stub())
        with open(os.path.join(self.runtime, "stub-fds.json")) as f:
            fds = json.load(f)
        for target in fds.values():
            self.assertNotIn("hook.lock", target)


class TestHookLocation(HookTestCase):
    def test_copied_hook_fails_without_bridge_root(self):
        # C2: a hook body stored elsewhere must fail loudly,
        # not silently never wake.
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(elsewhere)
        copied = os.path.join(elsewhere, "hook.sh")
        shutil.copy(self.hook, copied)
        env = dict(self.env)
        env.pop("BRIDGE_ROOT", None)
        result = subprocess.run(
            ["bash", copied], capture_output=True, text=True,
            env=env, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("BRIDGE_ROOT", result.stderr)

    def test_copied_hook_works_with_bridge_root(self):
        # C2: with BRIDGE_ROOT set, the copied hook works.
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(elsewhere)
        copied = os.path.join(elsewhere, "hook.sh")
        shutil.copy(self.hook, copied)
        result = subprocess.run(
            ["bash", copied], capture_output=True, text=True,
            env=self.env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_stub())
        self.assertEqual(len(self._wake_payloads()), 1)

    def test_missing_poller_py_fails_loudly(self):
        os.unlink(os.path.join(self.bridge, "poller", "poller.py"))
        result = self._run_hook()
        self.assertEqual(result.returncode, 1)
        self.assertIn("poller.py", result.stderr)

    def test_missing_scan_py_fails_loudly(self):
        os.unlink(os.path.join(self.bridge, "poller", "scan_inbox.py"))
        result = self._run_hook()
        self.assertEqual(result.returncode, 1)
        self.assertIn("scan_inbox.py", result.stderr)


class TestHookChatId(HookTestCase):
    def test_string_chat_id_accepted_and_passed_on(self):
        # M4: a digit-string chat id is valid, and the hook
        # passes the validated id to the poller.
        with open(os.path.join(self.runtime, "config.json"), "w") as f:
            json.dump({"chat_id": "12345"}, f)
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_stub())
        with open(os.path.join(self.runtime, "stub-argv.json")) as f:
            argv = json.load(f)
        self.assertIn("--chat-id", argv)
        self.assertEqual(argv[argv.index("--chat-id") + 1], "12345")
        self.assertEqual(len(self._wake_payloads()), 1)

    def test_invalid_chat_id_fails_loudly(self):
        with open(os.path.join(self.runtime, "config.json"), "w") as f:
            json.dump({"chat_id": "abc"}, f)
        result = self._run_hook()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no valid owner chat id", result.stderr)


class TestDroppedOnlyWake(HookTestCase):
    def test_wakes_when_only_dropped_messages(self):
        # C: a payload with dropped messages and no new ones
        # must still wake the agent, so the owner is told what
        # was lost instead of the hook staying silent.
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_stub())
        self.assertEqual(len(self._wake_payloads()), 1)
        # Age the pending entry to the retry cap: it will be
        # dropped on the next scan, with no new message.
        import time
        pending_path = os.path.join(self.runtime, "pending.json")
        with open(pending_path) as f:
            data = json.load(f)
        entry = data["entries"]["1"]
        entry["attempts"] = 4  # MAX_RETRY_ATTEMPTS
        entry["woken_at"] = time.time() - 4000  # past the backoff
        entry["first_woken_at"] = time.time() - 4000
        with open(pending_path, "w") as f:
            json.dump(data, f)
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        payloads = self._wake_payloads()
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[1]["new_messages"], [])
        self.assertEqual(len(payloads[1]["dropped"]), 1)
        self.assertEqual(payloads[1]["dropped"][0]["update_id"], 1)
        self.assertEqual(payloads[1]["dropped"][0]["message_id"], 1)
        self.assertEqual(payloads[1]["dropped"][0]["text"], "hello 1")


class TestBridgeDirFromRoot(HookTestCase):
    def test_runtime_defaults_to_bridge_root_runtime(self):
        # C2c: with BRIDGE_ROOT set and BRIDGE_DIR unset, the
        # hook must work in $BRIDGE_ROOT/runtime — not a
        # hardcoded path that does not exist here.
        env = dict(self.env)
        env.pop("BRIDGE_DIR", None)
        # The runtime already lives at bridge/runtime (setUp);
        # the hook must find the config and inbox there.
        result = subprocess.run(
            ["bash", self.hook], capture_output=True, text=True,
            env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_stub())
        self.assertEqual(len(self._wake_payloads()), 1)
        self.assertTrue(os.path.exists(
            os.path.join(self.runtime, "seen.json")))
        # The old hardcoded default must not have been created.
        self.assertFalse(os.path.exists(os.path.join(
            self.tmp.name, "workspace", "muse-telegram-bridge",
            "runtime")))

    def test_wake_spawning_background_child_does_not_block_next_run(self):
        # C1b: if the wake spawns a background process, that
        # child must not inherit the hook lock (fd 9) and block
        # every later hook run.
        bg_runtime = os.path.join(self.tmp.name, "runtime-bg.sh")
        with open(bg_runtime, "w") as f:
            f.write(FAKE_RUNTIME.replace(
                'wake() { echo "WAKE $1" >> "$HOOK_EVENTS"; '
                'printf \'%s\\n\' "$2" >> "$HOOK_WAKES"; }',
                'wake() { echo "WAKE $1" >> "$HOOK_EVENTS"; '
                'printf \'%s\\n\' "$2" >> "$HOOK_WAKES"; '
                '(sleep 5 &); }'))
        env = dict(self.env)
        env["HATCH_HOOK_RUNTIME"] = bg_runtime
        result = subprocess.run(
            ["bash", self.hook], capture_output=True, text=True,
            env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._wake_payloads()), 1)
        # A second message arrives while the background child
        # from the first wake is still alive.
        with open(os.path.join(self.runtime, "inbox.jsonl"), "a") as f:
            f.write(json.dumps(_msg(2)) + "\n")
        result = subprocess.run(
            ["bash", self.hook], capture_output=True, text=True,
            env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("another hook run is active",
                         self._events_text())
        self.assertEqual(len(self._wake_payloads()), 2)


class TestLegacyMigration(HookTestCase):
    def test_legacy_seen_is_migrated_once(self):
        # B: on upgrade from a pre-v3 install, the hook copies
        # the old markers (from the Muse hook state dir) into
        # the new location, so the last day of messages is not
        # re-woken.
        legacy = os.path.join(self.tmp.name, "hooks", "state",
                              "telegram-webhook")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "seen.json"), "w") as f:
            json.dump({"last_update_id": 1}, f)
        with open(os.path.join(legacy, "pending.json"), "w") as f:
            json.dump({"entries": {}}, f)
        # The inbox holds the already-seen message 1 and a fresh
        # message 2; only 2 may wake after the migration.
        with open(os.path.join(self.runtime, "inbox.jsonl"), "a") as f:
            f.write(json.dumps(_msg(2)) + "\n")
        self.assertFalse(os.path.exists(
            os.path.join(self.runtime, "seen.json")))
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(
            os.path.join(self.runtime, "seen.json")))
        self.assertIn("migrated", self._events_text())
        payloads = self._wake_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(
            [m["update_id"] for m in payloads[0]["new_messages"]], [2])

    def test_no_migration_when_new_seen_exists(self):
        # The migration must not overwrite markers that are
        # already in the new location: a legacy marker of 99
        # must not suppress the pending message 1.
        legacy = os.path.join(self.tmp.name, "hooks", "state",
                              "telegram-webhook")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "seen.json"), "w") as f:
            json.dump({"last_update_id": 99}, f)
        with open(os.path.join(self.runtime, "seen.json"), "w") as f:
            json.dump({"last_update_id": 0}, f)
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("migrated", self._events_text())
        payloads = self._wake_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(
            [m["update_id"] for m in payloads[0]["new_messages"]], [1])


class TestHookFlockErrors(HookTestCase):
    def _fake_flock(self, mode):
        # A flock shim: "hook-lock-fails" makes the fd form
        # (the hook's own lock) fail with a real error code;
        # "poller-check-fails" makes only the poller-lock probe
        # fail, while the fd form succeeds.
        bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        script = os.path.join(bin_dir, "flock")
        with open(script, "w") as f:
            f.write("#!/usr/bin/env bash\n")
            if mode == "hook-lock-fails":
                f.write("if [[ \"$*\" == *' 9' ]]; then exit 42; fi\n")
            else:
                f.write("if [[ \"$*\" == *'poller.lock'* ]]; then "
                        "exit 42; fi\n")
            f.write("exec /usr/bin/flock \"$@\"\n")
        os.chmod(script, 0o755)
        return bin_dir

    def test_hook_lock_error_is_loud_not_silent(self):
        # L8: a real flock error on the hook lock must exit 1,
        # not be mistaken for "another hook run is active".
        bin_dir = self._fake_flock("hook-lock-fails")
        env = dict(self.env)
        env["PATH"] = bin_dir + os.pathsep + env["PATH"]
        result = subprocess.run(
            ["bash", self.hook], capture_output=True, text=True,
            env=env, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot take hook lock", result.stderr)

    def test_poller_lock_error_still_attempts_start(self):
        # L8: a failed poller-lock probe must not be read as
        # "running" — the hook still attempts a start (the
        # poller's own flock prevents a duplicate).
        bin_dir = self._fake_flock("poller-check-fails")
        env = dict(self.env)
        env["PATH"] = bin_dir + os.pathsep + env["PATH"]
        result = subprocess.run(
            ["bash", self.hook], capture_output=True, text=True,
            env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_stub())
        self.assertIn("lock check failed", self._events_text())


class TestHookGroups(HookTestCase):
    def test_allow_groups_reaches_poller_and_scan(self):
        # BRIDGE_ALLOW_GROUPS must reach BOTH the poller (so
        # group updates are claimed) and the scan (so they wake).
        with open(os.path.join(self.runtime, "inbox.jsonl"), "w") as f:
            f.write(json.dumps(_group_msg(1)) + "\n")
        result = self._run_hook({"BRIDGE_ALLOW_GROUPS": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_stub())
        with open(os.path.join(self.runtime, "stub-argv.json")) as f:
            argv = json.load(f)
        self.assertIn("--allow-groups", argv)
        payloads = self._wake_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["new_messages"][0]["chat_id"],
                         -100123)

    def test_group_ignored_without_flag(self):
        with open(os.path.join(self.runtime, "inbox.jsonl"), "w") as f:
            f.write(json.dumps(_group_msg(1)) + "\n")
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._wake_payloads(), [])


class TestHookHomeUnset(HookTestCase):
    def test_hook_runs_without_home(self):
        # set -u + unset HOME must not kill the hook at the
        # legacy-migration default.
        env = dict(self.env)
        env.pop("HOME", None)
        result = subprocess.run(
            ["bash", self.hook], capture_output=True, text=True,
            env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._wake_payloads()), 1)


class TestHookScanFailure(HookTestCase):
    def test_scan_failure_logged_and_notified_once(self):
        # A failing scan logs its stderr and wakes the agent
        # once after 3 consecutive failures, not silently
        # forever and not on every run.
        with open(os.path.join(self.bridge, "poller", "scan_inbox.py"),
                  "w") as f:
            f.write("import sys\n"
                    "print('boom: scan exploded', file=sys.stderr)\n"
                    "sys.exit(1)\n")
        for _ in range(3):
            result = self._run_hook()
            self.assertEqual(result.returncode, 0, result.stderr)
        events = self._events_text()
        self.assertIn("boom: scan exploded", events)
        self.assertIn("consecutive_failures", events)
        payloads = self._wake_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertIn("scan_error", payloads[0])
        # A fourth failure does not wake again.
        self._run_hook()
        self.assertEqual(len(self._wake_payloads()), 1)

    def test_scan_failure_wake_exports_custom_config_env(self):
        # The scan-failure wake must see the same TELEGRAM_*
        # exports as a normal wake, including a custom
        # BRIDGE_CONFIG path.
        with open(os.path.join(self.bridge, "poller", "scan_inbox.py"),
                  "w") as f:
            f.write("import sys\n"
                    "print('boom: scan exploded', file=sys.stderr)\n"
                    "sys.exit(1)\n")
        custom_config = os.path.join(self.tmp.name, "custom.json")
        with open(custom_config, "w") as f:
            json.dump({"chat_id": OWNER}, f)
        wake_env = os.path.join(self.tmp.name, "wake-env.log")
        extra = {"BRIDGE_CONFIG": custom_config,
                 "HOOK_WAKE_ENV": wake_env}
        for _ in range(3):
            result = self._run_hook(extra_env=extra)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._wake_payloads()), 1)
        with open(wake_env) as f:
            env_text = f.read()
        self.assertIn(f"TELEGRAM_CONFIG_FILE={custom_config}",
                      env_text)
        self.assertIn(
            "TELEGRAM_HANDLED_FILE="
            + os.path.join(self.runtime, "handled.txt"), env_text)
        self.assertIn(f"BRIDGE_CHAT_ID={OWNER}", env_text)


class TestHookStuckPoller(HookTestCase):
    def _stub_pid(self):
        with open(os.path.join(self.runtime, "stub.pid")) as f:
            return int(f.read().strip())

    def _set_beat_age(self, age_secs):
        hb_path = os.path.join(self.runtime, "poller-state.json")
        with open(hb_path) as f:
            hb = json.load(f)
        hb["beat_at"] = time.time() - age_secs
        with open(hb_path, "w") as f:
            json.dump(hb, f)

    def test_stuck_poller_killed_and_restarted(self):
        # The full chain: poller running (lock held) but its
        # heartbeat is stale past the 120s threshold -> the hook
        # verifies the pid, kills it, restarts a fresh poller,
        # and still wakes for the new message.
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_stub())
        first_pid = self._stub_pid()
        # Fresh heartbeat (30s old): no kill.
        self._set_beat_age(30)
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._stub_pid(), first_pid)
        # Stale heartbeat (past the 120s threshold): kill+restart.
        self._set_beat_age(300)
        with open(os.path.join(self.runtime, "inbox.jsonl"), "a") as f:
            f.write(json.dumps(_msg(2)) + "\n")
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hung poller killed", self._events_text())
        self.assertTrue(self._wait_for(
            lambda: self._stub_pid() != first_pid))
        with self.assertRaises(OSError):
            os.kill(first_pid, 0)
        payloads = self._wake_payloads()
        self.assertEqual(
            [m["update_id"] for m in payloads[-1]["new_messages"]],
            [2])

    def _wait_for(self, predicate, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False


class TestHookDroppedRedelivery(HookTestCase):
    def test_dropped_notice_repeats_until_confirmed(self):
        # Hook-level end to end: an immediate rerun does not
        # duplicate the dropped notice; after the retry
        # interval it wakes again when the agent never confirmed
        # it, and stops once the drop key is marked handled.
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_stub())
        pending_path = os.path.join(self.runtime, "pending.json")
        with open(pending_path) as f:
            data = json.load(f)
        entry = data["entries"]["1"]
        entry["attempts"] = 4
        entry["woken_at"] = time.time() - 4000
        entry["first_woken_at"] = time.time() - 4000
        with open(pending_path, "w") as f:
            json.dump(data, f)
        self._run_hook()  # drops, wakes with the notice
        self._run_hook()  # immediate rerun: no duplicate notice
        payloads = self._wake_payloads()
        self.assertEqual(len(payloads), 2)
        self.assertEqual(len(payloads[1]["dropped"]), 1)
        with open(pending_path) as f:
            data = json.load(f)
        data["entries"]["1"]["dropped_notified_at"] = \
            time.time() - 601
        with open(pending_path, "w") as f:
            json.dump(data, f)
        self._run_hook()  # retry interval elapsed: notice returns
        payloads = self._wake_payloads()
        self.assertEqual(len(payloads), 3)
        self.assertEqual(len(payloads[2]["dropped"]), 1)
        with open(os.path.join(self.runtime, "handled.txt"), "a") as f:
            f.write(f"{OWNER}:drop:1\n")
        self._run_hook()
        self.assertEqual(len(self._wake_payloads()), 3)


if __name__ == "__main__":
    unittest.main()