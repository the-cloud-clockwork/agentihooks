import argparse
import json
import re
from collections import Counter
from pathlib import Path

import pytest


class _Collected:
    def __init__(self) -> None:
        self.nodeids: list[str] = []

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.nodeids = [item.nodeid for item in session.items]


def collect(tests: str) -> list[str] | None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail unless every collected test ran in exactly one shard")
    parser.add_argument("durations", type=Path, nargs="+", help="the durations.json each shard stored")
    parser.add_argument("--tests", default="tests/")
    parser.add_argument("--collected", type=Path, help="JSON test identifiers collected by the head run")
    args = parser.parse_args(argv)
    if args.collected:
        try:
            collected = json.loads(args.collected.read_text())
        except (OSError, ValueError) as error:
            print(f"::error::Cannot read collected test data at {args.collected}: {error}")
            return 1
        if (
            not isinstance(collected, list)
            or not all(isinstance(nodeid, str) and nodeid for nodeid in collected)
            or len(set(collected)) != len(collected)
        ):
            print(f"::error::Invalid collected test data at {args.collected}.")
            return 1
    else:
        collected = collect(args.tests)
    if not collected:
        print(f"::error::Collecting {args.tests} failed or found no tests, so no shard can be graded.")
        return 1
    unread = [str(path) for path in args.durations if not path.is_file()]
    if unread:
        print(f"::error::No shard durations at {', '.join(unread)}, so those shards cannot be graded.")
        return 1
    hashes = {path: _durations_hash(path) for path in args.durations}
    if len(set(hashes.values())) != 1 or None in hashes.values():
        named = ", ".join(f"{path.parent.name}: {digest or 'none'}" for path, digest in hashes.items())
        print(f"::error::Shards split on different stored durations, so their test sets overlap or leave gaps: {named}")
        return 1
    counts = run_counts(args.durations)
    wrong = sorted(nodeid for nodeid in collected if counts[nodeid] != 1)
    print(f"{len(collected)} collected tests, {len(wrong)} ran zero times or more than once")
    for nodeid in wrong:
        print(f"{nodeid} ran {counts[nodeid]} times")
    if wrong:
        print(f"::error::{len(wrong)} collected tests did not run in exactly one of {len(args.durations)} shards.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
