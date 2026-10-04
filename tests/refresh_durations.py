import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).parent.parent
RUNS = 5


def median_durations(runs: list[dict[str, float]]) -> dict[str, float]:
    nodeids = sorted(set().union(*runs))
    return {nodeid: statistics.median(run[nodeid] for run in runs if nodeid in run) for nodeid in nodeids}


def main() -> None:
    runs = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(RUNS):
            path = Path(tmp) / f"run{i}.json"
            command = [sys.executable, "-m", "pytest", "tests/", "-q", "-n", "4"]
            subprocess.run([*command, "--store-durations", "--durations-path", str(path)], cwd=_ROOT, check=False)
            runs.append(json.loads(path.read_text()))
    (_ROOT / ".test_durations").write_text(json.dumps(median_durations(runs), indent=4, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
