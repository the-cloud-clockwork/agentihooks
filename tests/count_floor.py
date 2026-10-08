import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Runs inside each tree so the base imports its own sources, never the head's.
_COLLECT = """
import json, sys, pytest

class Collected:
    nodeids = []

    def pytest_collection_finish(self, session):
        Collected.nodeids = [item.nodeid for item in session.items]

code = pytest.main(["--collect-only", "-qq", "-p", "no:cacheprovider", sys.argv[1]], plugins=[Collected()])
with open(sys.argv[2], "w") as out:
    json.dump(Collected.nodeids if code == 0 else None, out)
"""


def collect_all(trees: dict[str, Path], tests: str) -> dict[str, list[str] | None]:
    with tempfile.TemporaryDirectory() as scratch:
        outputs = {side: Path(scratch) / f"{side}.json" for side in trees}
        runs = {
            side: subprocess.Popen(
                [sys.executable, "-c", _COLLECT, tests, str(outputs[side])], cwd=tree, stdout=subprocess.DEVNULL
            )
            for side, tree in trees.items()
        }
        for run in runs.values():
            run.wait()
        return {side: json.loads(outputs[side].read_text()) if outputs[side].is_file() else None for side in trees}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail when the head collects fewer tests than the base")
    parser.add_argument("--base", type=Path, required=True, help="checkout of the base revision")
    parser.add_argument("--head", type=Path, default=Path.cwd(), help="checkout of the head revision")
    parser.add_argument("--tests", default="tests/")
    args = parser.parse_args(argv)
    collected = collect_all({"base": args.base, "head": args.head}, args.tests)
    failed = [side for side, nodeids in collected.items() if not nodeids]
    for side in failed:
        print(f"::error::Collecting the {side} tests failed or found none, so the floor cannot be graded.")
    if failed:
        return 1
    base, head = collected["base"], collected["head"]
    print(f"head collects {len(head)} tests, base floor {len(base)}")
    if len(head) < len(base):
        for nodeid in sorted(set(base) - set(head)):
            print(f"{nodeid} is in the base and not in the head")
        print(f"::error::The head collects {len(base) - len(head)} fewer tests than the base floor of {len(base)}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
