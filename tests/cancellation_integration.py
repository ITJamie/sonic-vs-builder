"""Cancel an isolated Docker command and verify its container does not survive."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).parents[1]
name = "sonic-vs-cancel-test-" + uuid.uuid4().hex
command = ["docker", "run", "--rm", "--name", name, "--network", "none",
           "debian:bookworm-slim", "sh", "-ec", "trap 'exit 0' TERM; while :; do sleep 1; done"]
wrapper = None
try:
    subprocess.run(["docker", "pull", "debian:bookworm-slim"], check=True)
    with tempfile.TemporaryDirectory() as directory:
        code = (f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
                "import sonic_vs; sonic_vs.install_signal_handlers(); "
                f"sonic_vs.run(*{command!r})")
        wrapper = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", name],
                                   capture_output=True, text=True)
            if state.returncode == 0 and state.stdout.strip() == "true":
                break
            if wrapper.poll() is not None:
                raise RuntimeError(f"Test command exited before cancellation: {wrapper.communicate()}")
            time.sleep(0.1)
        else:
            raise RuntimeError("Test container did not start")
        os.kill(wrapper.pid, signal.SIGINT)
        output = wrapper.communicate(timeout=15)
        if wrapper.returncode != 130:
            raise RuntimeError(f"Unexpected cancellation status {wrapper.returncode}: {output}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode != 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Docker test container survived command cancellation")
    print("Docker cancellation forwards to the isolated container; no test container remains")
finally:
    if wrapper is not None and wrapper.poll() is None:
        wrapper.kill()
        wrapper.communicate()
    # This name is random and created only by this regression, never a host build.
    subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
