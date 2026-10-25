import json
import re
import subprocess
import sys
import time

LIMITED = re.compile(r"rate limit", re.IGNORECASE)
SECONDARY_WAIT = 60
MAX_WAIT = 900
WAITS = 4
calls = 0
# One budget per process, so nested retries and polling loops cannot outwait the queue check timeout.
slept = 0.0
deadline: float | None = None


def reset_wait() -> float:
    # The rate_limit endpoint answers while the installation is limited and does not count against it.
    try:
        done = subprocess.run(["gh", "api", "rate_limit"], capture_output=True, check=True, timeout=30, text=True)
        reset = json.loads(done.stdout)["resources"]["core"]["reset"]
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return SECONDARY_WAIT
    return min(MAX_WAIT, max(1.0, reset - time.time() + 1))


def gh_api(args: list[str], *, timeout: float, text: bool = False) -> subprocess.CompletedProcess:
    global calls, slept
    for waited in range(WAITS + 1):
        calls += 1
        try:
            return subprocess.run(["gh", "api", *args], capture_output=True, check=True, timeout=timeout, text=text)
        except subprocess.CalledProcessError as error:
            stderr = error.stderr if isinstance(error.stderr, str) else (error.stderr or b"").decode(errors="replace")
            left = MAX_WAIT - slept if deadline is None else min(MAX_WAIT - slept, deadline - time.monotonic())
            if waited == WAITS or left <= 0 or not LIMITED.search(stderr):
                raise
            wait = min(left, SECONDARY_WAIT if "secondary" in stderr.lower() else reset_wait())
            print(f"GitHub API rate limit reached; waiting {wait:.0f} seconds for the reset", file=sys.stderr)
            time.sleep(wait)
            slept += wait


def report(name: str) -> None:
    print(f"{name} made {calls} GitHub API calls")


if __name__ == "__main__":
    try:
        sys.stdout.write(gh_api(sys.argv[1:], timeout=120, text=True).stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        sys.exit(str(error.stderr or error))
    finally:
        print(f"made {calls} GitHub API calls", file=sys.stderr)
