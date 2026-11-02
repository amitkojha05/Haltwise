"""Start the mock service (port 9000) and the Haltwise gateway (port 8000).

    python run.py           # start both, keep running; dashboard at http://127.0.0.1:8000
    python run.py --check   # start both, run all three demos unattended, stop, exit 0 if they pass

Works the same in PowerShell, Git Bash, macOS and Linux.
"""
import subprocess
import sys
import time

import httpx


def wait(url: str, timeout: float = 20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.3)
    sys.exit(f"{url} did not start; is the port already in use?")


def start(module: str, port: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--port", str(port),
                             "--log-level", "warning"])


if __name__ == "__main__":
    procs = [start("mock_service.app:app", 9000)]
    wait("http://127.0.0.1:9000/schema")
    procs.append(start("haltwise.gateway:app", 8000))
    wait("http://127.0.0.1:8000/v1/state")
    try:
        if "--check" in sys.argv:
            code = subprocess.call([sys.executable, "agent/demo.py", "all", "--auto"])
            sys.exit(code)
        print("\nHaltwise is running.")
        print("  Dashboard:  http://127.0.0.1:8000")
        print("  Demo:       python agent/demo.py all      (in a second terminal)")
        print("  Stop:       Ctrl+C\n")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
