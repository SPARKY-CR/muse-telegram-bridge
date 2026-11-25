"""Tests for setup.sh idempotency (the review's item #22):
running it twice must not create a nested symlink loop, must not
fail, and must not overwrite an existing config or state file.
The poller itself is stubbed out so no background process starts."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSetupIdempotent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = os.path.join(self.tmp.name, "bridge")
        shutil.copytree(REPO, self.bridge,
                        ignore=shutil.ignore_patterns(
                            "tests", "__pycache__", "runtime"))
        # Stub the poller: it just records that it was invoked
        # and writes a heartbeat (setup.sh verifies the start).
        stub = os.path.join(self.bridge, "poller", "poller.py")
        with open(stub, "w") as f:
            f.write("import json, sys, time\n")
            f.write("rt = sys.argv[sys.argv.index('--runtime-dir')+1]\n")
            f.write("open(sys.argv[sys.argv.index('--log')+1], 'a').write("
                    "'stub poller ran\\n')\n")
            f.write("json.dump({'beat_at': time.time(), 'pid': 123},\n")
            f.write("          open(rt + '/poller-state.json', 'w'))\n")
        self.runtime = os.path.join(self.bridge, "runtime")

    def tearDown(self):
        self.tmp.cleanup()

    def _run_setup(self):
        return subprocess.run(
            ["bash", os.path.join(self.bridge, "setup.sh")],
            capture_output=True, text=True, timeout=30)

    def _wait_for_log(self, needle, timeout=10):
        """The poller starts in the background; wait for its log line."""
        import time
        log = os.path.join(self.runtime, "poller.log")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(log):
                with open(log) as f:
                    if needle in f.read():
                        return True
            time.sleep(0.1)
        return False

    def test_first_run_creates_config_and_exits(self):
        result = self._run_setup()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(os.path.exists(
            os.path.join(self.runtime, "config.json")))
        with open(os.path.join(self.runtime, "config.json")) as f:
            self.assertEqual(json.load(f), {"chat_id": 0})

    def test_second_run_with_valid_config_starts_poller(self):
        self._run_setup()
        with open(os.path.join(self.runtime, "config.json"), "w") as f:
            json.dump({"chat_id": 123}, f)
        result = self._run_setup()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(os.path.exists(
            os.path.join(self.runtime, "state.json")))
        with open(os.path.join(self.runtime, "state.json")) as f:
            self.assertEqual(json.load(f), {"offset": 0})
        self.assertTrue(self._wait_for_log("stub poller ran"))

    def test_third_run_no_symlink_loop_and_config_kept(self):
        # #22: the old `ln -sf` on an existing dir symlink created
        # bin/bin on the second run. There is no symlink at all now.
        self._run_setup()
        with open(os.path.join(self.runtime, "config.json"), "w") as f:
            json.dump({"chat_id": 123}, f)
        self._run_setup()
        result = self._run_setup()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(os.path.lexists(
            os.path.join(self.runtime, "bin")))
        self.assertFalse(os.path.lexists(
            os.path.join(self.runtime, "bin", "bin")))
        with open(os.path.join(self.runtime, "config.json")) as f:
            self.assertEqual(json.load(f), {"chat_id": 123})

    def test_zero_chat_id_refuses(self):
        self._run_setup()
        result = self._run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn("chat_id", result.stdout)

    def test_string_chat_id_accepted(self):
        # M4/L7: a digit string is a valid chat id everywhere.
        self._run_setup()
        with open(os.path.join(self.runtime, "config.json"), "w") as f:
            json.dump({"chat_id": "123"}, f)
        result = self._run_setup()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(self._wait_for_log("stub poller ran"))

    def test_non_numeric_chat_id_refuses(self):
        self._run_setup()
        with open(os.path.join(self.runtime, "config.json"), "w") as f:
            json.dump({"chat_id": "abc"}, f)
        result = self._run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn("chat_id", result.stdout)

    def test_poller_start_failure_reported(self):
        # L7: if the poller dies at once without a heartbeat,
        # setup.sh must fail instead of claiming success.
        stub = os.path.join(self.bridge, "poller", "poller.py")
        with open(stub, "w") as f:
            f.write("import sys\n")
            f.write("print('boom', file=sys.stderr)\n")
            f.write("sys.exit(1)\n")
        self._run_setup()
        with open(os.path.join(self.runtime, "config.json"), "w") as f:
            json.dump({"chat_id": 123}, f)
        result = self._run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not start", result.stderr)

    def test_outbox_created(self):
        # M7: the send_photo outbox directory is created.
        result = self._run_setup()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(os.path.isdir(
            os.path.join(self.runtime, "outbox")))


if __name__ == "__main__":
    unittest.main()
