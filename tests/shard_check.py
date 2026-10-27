import argparse
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


class _Collected:
    def __init__(self) -> None:
        self.nodeids: list[str] = []

    def pytest_collection_finish(self, session: "pytest.Session") -> None:
        self.nodeids = [item.nodeid for item in session.items]


def collect(tests: str) -> list[str] | None:
    import pytest

    recorder = _Collected()
    code = pytest.main(["--collect-only", "-qq", "-p", "no:cacheprovider", tests], plugins=[recorder])
    return recorder.nodeids if code == 0 else None


def run_counts(paths: list[Path]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in paths:
        counts.update({re.sub(r"@[^\[\]]*$", "", nodeid) for nodeid in json.loads(path.read_text())})
    return counts


def _durations_hash(path: Path) -> str | None:
    recorded = path.parent / "durations.sha256"
    return recorded.read_text().strip() if recorded.is_file() else None


def _read_collected(path: Path) -> list[str] | None:
    try:
        collected = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        print(f"::error::Cannot read collected test data at {path}: {error}")
        return None
    if (
        not isinstance(collected, list)
        or not all(isinstance(nodeid, str) and nodeid for nodeid in collected)
        or len(set(collected)) != len(collected)
    ):
        print(f"::error::Invalid collected test data at {path}.")
        return None
    return collected


def within(collected: list[str], files: list[str]) -> list[str]:
    paths = {PurePosixPath(path) for path in files}
    return [nodeid for nodeid in collected if PurePosixPath(nodeid.split("::", 1)[0]) in paths]


def _scope(args: argparse.Namespace) -> list[str] | None:
    if args.collected:
        collected = _read_collected(args.collected)
        if collected is None:
            return None
    else:
        collected = collect(args.tests)
    if not collected:
        print(f"::error::Collecting {args.tests} failed or found no tests, so no shard can be graded.")
        return None
    if args.within:
        collected = within(collected, args.within)
        if not collected:
            print(f"::error::No collected test is in {' '.join(args.within)}, so the leg cannot be graded.")
            return None
    return collected


def plan_tests(path: Path, collected: list[str]) -> tuple[set[str], set[str]]:
    recorded = json.loads(path.read_text())
    tests = set(recorded["tests"]).intersection(collected)
    return tests, tests if "runs" in recorded else tests.intersection(recorded.get("branch", []))


def _reused(paths: list[Path], collected: list[str]) -> tuple[set[str], set[str]] | None:
    dev: set[str] = set()
    branch: set[str] = set()
    for path in paths:
        try:
            tests, from_runs = plan_tests(path, collected)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"::error::Cannot read the test plan at {path}: {error}")
            return None
        dev |= tests - from_runs
        branch |= from_runs
    return dev, branch - dev


def _split_apart(paths: list[Path]) -> bool:
    hashes = {path: _durations_hash(path) for path in paths}
    if len(set(hashes.values())) != 1 or None in hashes.values():
        named = ", ".join(f"{path.parent.name}: {digest or 'none'}" for path, digest in hashes.items())
        print(f"::error::Shards split on different stored durations, so their test sets overlap or leave gaps: {named}")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail unless every collected test ran in exactly one shard")
    parser.add_argument("durations", type=Path, nargs="+", help="the durations.json each shard stored")
    parser.add_argument("--tests", default="tests/")
    parser.add_argument("--collected", type=Path, help="JSON test identifiers collected by the head run")
    parser.add_argument(
        "--reused",
        type=Path,
        action="append",
        default=[],
        help="a test plan whose tests reuse an earlier passed result; repeat it for the dev and pull request plans",
    )
    parser.add_argument("--within", nargs="+", help="grade only the collected tests of these test files, one leg")
    parser.add_argument("--leg", help="the leg named on the ran and reused count line")
    args = parser.parse_args(argv)
    collected = _scope(args)
    if collected is None:
        return 1
    unread = [str(path) for path in args.durations if not path.is_file()]
    if unread:
        print(f"::error::No shard durations at {', '.join(unread)}, so those shards cannot be graded.")
        return 1
    if not args.within and _split_apart(args.durations):
        return 1
    plans = _reused(args.reused, collected)
    if plans is None:
        return 1
    dev, branch = plans
    reused = dev | branch
    counts = run_counts(args.durations)
    wrong = sorted(nodeid for nodeid in collected if counts[nodeid] != (0 if nodeid in reused else 1))
    print(f"{len(collected)} collected tests, {len(wrong)} ran zero times or more than once, {len(reused)} reused")
    if args.leg:
        ran = sum(1 for nodeid in collected if counts[nodeid])
        print(f"Leg {args.leg}: ran {ran}, reused {len(dev)} from dev, {len(branch)} from pull request runs")
    for nodeid in wrong:
        print(f"{nodeid} ran {counts[nodeid]} times{' though reused' if nodeid in reused else ''}")
    if wrong:
        print(f"::error::{len(wrong)} collected tests did not run in exactly one of {len(args.durations)} shards.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
