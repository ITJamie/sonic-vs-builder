"""Real process-tree cancellation: no orphan build may hold the cleanup lock."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import sonic_vs


class CommandTests(unittest.TestCase):
    def test_capture_and_stdin_are_preserved(self):
        result = sonic_vs.run(sys.executable, "-c", "import sys; print(sys.stdin.read().upper())",
                              input="input", stdout=subprocess.PIPE)
        self.assertEqual(result.stdout, "INPUT\n")
        self.assertEqual(result.returncode, 0)

    def test_failure_still_propagates_with_output(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            sonic_vs.run(sys.executable, "-c", "print('failure'); raise SystemExit(7)", stdout=subprocess.PIPE)
        self.assertEqual(error.exception.returncode, 7)
        self.assertEqual(error.exception.stdout, "failure\n")

    def test_signals_stop_descendants_and_release_build_lock(self):
        child_code = """
import os, signal, subprocess, sys, time
from pathlib import Path
# Exercise a grandchild that requires KILL after its parent exits on TERM.
grandchild = subprocess.Popen([sys.executable, '-c',
    'import signal,time,sys; from pathlib import Path; '
    'signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(60)',
    sys.argv[1] + '.ready'])
while not Path(sys.argv[1] + '.ready').exists():
    time.sleep(0.01)
Path(sys.argv[1]).write_text(str(os.getpid()) + ' ' + str(grandchild.pid))
time.sleep(60)
"""
        for number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=number), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pidfile = root / "pids"
                code = (
                    "import signal,sys; from pathlib import Path; "
                    f"sys.path.insert(0, {str(Path(sonic_vs.__file__).parent)!r}); "
                    "import sonic_vs; "
                    # Match a Python process inheriting SIGINT=ignored from a
                    # cancelled Actions shell pipeline, then restore handling.
                    "signal.signal(signal.SIGINT, signal.SIG_IGN); "
                    "sonic_vs.install_signal_handlers(); "
                    f"lock=sonic_vs.runner_lock(Path({str(root)!r})); lock.__enter__(); "
                    f"sonic_vs.run(sys.executable, '-c', {child_code!r}, {str(pidfile)!r})"
                )
                wrapper = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                pids = []
                try:
                    deadline = time.monotonic() + 10
                    while not pidfile.exists() and wrapper.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(pidfile.exists(), "Child command did not start")
                    pids = [int(pid) for pid in pidfile.read_text().split()]
                    os.kill(wrapper.pid, number)
                    stdout, stderr = wrapper.communicate(timeout=10)
                    self.assertEqual(wrapper.returncode, 128 + number, (stdout, stderr))
                    with sonic_vs.runner_lock(root, blocking=False):
                        pass
                    deadline = time.monotonic() + 3
                    while any(self.running(pid) for pid in pids) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(any(self.running(pid) for pid in pids), "Build descendant survived cancellation")
                finally:
                    if wrapper.poll() is None:
                        wrapper.kill()
                    for pid in pids:
                        try:
                            if self.running(pid):
                                os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    wrapper.communicate()

    @staticmethod
    def running(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            try:
                return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
            except FileNotFoundError:
                return False
        return True


if __name__ == "__main__":
    unittest.main()
