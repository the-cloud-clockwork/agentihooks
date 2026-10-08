import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).parent.parent
RUNS = 5
ARTIFACTS = "repos/{owner}/{repo}/actions/artifacts?name=durations-3.12-1&per_page=100"


def median_durations(runs: list[dict[str, float]]) -> dict[str, float]:
    nodeids = sorted(set().union(*runs))
    return {nodeid: statistics.median(run[nodeid] for run in runs if nodeid in run) for nodeid in nodeids}


def _gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], cwd=_ROOT, check=True, capture_output=True, text=True).stdout


def ci_run_ids(limit: int, gh=_gh) -> list[str]:
    jq = ".artifacts[] | select(.expired | not) | .workflow_run.id"
    return list(dict.fromkeys(gh(["api", ARTIFACTS, "--jq", jq]).split()))[:limit]


def ci_download(run_ids: list[str], folder: Path) -> None:
    for run in run_ids:
        _gh(["run", "download", run, "--dir", str(folder / run)])


def ci_samples(folder: Path, version: str = "*") -> list[dict[str, float]]:
    samples = []
    for path in sorted(folder.glob(f"*/durations-{version}-*/durations.json")):
        durations = json.loads(path.read_text())
        samples.append({re.sub(r"@[^\[\]]*$", "", nodeid): seconds for nodeid, seconds in durations.items()})
    return samples


def local_samples(folder: Path) -> list[dict[str, float]]:
    runs = []
    for i in range(RUNS):
        path = folder / f"run{i}.json"
        command = [sys.executable, "-m", "pytest", "tests/", "-q", "-n", "4"]
        subprocess.run([*command, "--store-durations", "--durations-path", str(path)], cwd=_ROOT, check=False)
        runs.append(json.loads(path.read_text()))
    return runs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ci", type=int, default=0, metavar="RUNS", help="take the durations CI kept from its last RUNS runs"
    )
    parser.add_argument("--ci-run", help="take the durations from one complete CI run")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        if args.ci or args.ci_run:
            ci_download([args.ci_run] if args.ci_run else ci_run_ids(args.ci), Path(tmp))
            runs = ci_samples(Path(tmp))
            for version in ("3.11", "3.12"):
                measured = median_durations(ci_samples(Path(tmp), version))
                (_ROOT / f".test_durations-{version}").write_text(json.dumps(measured, indent=4, sort_keys=True) + "\n")
        else:
            runs = local_samples(Path(tmp))
    (_ROOT / ".test_durations").write_text(json.dumps(median_durations(runs), indent=4, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
