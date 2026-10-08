import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.duration_coverage import collected_tests, validate_coverage

_ROOT = Path(__file__).parent.parent
RUNS = 5
ARTIFACTS = "repos/{owner}/{repo}/actions/artifacts?name=durations-3.12-1&per_page=100"


def median_durations(runs: list[dict[str, float]]) -> dict[str, float]:
    nodeids = sorted(set().union(*runs))
    return {nodeid: statistics.median(run[nodeid] for run in runs if nodeid in run) for nodeid in nodeids}


def _gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], cwd=_ROOT, check=True, capture_output=True, text=True).stdout


def ci_run_ids(limit: int, gh=_gh) -> list[str]:
    jq = (
        "[.artifacts[] | select(.expired | not)"
        " | select(.workflow_run.head_repository_id == .workflow_run.repository_id)]"
        " | sort_by(.created_at) | reverse | .[].workflow_run.id"
    )
    return list(dict.fromkeys(gh(["api", ARTIFACTS, "--jq", jq]).split()))[:limit]


def ci_download(run_ids: list[str], folder: Path) -> None:
    for run in run_ids:
        _gh(["run", "download", run, "--dir", str(folder / run)])


def ci_samples(folder: Path, version: str = "*", run: str = "*") -> list[dict[str, float]]:
    samples = []
    for path in sorted(folder.glob(f"{run}/durations-{version}-*/durations.json")):
        durations = json.loads(path.read_text())
        samples.append({re.sub(r"@[^\[\]]*$", "", nodeid): seconds for nodeid, seconds in durations.items()})
    return samples


def ci_medians(folder: Path, version: str, source: str | None) -> dict[str, float]:
    measured = median_durations(ci_samples(folder, version))
    if not source:
        return measured
    current = set().union(*ci_samples(folder, version, source))
    if not current:
        raise SystemExit(f"run {source} kept no durations for Python {version}")
    return {nodeid: seconds for nodeid, seconds in measured.items() if nodeid in current}


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
    parser.add_argument("--ci-run", help="take the durations of the tests this complete CI run measured")
    args = parser.parse_args(argv)
    candidates = {}
    with tempfile.TemporaryDirectory() as tmp:
        if args.ci or args.ci_run:
            run_ids = [args.ci_run] if args.ci_run else []
            run_ids = list(dict.fromkeys([*run_ids, *(ci_run_ids(args.ci) if args.ci else [])]))[: args.ci or 1]
            ci_download(run_ids, Path(tmp))
            for version in ("3.11", "3.12"):
                candidates[f".test_durations-{version}"] = ci_medians(Path(tmp), version, args.ci_run)
            merged = ci_medians(Path(tmp), "*", args.ci_run)
        else:
            merged = median_durations(local_samples(Path(tmp)))
    candidates[".test_durations"] = merged
    collected = collected_tests(_ROOT)
    for durations in candidates.values():
        validate_coverage(durations, collected)
    for name, durations in candidates.items():
        (_ROOT / name).write_text(json.dumps(durations, indent=4, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
