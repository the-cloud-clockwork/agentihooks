import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.ci_mutation.runner import prepare_workspace


def main():
    root = Path.cwd()
    changed = subprocess.check_output(
        ["git", "-C", "../head", "diff", "--name-only", os.environ["RECEIPT_STATS_BASE"], "HEAD"], text=True
    ).splitlines()
    paths = [
        path
        for path in changed
        if path.startswith("scripts/swarm_ledger/") and path.endswith(".py") and (root / path).is_file()
    ]
    work = root / ".receipt-stats"
    work.mkdir()
    tests = os.environ.get("RECEIPT_STATS_TESTS", "tests/swarm_ledger/receipt_probe.py").split()
    prepare_workspace(root, work, paths, tests)
    selection = work / "selection.json"
    selection.write_text(
        json.dumps(
            {
                path: {
                    "lines": list(range(1, len((root / path).read_text().splitlines()) + 1)),
                    "tests": tests,
                }
                for path in paths
            }
        )
    )
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "scripts.ci_mutation.selection",
            str(selection),
            "0",
            "1",
            "collect",
            str(work / "stats.json"),
            "receipt-stats",
            "0",
            "1",
        ],
        cwd=work,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )


if __name__ == "__main__":
    raise SystemExit(main())
