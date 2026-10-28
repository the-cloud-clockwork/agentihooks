import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime

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


def _run(args: list[str], timeout: float, text: bool) -> subprocess.CompletedProcess:
    from scripts import gh_audit
    from scripts.swarm import github_reads

    global calls
    calls += 1
    caller = github_reads.CALLER.get(None) or gh_audit.process_caller()
    return github_reads.rest(
        caller, ["api", *args], subprocess.run, capture_output=True, check=True, timeout=timeout, text=text
    )


def gh_api(args: list[str], *, timeout: float, text: bool = False) -> subprocess.CompletedProcess:
    global slept
    for _ in range(WAITS):
        try:
            return _run(args, timeout, text)
        except subprocess.CalledProcessError as error:
            stderr = str(error.stderr)
            left = MAX_WAIT - slept if deadline is None else min(MAX_WAIT - slept, deadline - time.monotonic())
            if left <= 0 or not LIMITED.search(stderr):
                raise
            wait = min(left, SECONDARY_WAIT if "secondary" in stderr.lower() else reset_wait())
            print(f"GitHub API rate limit reached; waiting {wait:.0f} seconds for the reset", file=sys.stderr)
            time.sleep(wait)
            slept += wait
    return _run(args, timeout, text)


def report(name: str) -> None:
    print(f"{name} made {calls} GitHub API calls", file=sys.stderr)


def budget(name: str, when: str) -> None:
    try:
        done = subprocess.run(
            ["gh", "api", "--include", "rate_limit"], capture_output=True, check=True, timeout=30, text=True
        )
        headers = {
            key.strip().lower(): value.strip()
            for key, _, value in (line.partition(":") for line in done.stdout.splitlines())
        }
        reset = datetime.fromtimestamp(int(headers["x-ratelimit-reset"]), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (
            f"{headers['x-ratelimit-remaining']} of {headers['x-ratelimit-limit']} App API calls left, resets {reset}"
        )
    except (OSError, subprocess.SubprocessError, KeyError, OverflowError, ValueError):
        line = "App API budget unreadable"
    print(f"{name} {when}: {line}", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.stdout.write(gh_api(sys.argv[1:], timeout=120, text=True).stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        sys.exit(str(error.stderr or error))
    finally:
        print(f"made {calls} GitHub API calls", file=sys.stderr)
