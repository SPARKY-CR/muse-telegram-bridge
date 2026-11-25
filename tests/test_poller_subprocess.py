"""Subprocess tests for poller/poller.py (the review's items M1, M2,
M3, L6): the REAL poller runs as a subprocess against a FAKE
telegram module (scripted getUpdates responses, recorded calls)
and stub keeper/watchdog scripts. This is the test the v2 review
said was missing: poller.main() never ran anywhere in v1/v2."""
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 12345

FAKE_TELEGRAM = '''\
"""Fake telegram module for poller subprocess tests.

Behavior is scripted by a scenario JSON file (env
FAKE_TELEGRAM_SCENARIO): {"getUpdates": [step, ...]} where each
step is {"ok": true, "result": [...], "sleep": N} or
{"raise": "429", "retry_after": N} / {"raise": "409"}.
Every call is logged as one JSON line to FAKE_TELEGRAM_CALLS.
"""
import io
import json
import os
import time
import urllib.error

_calls_path = os.environ.get("FAKE_TELEGRAM_CALLS", "/dev/null")
_scenario_path = os.environ.get("FAKE_TELEGRAM_SCENARIO", "")
try:
    with open(_scenario_path) as _f:
        _steps = json.load(_f).get("getUpdates", [])
except Exception:
    _steps = []
_index = [0]


def _log(method, params):
    try:
        with open(_calls_path, "a") as f:
            f.write(json.dumps({"method": method, "params": params}) + "\\n")
    except OSError:
        pass


def call(method, params=None, timeout=30):
    _log(method, params)
    if method != "getUpdates":
        return {"ok": True, "result": {}}
    if _index[0] < len(_steps):
        step = _steps[_index[0]]
        _index[0] += 1
    else:
        step = {"ok": True, "result": [], "sleep": 0.2}
    if "raise" in step:
        code = int(step["raise"])
        body = json.dumps(
            {"ok": False, "error_code": code,
             "parameters": {"retry_after": step.get("retry_after", 1)}})
        raise urllib.error.HTTPError(
            "http://fake", code, "Fake", {}, io.BytesIO(body.encode()))
    time.sleep(step.get("sleep", 0))
    return {"ok": step.get("ok", True), "result": step.get("result", [])}


def http_retry_after(exc):
    if not isinstance(exc, urllib.error.HTTPError) or exc.code != 429:
        return None
    try:
        body = exc.read().decode("utf-8", errors="replace")
        return json.loads(body)["parameters"]["retry_after"]
    except Exception:
        return None
'''

STUB_SIGNAL = """\
import json, os, sys
log = os.path.join(os.environ["FAKE_SIGNAL_LOG"])
with open(log, "a") as f:
    f.write(json.dumps({"script": os.path.basename(sys.argv[0]),
                        "args": sys.argv[1:]}) + "\\n")
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


class PollerSubprocessCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.bin_dir = os.path.join(d, "bin")
        self.poller_dir = os.path.join(d, "poller")
        self.runtime = os.path.join(d, "runtime")
        os.makedirs(self.bin_dir)
        os.makedirs(self.poller_dir)
        os.makedirs(self.runtime)
        # Fake telegram module + real poller code.
        with open(os.path.join(self.bin_dir, "telegram.py"), "w") as f:
            f.write(FAKE_TELEGRAM)
        for name in ("poller.py", "bridge_common.py"):
            shutil.copy(os.path.join(REPO, "poller", name),
                        os.path.join(self.poller_dir, name))
        for name in ("typing_keeper.py", "stuck_watchdog.py"):
            with open(os.path.join(self.poller_dir, name), "w") as f:
                f.write(STUB_SIGNAL)
        self.config = os.path.join(self.runtime, "config.json")
        self.state = os.path.join(self.runtime, "state.json")
        self.inbox = os.path.join(self.runtime, "inbox.jsonl")
        self.log = os.path.join(self.runtime, "poller.log")
        self.heartbeat = os.path.join(self.runtime, "poller-state.json")
        self.calls = os.path.join(d, "calls.jsonl")
        self.signals = os.path.join(d, "signals.jsonl")
        self.scenario = os.path.join(d, "scenario.json")
        with open(self.config, "w") as f:
            json.dump({"chat_id": OWNER}, f)
        with open(self.state, "w") as f:
            json.dump({"offset": 0}, f)
        open(self.inbox, "w").close()
        self.proc = None

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.tmp.cleanup()

    def _start(self, scenario=None, extra_args=None):
        with open(self.scenario, "w") as f:
            json.dump({"getUpdates": scenario or []}, f)
        env = dict(os.environ)
        env.update({"FAKE_TELEGRAM_SCENARIO": self.scenario,
                    "FAKE_TELEGRAM_CALLS": self.calls,
                    "FAKE_SIGNAL_LOG": self.signals})
        cmd = [sys.executable, os.path.join(self.poller_dir, "poller.py"),
               "--config", self.config, "--state", self.state,
               "--inbox", self.inbox, "--log", self.log,
               "--runtime-dir", self.runtime,
               "--heartbeat-secs", "1"]
        if extra_args:
            cmd.extend(extra_args)
        self.proc = subprocess.Popen(cmd, env=env,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        return self.proc

    def _wait_for(self, predicate, timeout=10, interval=0.1):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def _heartbeat(self):
        try:
            with open(self.heartbeat) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _log_text(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log) as f:
            return f.read()


class TestPollerSubprocess(PollerSubprocessCase):
    def test_heartbeat_within_2_seconds(self):
        # M1: the first heartbeat must exist almost at once,
        # even while the first poll blocks.
        start = time.time()
        self._start(scenario=[{"ok": True, "result": [], "sleep": 30}])
        self.assertTrue(self._wait_for(
            lambda: self._heartbeat() is not None, timeout=5))
        self.assertLess(time.time() - start, 2)
        hb = self._heartbeat()
        self.assertEqual(hb["pid"], self.proc.pid)

    def test_sigterm_exits_within_3_seconds(self):
        # M2: SIGTERM during a blocked poll must end the process
        # fast (the handler raises SystemExit inside the poll).
        self._start(scenario=[{"ok": True, "result": [], "sleep": 30}])
        self.assertTrue(self._wait_for(
            lambda: self._heartbeat() is not None, timeout=5))
        start = time.time()
        self.proc.send_signal(signal.SIGTERM)
        code = self.proc.wait(timeout=10)
        self.assertLess(time.time() - start, 3)
        self.assertEqual(code, 0)

    def test_heartbeat_advances_during_429_backoff(self):
        # M1: a 429 backoff (up to 300s) must not make the poller
        # look stale — the heartbeat thread keeps beating.
        self._start(scenario=[{"raise": "429", "retry_after": 30}])
        self.assertTrue(self._wait_for(
            lambda: self._heartbeat() is not None, timeout=5))
        first = self._heartbeat()["beat_at"]
        self.assertTrue(self._wait_for(
            lambda: (self._heartbeat() or {}).get("beat_at", 0)
            > first + 0.5, timeout=5))
        self.assertIn("rate-limited", self._log_text())

    def test_409_backoff_keeps_poller_alive(self):
        # L6: a 409 is detected by exc.code and backs off. The
        # log must name the 30s backoff, not just "409" — the
        # generic failure path also prints 409 (in the exception
        # text), so checking only for "409" proves nothing.
        self._start(scenario=[{"raise": "409"}])
        self.assertTrue(self._wait_for(
            lambda: "backing off 30s" in self._log_text(), timeout=5))
        self.assertIsNone(self.proc.poll())

    def test_heartbeat_goes_stale_when_main_loop_stuck(self):
        # D: a main loop stuck in a long getUpdates must go
        # stale — the heartbeat thread only beats until the
        # progress deadline (poll-timeout + grace), then stops.
        # The fake call sleeps 30s; with a 3s poll timeout and
        # 1s grace the beat must freeze ~4s after the poll
        # started, even though the poller is still alive.
        self._start(scenario=[{"ok": True, "result": [], "sleep": 30}],
                    extra_args=["--poll-timeout", "3",
                                "--progress-grace-secs", "1"])
        self.assertTrue(self._wait_for(
            lambda: self._heartbeat() is not None, timeout=5))
        started = self._heartbeat()["started_at"]
        time.sleep(7 - (time.time() - started))
        beat_first = self._heartbeat()["beat_at"]
        time.sleep(3)
        beat_second = self._heartbeat()["beat_at"]
        # The poller is still alive (stuck in the call), but its
        # beat froze at the deadline instead of advancing.
        self.assertIsNone(self.proc.poll())
        self.assertEqual(beat_first, beat_second)
        self.assertLess(beat_first - started, 6)

    def test_claims_message_and_signals(self):
        self._start(scenario=[{"ok": True, "result": [_msg(7)],
                               "sleep": 0}])
        self.assertTrue(self._wait_for(
            lambda: os.path.exists(self.inbox)
            and os.path.getsize(self.inbox) > 0, timeout=5))
        with open(self.inbox) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual([u["update_id"] for u in lines], [7])
        with open(self.state) as f:
            self.assertEqual(json.load(f)["offset"], 8)
        # The read receipt was sent and the keeper/watchdog ran.
        methods = [c["method"] for c in self._calls()]
        self.assertIn("setMessageReaction", methods)
        self.assertTrue(self._wait_for(
            lambda: os.path.exists(self.signals), timeout=5))

    def test_group_message_claimed_only_with_allow_groups(self):
        # BRIDGE_ALLOW_GROUPS end to end: with --allow-groups
        # the poller claims the owner's group message and the
        # keeper/watchdog target the GROUP chat.
        self._start(scenario=[{"ok": True, "result": [_group_msg(7)],
                               "sleep": 0}],
                    extra_args=["--allow-groups"])
        self.assertTrue(self._wait_for(
            lambda: os.path.exists(self.inbox)
            and os.path.getsize(self.inbox) > 0, timeout=5))
        with open(self.inbox) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual([u["update_id"] for u in lines], [7])
        self.assertIn("received_at", lines[0])
        self.assertTrue(self._wait_for(
            lambda: os.path.exists(self.signals), timeout=5))
        with open(self.signals) as f:
            signals = [json.loads(line) for line in f if line.strip()]
        self.assertTrue(signals)
        for signal in signals:
            args = signal["args"]
            self.assertIn("--", args)
            self.assertLess(args.index("--runtime-dir"),
                            args.index("--"))
            self.assertEqual(args[-2], "-100123")
            self.assertEqual(args[-1], "-100123:7")
        reactions = [c for c in self._calls()
                     if c["method"] == "setMessageReaction"]
        self.assertEqual(reactions[0]["params"]["chat_id"], -100123)

    def test_group_message_ignored_without_flag(self):
        self._start(scenario=[{"ok": True, "result": [_group_msg(7)],
                               "sleep": 0}])
        self.assertTrue(self._wait_for(
            lambda: "claimed 1 updates (0 owner)" in self._log_text(),
            timeout=5))
        self.assertEqual(os.path.getsize(self.inbox), 0)

    def test_offset_dropped_once_after_long_silence(self):
        # Defensive epoch path: a high stored offset can hide a
        # lower-id epoch forever, so after >7 days without
        # updates (state.json mtime) the first poll uses 0.
        with open(self.state, "w") as f:
            json.dump({"offset": 1000}, f)
        old = time.time() - 8 * 24 * 3600
        os.utime(self.state, (old, old))
        self._start(scenario=[{"ok": True, "result": [_msg(3)],
                               "sleep": 0}])
        self.assertTrue(self._wait_for(
            lambda: os.path.getsize(self.inbox) > 0, timeout=5))
        get_updates = [c for c in self._calls()
                       if c["method"] == "getUpdates"]
        self.assertEqual(get_updates[0]["params"]["offset"], 0)
        self.assertIn("offset dropped after long silence",
                      self._log_text())
        with open(self.state) as f:
            self.assertEqual(json.load(f)["offset"], 4)

    def test_unwritable_inbox_survives(self):
        # M3: an inbox that cannot be written must not kill the
        # poller or leave a partial line.
        os.unlink(self.inbox)
        os.makedirs(self.inbox)  # a directory, not a file
        self._start(scenario=[{"ok": True, "result": [_msg(7)],
                               "sleep": 0}])
        self.assertTrue(self._wait_for(
            lambda: "cannot write inbox" in self._log_text(),
            timeout=5))
        self.assertIsNone(self.proc.poll())

    def test_corrupt_state_refuses_to_start(self):
        with open(self.state, "w") as f:
            f.write("{corrupt")
        self._start()
        code = self.proc.wait(timeout=10)
        self.assertEqual(code, 1)
        self.assertIn("corrupt", self._log_text())

    def test_string_chat_id_starts(self):
        # M4: a digit-string chat id is valid for the poller too.
        with open(self.config, "w") as f:
            json.dump({"chat_id": "12345"}, f)
        self._start(scenario=[{"ok": True, "result": [], "sleep": 0.2}])
        self.assertTrue(self._wait_for(
            lambda: self._heartbeat() is not None, timeout=5))
        self.assertIsNone(self.proc.poll())


if __name__ == "__main__":
    unittest.main()
