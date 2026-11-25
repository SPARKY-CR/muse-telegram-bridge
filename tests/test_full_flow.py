"""Full-flow integration test (the v3 review's test gap 7): the REAL
poller claims a message, the REAL hook scans and wakes, the agent
marks it handled with the REAL helper, and the REAL typing keeper
stops. The only stubs are the Telegram API itself (the scripted
fake module from test_poller_subprocess) and the hook's wake
function, which records the payload instead of spawning an agent.
Nothing else is faked: poller.py, scan_inbox.py, hook-script.sh,
typing_keeper.py, stuck_watchdog.py and bin/telegram.py are the
real files, copied from the repo."""
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from test_poller_subprocess import FAKE_TELEGRAM
    from test_hook import FAKE_RUNTIME
except ImportError:  # run as tests.test_full_flow
    from tests.test_poller_subprocess import FAKE_TELEGRAM
    from tests.test_hook import FAKE_RUNTIME


def _msg(update_id):
    return {"update_id": update_id,
            "message": {"message_id": update_id,
                        "date": int(time.time()),
                        "from": {"id": OWNER, "is_bot": False},
                        "chat": {"id": OWNER, "type": "private"},
                        "text": "hello %d" % update_id}}


class TestFullFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        # A full copy of the repo, with the fake Telegram API in
        # bin/ (what the poller and keeper import) and the REAL
        # helper kept aside for the agent's mark_handled step.
        self.bridge = os.path.join(d, "bridge")
        shutil.copytree(REPO, self.bridge,
                        ignore=shutil.ignore_patterns(
                            "tests", "__pycache__", "runtime"))
        with open(os.path.join(self.bridge, "bin", "telegram.py"),
                  "w") as f:
            f.write(FAKE_TELEGRAM)
        # OUTSIDE the bridge copy on purpose: the helper's
        # REPO_DIR is this dir's parent, so its default runtime
        # (REPO_DIR/runtime) is a DIFFERENT directory from the
        # test runtime. That is what makes the C2c mutation
        # (ignoring BRIDGE_DIR) visible: with it, mark_handled
        # writes to the wrong handled.txt and the keeper never
        # stops. bridge_common is found via PYTHONPATH instead
        # of the helper's ../poller relative import.
        self.real_bin = os.path.join(d, "real_bin")
        os.makedirs(self.real_bin)
        shutil.copy(os.path.join(REPO, "bin", "telegram.py"),
                    os.path.join(self.real_bin, "telegram.py"))
        self.runtime = os.path.join(self.bridge, "runtime")
        os.makedirs(self.runtime)
        with open(os.path.join(self.runtime, "config.json"), "w") as f:
            json.dump({"chat_id": OWNER}, f)
        with open(os.path.join(self.runtime, "state.json"), "w") as f:
            json.dump({"offset": 0}, f)
        open(os.path.join(self.runtime, "inbox.jsonl"), "w").close()
        open(os.path.join(self.runtime, "handled.txt"), "w").close()
        self.events = os.path.join(d, "events.log")
        self.wakes = os.path.join(d, "wakes.log")
        self.fake_runtime = os.path.join(d, "runtime.sh")
        with open(self.fake_runtime, "w") as f:
            f.write(FAKE_RUNTIME)
        self.calls = os.path.join(d, "calls.jsonl")
        self.signals = os.path.join(d, "signals.jsonl")
        self.scenario = os.path.join(d, "scenario.json")
        self.hook = os.path.join(self.bridge, "poller", "hook-script.sh")
        self.env = dict(os.environ)
        self.env.update({
            "HATCH_HOOK_RUNTIME": self.fake_runtime,
            "HOOK_EVENTS": self.events,
            "HOOK_WAKES": self.wakes,
            "BRIDGE_DIR": self.runtime,
            "BRIDGE_ROOT": self.bridge,
            # Isolate HOME: the hook migrates legacy markers from
            # $HOME/hooks/state/telegram-webhook, and the real
            # home holds the LIVE bridge's markers.
            "HOME": d,
        })
        self.proc = None

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        # The poller detaches the keeper and watchdog into their
        # own sessions; kill anything still running from this
        # test's temp dir (best-effort).
        subprocess.run(["pkill", "-f", self.tmp.name],
                       capture_output=True)
        self.tmp.cleanup()

    def _wait_for(self, predicate, timeout=10, interval=0.1):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def _inbox_updates(self):
        try:
            with open(os.path.join(self.runtime, "inbox.jsonl")) as f:
                return [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            return []

    def _calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _wake_payloads(self):
        if not os.path.exists(self.wakes):
            return []
        with open(self.wakes) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _run_hook(self):
        return subprocess.run(["bash", self.hook], capture_output=True,
                              text=True, env=self.env, timeout=60)

    def test_poller_to_keeper_full_flow(self):
        # 1. The real poller claims a message from the fake API.
        with open(self.scenario, "w") as f:
            json.dump({"getUpdates": [{"ok": True, "result": [_msg(7)],
                                       "sleep": 0}]}, f)
        env = dict(os.environ)
        env.update({"FAKE_TELEGRAM_SCENARIO": self.scenario,
                    "FAKE_TELEGRAM_CALLS": self.calls,
                    "FAKE_SIGNAL_LOG": self.signals})
        self.proc = subprocess.Popen(
            [sys.executable,
             os.path.join(self.bridge, "poller", "poller.py"),
             "--config", os.path.join(self.runtime, "config.json"),
             "--state", os.path.join(self.runtime, "state.json"),
             "--inbox", os.path.join(self.runtime, "inbox.jsonl"),
             "--log", os.path.join(self.runtime, "poller.log"),
             "--runtime-dir", self.runtime,
             "--heartbeat-secs", "1"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertTrue(self._wait_for(
            lambda: [u["update_id"] for u in self._inbox_updates()]
            == [7], timeout=10))
        with open(os.path.join(self.runtime, "state.json")) as f:
            self.assertEqual(json.load(f)["offset"], 8)

        # 2. The real keeper started typing (sendChatAction).
        self.assertTrue(self._wait_for(
            lambda: "sendChatAction" in
            [c["method"] for c in self._calls()], timeout=10))

        # 3. The real hook scans the inbox and wakes the agent.
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        payloads = self._wake_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(
            [m["update_id"] for m in payloads[0]["new_messages"]], [7])

        # 4. The agent marks the message handled with the real
        # helper (a local file write; no Telegram token needed).
        mark_env = dict(os.environ)
        mark_env["BRIDGE_DIR"] = self.runtime
        mark_env["PYTHONPATH"] = os.path.join(self.bridge, "poller")
        result = subprocess.run(
            [sys.executable,
             os.path.join(self.real_bin, "telegram.py"),
             "mark_handled", "%d:7" % OWNER],
            capture_output=True, text=True, env=mark_env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.runtime, "handled.txt")) as f:
            self.assertIn("%d:7" % OWNER, f.read())

        # 5. The real keeper notices and stops typing.
        keeper_log = os.path.join(self.runtime, "typing_keeper.log")

        def _keeper_stopped():
            try:
                with open(keeper_log) as f:
                    return "stop %d:7 handled" % OWNER in f.read()
            except OSError:
                return False

        self.assertTrue(self._wait_for(_keeper_stopped, timeout=15))
        typing_calls = [c for c in self._calls()
                        if c["method"] == "sendChatAction"]
        time.sleep(5)
        self.assertEqual(
            [c for c in self._calls()
             if c["method"] == "sendChatAction"], typing_calls,
            "the keeper kept typing after the message was handled")

        # 6. A second hook run stays silent: the message is
        # handled and the seen marker advanced past it.
        result = self._run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._wake_payloads()), 1)


if __name__ == "__main__":
    unittest.main()
