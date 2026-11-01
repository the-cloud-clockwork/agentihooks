"""REST calls per hour on the gh login this process uses, with the per caller tally of the charged log.

The rate_limit endpoint is not charged, so sampling it once a minute measures the spend without adding to it.
"""

import argparse
import json
import time
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from scripts.swarm import github_reads, stack
from scripts.tick.deadline import Deadline

if TYPE_CHECKING:
    from scripts.tick.ports.contracts import SourceControl

RATE_LIMIT_SECONDS = 30.0
DOCTOR = "doctor."
GRAPHQL = "graphql"


def core(provider: "SourceControl | None" = None) -> tuple[int, int]:
    """Read the used count and reset time of the login's core REST quota through the source control port."""
    found = (provider or stack.login_source_control()).rate_limit(deadline=Deadline.after(RATE_LIMIT_SECONDS))
    return found.used, found.reset


def spent(samples: list[tuple[int, int]]) -> int:
    total = 0
    for (used, reset), (later, later_reset) in zip(samples, samples[1:]):
        total += later if later_reset != reset else max(0, later - used)
    return total


def sample(
    minutes: int,
    interval: int = 60,
    provider: "SourceControl | None" = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Sample the core quota every interval for the window and report the spend with its per caller tally."""
    since = clock()
    samples = [core(provider)]
    for _ in range(minutes * 60 // interval):
        sleep(interval)
        samples.append(core(provider))
    until = clock()
    calls = spent(samples)
    callers = rest_counts(since, until)
    doctor = sum(count for caller, count in callers.items() if caller.startswith(DOCTOR))
    hours = (until - since) / 3600 or 1
    return {
        "since": since,
        "until": until,
        "calls": calls,
        "per_hour": round(calls / hours),
        "outside_doctor_per_hour": round(max(0, calls - doctor) / hours),
        "unattributed_per_hour": round(max(0, calls - sum(callers.values())) / hours),
        "callers": dict(sorted(callers.items(), key=lambda item: -item[1])),
    }


def rest_counts(since: float, until: float) -> dict[str, int]:
    try:
        lines = (github_reads.home() / "charged.jsonl").read_text().splitlines()
    except OSError:
        return {}
    login, tally = github_reads.source(), Counter()
    for line in lines:
        try:
            row = json.loads(line)
            if since <= row["at"] < until and row["source"] == login and row["endpoint"] != GRAPHQL:
                tally[row["caller"]] += 1
        except (ValueError, KeyError, TypeError):
            continue
    return dict(tally)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm.github_sampler")
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    print(json.dumps(sample(args.minutes, args.interval)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
