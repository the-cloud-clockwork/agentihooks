"""REST calls per hour on the gh login this process uses, with the per caller tally of the charged log.

The rate_limit endpoint is not charged, so sampling it once a minute measures the spend without adding to it.
"""

import argparse
import json
import subprocess
import time

from scripts.swarm import github_reads

DOCTOR = "doctor."


def core(run=subprocess.run) -> tuple[int, int]:
    done = run(["gh", "api", "rate_limit"], capture_output=True, text=True, check=True, timeout=30)
    found = json.loads(done.stdout)["resources"]["core"]
    return found["used"], found["reset"]


def spent(samples: list[tuple[int, int]]) -> int:
    total = 0
    for (used, reset), (later, later_reset) in zip(samples, samples[1:]):
        total += later if later_reset != reset else max(0, later - used)
    return total


def sample(minutes: int, interval: int = 60, run=subprocess.run, sleep=time.sleep, clock=time.time) -> dict:
    since = clock()
    samples = [core(run)]
    for _ in range(minutes * 60 // interval):
        sleep(interval)
        samples.append(core(run))
    until = clock()
    calls = spent(samples)
    callers = github_reads.counts(since, until)
    doctor = sum(count for caller, count in callers.items() if caller.startswith(DOCTOR))
    hours = (until - since) / 3600 or 1
    return {
        "since": since,
        "until": until,
        "calls": calls,
        "per_hour": round(calls / hours),
        "outside_doctor_per_hour": round(max(0, calls - doctor) / hours),
        "callers": dict(sorted(callers.items(), key=lambda item: -item[1])),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm.github_sampler")
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    print(json.dumps(sample(args.minutes, args.interval)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
