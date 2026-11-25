"""Subprocess tests for poller/typing_keeper.py and
poller/stuck_watchdog.py with short intervals (the v2 review's
test gap #4): the REAL scripts run against a FAKE telegram module
that logs every call."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 12345
GROUP = -1001111111111

FAKE_TELEGRAM = '''\
"""Fake telegram module: logs every call, always succeeds."""
import json
import os


def call(method, params=None, timeout=30):
    path = os.environ.get("FAKE_TELEGRAM_CALLS", "/dev/null")
    try:
        with open(path, "a") as f:
            f.write(json.dumps({"method": method, "params": params}) + "\\n")
    except OSError:
        pass
    return {"ok": True, "result": {}}
'''


class SignalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.bin_dir = os.path.join(d, "bin")
        self.poller_dir = os.path.join(d, "poller")
        self.runtime = os.path.join(d, "runtime")
        os.makedirs(self.bin_dir)
        os.makedirs(self.poller_dir)
        os.makedirs(self.runtime)
        with open(os.path.join(self.bin_dir, "telegram.py"), "w") as f:
            f.write(FAKE_TELEGRAM)
        for name in ("typing_keeper.py", "stuck_watchdog.py",
                     "bridge_common.py", "poller.py"):
            shutil.copy(os.path.join(REPO, "poller", name),
                        os.path.join(self.poller_dir, name))
        self.handled = os.path.join(self.runtime, "handled.txt")
        open(self.handled, "w").close()
        self.calls = os.path.join(d, "calls.jsonl")
        self.env = dict(os.environ)
        self.env["FAKE_TELEGRAM_CALLS"] = self.calls
        self.procs = []
        self.detached_pids = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for pid in self.detached_pids:
            if self._pid_alive(pid):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
        self.tmp.cleanup()

    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _start_detached_via_poller(self, script, chat_id, key):
        log = os.path.join(self.runtime, "detached.log")
        code = (
            "import os, sys\n"
            "sys.path.insert(0, os.environ['FAKE_POLLER_DIR'])\n"
            "import poller\n"
            "proc = poller.start_detached("
            "os.environ['FAKE_SCRIPT'], "
            "int(os.environ['FAKE_CHAT_ID']), "
            "os.environ['FAKE_KEY'], "
            "os.environ['FAKE_RUNTIME'], "
            "os.environ['FAKE_LOG'])\n"
            "print(proc.pid if proc is not None else '')\n")
        env = dict(self.env)
        env.update({
            "FAKE_POLLER_DIR": self.poller_dir,
            "FAKE_SCRIPT": script,
            "FAKE_CHAT_ID": str(chat_id),
            "FAKE_KEY": key,
            "FAKE_RUNTIME": self.runtime,
            "FAKE_LOG": log,
        })
        result = subprocess.run(
            [sys.executable, "-c", code], env=env,
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        pid_text = result.stdout.strip()
        self.assertTrue(pid_text.isdigit(),
                        result.stdout + result.stderr)
        pid = int(pid_text)
        self.detached_pids.append(pid)
        return pid

    def _log_contains(self, path, text):
        if not os.path.exists(path):
            return False
        with open(path) as f:
            return text in f.read()

    def _start(self, script, key, *args):
        proc = subprocess.Popen(
            [sys.executable, os.path.join(self.poller_dir, script),
             str(OWNER), key, "--runtime-dir", self.runtime, *args],
            env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.procs.append(proc)
        return proc

    def _calls(self, method=None):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            calls = [json.loads(line) for line in f if line.strip()]
        if method:
            calls = [c for c in calls if c["method"] == method]
        return calls

    def _wait_for(self, predicate, timeout=10, interval=0.05):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def _mark_handled(self, key):
        with open(self.handled, "a") as f:
            f.write(key + "\n")


class TestTypingKeeper(SignalCase):
    def test_keeps_typing_until_handled(self):
        key = f"{OWNER}:7"
        proc = self._start("typing_keeper.py", key,
                           "--interval", "0.3", "--max-secs", "30")
        self.assertTrue(self._wait_for(
            lambda: len(self._calls("sendChatAction")) >= 2, timeout=5))
        self._mark_handled(key)
        code = proc.wait(timeout=10)
        self.assertEqual(code, 0)
        # No more typing after the message was handled.
        count = len(self._calls("sendChatAction"))
        time.sleep(0.8)
        self.assertEqual(len(self._calls("sendChatAction")), count)

    def test_exits_at_max_secs(self):
        key = f"{OWNER}:8"
        proc = self._start("typing_keeper.py", key,
                           "--interval", "0.3", "--max-secs", "1")
        code = proc.wait(timeout=10)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(self._calls("sendChatAction")), 1)

    def test_second_keeper_for_same_key_exits(self):
        key = f"{OWNER}:9"
        first = self._start("typing_keeper.py", key,
                            "--interval", "0.3", "--max-secs", "30")
        self.assertTrue(self._wait_for(
            lambda: len(self._calls("sendChatAction")) >= 1, timeout=5))
        second = self._start("typing_keeper.py", key,
                             "--interval", "0.3", "--max-secs", "30")
        code = second.wait(timeout=10)
        self.assertEqual(code, 0)
        log = os.path.join(self.runtime, "typing_keeper.log")
        with open(log) as f:
            self.assertIn("already running", f.read())
        self._mark_handled(key)
        first.wait(timeout=10)


class TestStuckWatchdog(SignalCase):
    def test_hint_after_threshold_then_exits_when_handled(self):
        key = f"{OWNER}:11"
        proc = self._start("stuck_watchdog.py", key,
                           "--threshold", "0.5", "--interval", "0.2",
                           "--max-secs", "30")
        self.assertTrue(self._wait_for(
            lambda: len(self._calls("sendMessage")) >= 1, timeout=5))
        hint = self._calls("sendMessage")[0]["params"]
        self.assertEqual(hint["reply_to_message_id"], 11)
        self.assertTrue(hint["allow_sending_without_reply"])
        # Exactly one hint, even if the message stays unhandled.
        time.sleep(0.8)
        self.assertEqual(len(self._calls("sendMessage")), 1)
        self._mark_handled(key)
        code = proc.wait(timeout=10)
        self.assertEqual(code, 0)

    def test_no_hint_when_handled_before_threshold(self):
        key = f"{OWNER}:12"
        proc = self._start("stuck_watchdog.py", key,
                           "--threshold", "30", "--interval", "0.2",
                           "--max-secs", "30")
        self._mark_handled(key)
        code = proc.wait(timeout=10)
        self.assertEqual(code, 0)
        self.assertEqual(self._calls("sendMessage"), [])

    def test_bad_key_exits_2(self):
        proc = self._start("stuck_watchdog.py", "not-a-key",
                           "--threshold", "1", "--interval", "0.2",
                           "--max-secs", "5")
        code = proc.wait(timeout=10)
        self.assertEqual(code, 2)


class TestGroupDetachedSignals(SignalCase):
    def test_group_typing_keeper_starts_via_poller(self):
        # Group mode: poller.start_detached must put options
        # before "--" so argparse accepts the negative chat id
        # and the "-100...:7" key instead of exiting rc=2.
        key = f"{GROUP}:7"
        pid = self._start_detached_via_poller(
            "typing_keeper.py", GROUP, key)
        self.assertTrue(self._wait_for(
            lambda: len(self._calls("sendChatAction")) >= 1,
            timeout=5))
        self.assertEqual(
            self._calls("sendChatAction")[0]["params"]["chat_id"],
            str(GROUP))
        self._mark_handled(key)
        log = os.path.join(self.runtime, "typing_keeper.log")
        self.assertTrue(self._wait_for(
            lambda: self._log_contains(log, f"stop {key} handled"),
            timeout=10))
        self.assertTrue(self._wait_for(
            lambda: not self._pid_alive(pid), timeout=5))

    def test_group_stuck_watchdog_starts_via_poller(self):
        key = f"{GROUP}:7"
        pid = self._start_detached_via_poller(
            "stuck_watchdog.py", GROUP, key)
        log = os.path.join(self.runtime, "stuck_watchdog.log")
        self.assertTrue(self._wait_for(
            lambda: self._log_contains(log, f"start {key}"),
            timeout=5))
        self.assertTrue(self._pid_alive(pid))
        self._mark_handled(key)
        # The watchdog's default poll interval is 15s; handled
        # is noticed on the next cycle and it exits cleanly.
        self.assertTrue(self._wait_for(
            lambda: self._log_contains(log, f"stop {key} handled"),
            timeout=20))
        self.assertTrue(self._wait_for(
            lambda: not self._pid_alive(pid), timeout=5))


if __name__ == "__main__":
    unittest.main()
