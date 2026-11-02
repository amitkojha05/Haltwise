"""Starts the mock service and the gateway, then runs all three demo scenarios unattended."""
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def _wait(url, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not start")


def test_three_demos():
    env = {**os.environ, "HALTWISE_UPSTREAM": "http://127.0.0.1:9100",
           "HALTWISE_URL": "http://127.0.0.1:8100", "MOCK_URL": "http://127.0.0.1:9100",
           "HALTWISE_AUDIT": str(ROOT / "audit.test.jsonl")}
    up = lambda mod, port: subprocess.Popen(
        [sys.executable, "-m", "uvicorn", mod, "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env)
    procs = [up("mock_service.app:app", 9100), up("haltwise.gateway:app", 8100)]
    try:
        _wait("http://127.0.0.1:9100/schema")
        _wait("http://127.0.0.1:8100/v1/state")
        r = subprocess.run([sys.executable, "agent/demo.py", "all", "--auto"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=60)
        print(r.stdout)
        assert r.returncode == 0, r.stdout + r.stderr
    finally:
        for p in procs:
            p.terminate()
        (ROOT / "audit.test.jsonl").unlink(missing_ok=True)
