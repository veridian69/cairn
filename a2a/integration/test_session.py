"""Exercise foreground listener lifecycle using disposable processes only."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "garden-session"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile.json"
        self.profile.write_text(json.dumps({"adapter": "opencode"}))
        self.log = self.root / "calls"
        self.binary = self.root / "a2a"
        self.binary.write_text(
            "#!/usr/bin/env python3\n"
            "import os,signal,sys,time\n"
            "from pathlib import Path\n"
            f"log=Path({str(self.log)!r})\n"
            "with log.open('a') as f: f.write(sys.argv[1]+'\\n')\n"
            "if (os.environ.get('GARDEN_TEST_DESCENDANT') and sys.argv[1]=='attend') or (os.environ.get('GARDEN_TEST_DOCTOR_DESCENDANT') and sys.argv[1]=='doctor'):\n"
            " import subprocess\n"
            " descendant=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(\"ready\",flush=True); time.sleep(60)'],stdout=subprocess.PIPE)\n"
            " descendant.stdout.readline()\n"
            f" Path({str(self.root / 'descendant')!r}).write_text(str(descendant.pid))\n"
            " sys.exit(42)\n"
            "if sys.argv[1]=='doctor' and os.environ.get('GARDEN_TEST_DOCTOR_WAIT'): time.sleep(60)\n"
            "if sys.argv[1]=='doctor': sys.exit(0)\n"
            "if os.environ.get('GARDEN_TEST_FAIL'): sys.exit(42)\n"
            "signal.signal(signal.SIGTERM,lambda *_:sys.exit(int(os.environ.get('GARDEN_TEST_STOP','0'))))\n"
            "while True: time.sleep(.1)\n"
        )
        self.binary.chmod(0o700)
        self.host = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.stop, self.host)

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def launch(self, extra=None):
        env = os.environ.copy()
        env.update(extra or {})
        process = subprocess.Popen(
            [
                str(SCRIPT),
                "--profile",
                str(self.profile),
                "--binary",
                str(self.binary),
                "--host-pid",
                str(self.host.pid),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.addCleanup(self.stop, process)
        return process

    def started(self):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            if self.log.exists() and "attend" in self.log.read_text():
                time.sleep(0.05)
                return
            time.sleep(0.01)
        self.fail("listener did not start")

    def test_host_exit_stops_listener(self):
        process = self.launch()
        self.started()
        self.stop(self.host)
        self.assertEqual(process.wait(timeout=4), 0)
        self.assertEqual(self.log.read_text().splitlines(), ["doctor", "attend"])

    def test_failure_is_not_restarted_or_hidden(self):
        process = self.launch({"GARDEN_TEST_FAIL": "1"})
        self.assertEqual(process.wait(timeout=4), 42)
        self.assertIsNone(self.host.poll())
        self.assertEqual(self.log.read_text().splitlines(), ["doctor", "attend"])

    def test_host_exit_preserves_uncertain_listener_status(self):
        process = self.launch({"GARDEN_TEST_STOP": "7"})
        self.started()
        self.stop(self.host)
        self.assertEqual(process.wait(timeout=4), 7)

    def test_stopping_supervisor_does_not_kill_host(self):
        process = self.launch()
        self.started()
        process.terminate()
        self.assertEqual(process.wait(timeout=4), 0)
        self.assertIsNone(self.host.poll())

    def test_dead_host_never_starts_listener(self):
        self.stop(self.host)
        process = self.launch()
        self.assertNotEqual(process.wait(timeout=4), 0)
        self.assertFalse(self.log.exists())

    def test_claude_uses_its_own_mcp_lifecycle(self):
        self.profile.write_text(json.dumps({"adapter": "claude"}))
        process = self.launch()
        self.assertNotEqual(process.wait(timeout=4), 0)
        self.assertFalse(self.log.exists())

    def test_profile_symlink_rejected(self):
        original = self.profile
        self.profile = self.root / "link.json"
        self.profile.symlink_to(original)
        process = self.launch()
        self.assertNotEqual(process.wait(timeout=4), 0)
        self.assertFalse(self.log.exists())

    def test_exited_listener_does_not_leave_proxy_descendant(self):
        process = self.launch({"GARDEN_TEST_DESCENDANT": "1"})
        self.assertEqual(process.wait(timeout=4), 42)
        descendant = int((self.root / "descendant").read_text())
        until = time.monotonic() + 2
        while time.monotonic() < until:
            path = Path(f"/proc/{descendant}/stat")
            if not path.exists() or path.read_text().split(")", 1)[1].split()[0] == "Z":
                return
            time.sleep(0.01)
        os.kill(descendant, 9)
        self.fail("orphaned proxy still running")

    def test_preflight_failure_also_cleans_descendants(self):
        process = self.launch({"GARDEN_TEST_DOCTOR_DESCENDANT": "1"})
        self.assertEqual(process.wait(timeout=4), 1)
        descendant = int((self.root / "descendant").read_text())
        until = time.monotonic() + 2
        while time.monotonic() < until:
            path = Path(f"/proc/{descendant}/stat")
            if not path.exists() or path.read_text().split(")", 1)[1].split()[0] == "Z":
                self.assertEqual(self.log.read_text().splitlines(), ["doctor"])
                return
            time.sleep(0.01)
        os.kill(descendant, 9)
        self.fail("orphaned preflight proxy still running")

    def test_timed_out_preflight_cannot_succeed_through_clean_shutdown(self):
        loader = importlib.machinery.SourceFileLoader(
            "garden_session_test", str(SCRIPT)
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        cleanup = module.stop_listener
        args = [
            str(SCRIPT),
            "--profile",
            str(self.profile),
            "--binary",
            str(self.binary),
            "--host-pid",
            str(self.host.pid),
        ]
        with (
            mock.patch.object(module, "PREFLIGHT_TIMEOUT", 0.05),
            mock.patch.object(module.sys, "argv", args),
            mock.patch.dict(
                os.environ, {"GARDEN_TEST_DOCTOR_WAIT": "1", "GARDEN_TEST_FAIL": "1"}
            ),
            mock.patch.object(
                module,
                "stop_listener",
                side_effect=lambda child: (cleanup(child), 0)[1],
            ),
        ):
            with self.assertRaises(ValueError):
                module.main()
        self.assertEqual(self.log.read_text().splitlines(), ["doctor"])


if __name__ == "__main__":
    unittest.main()
