import json
import re
import subprocess
import sys
import time

LIMITED = re.compile(r"rate limit", re.IGNORECASE)
SECONDARY_WAIT = 60
MAX_WAIT = 3600
calls = 0


def reset_wait() -> float:
    # The rate_limit endpoint answers while the installation is limited and does not count against it.
    done = subprocess.run(["gh", "api", "rate_limit"], capture_output=True, check=True, timeout=30)
    reset = json.loads(done.stdout)["resources"]["core"]["reset"]
    return min(MAX_WAIT, max(1.0, reset - time.time() + 1))


def gh_api(args: list[str], *, timeout: float, text: bool = False) -> subprocess.CompletedProcess:
    global calls
    while True:
        calls += 1
        try:
            return subprocess.run(["gh", "api", *args], capture_output=True, check=True, timeout=timeout, text=text)
        except subprocess.CalledProcessError as error:
            stderr = error.stderr if isinstance(error.stderr, str) else (error.stderr or b"").decode(errors="replace")
            if not LIMITED.search(stderr):
                raise
            wait = SECONDARY_WAIT if "secondary" in stderr.lower() else reset_wait()
            print(f"GitHub API rate limit reached; waiting {wait:.0f} seconds for the reset", file=sys.stderr)
            time.sleep(wait)


def report(name: str) -> None:
    print(f"{name} made {calls} GitHub API calls")
