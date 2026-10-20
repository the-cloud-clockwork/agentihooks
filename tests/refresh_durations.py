import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.duration_coverage import collected_tests, validate_coverage

_ROOT = Path(__file__).parent.parent
RUNS = 5
VERSIONS = ("3.11", "3.12")
SHARDS = 12
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


def keep_newest(folder: Path, limit: int, keep: str | None = None) -> None:
    runs = sorted(
        (path for path in folder.iterdir() if path.is_dir() and path.name.isdecimal() and path.name != keep),
        key=lambda path: int(path.name),
    )
    for path in runs[: max(len(runs) - limit + (keep is not None), 0)]:
        shutil.rmtree(path)


def ci_samples(folder: Path, version: str = "*", run: str = "*") -> list[dict[str, float]]:
    samples = []
    for path in sorted(folder.glob(f"{run}/durations-{version}-*/durations.json")):
        try:
            durations = json.loads(path.read_text()).items()
        except (ValueError, AttributeError) as error:
            raise ValueError(f"malformed sample file {path}: {error}") from error
        samples.append({re.sub(r"@[^\[\]]*$", "", nodeid): seconds for nodeid, seconds in durations})
    return samples


def ci_medians(
    folder: Path, version: str, source: str | None, collected: list[str], slowest: bool = False
) -> dict[str, float]:
    samples = ci_samples(folder, version)
    measured = (
        {node: max(run[node] for run in samples if node in run) for node in set().union(*samples)}
        if slowest
        else median_durations(samples)
    )
    if not source:
        return measured
    current = set().union(*ci_samples(folder, version, source))
    if not current:
        raise SystemExit(f"run {source} kept no durations for Python {version}")
    kept = current.union(collected)
    return {nodeid: seconds for nodeid, seconds in measured.items() if nodeid in kept}


def local_samples(folder: Path) -> list[dict[str, float]]:
    runs = []
    for i in range(RUNS):
        path = folder / f"run{i}.json"
        command = [sys.executable, "-m", "pytest", "tests/", "-q", "-n", "4"]
        subprocess.run([*command, "--store-durations", "--durations-path", str(path)], cwd=_ROOT, check=False)
        runs.append(json.loads(path.read_text()))
    return runs


def parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument(
        "--ci", type=int, default=0, metavar="RUNS", help="take the durations CI kept from its last RUNS runs"
    )
    arg_parser.add_argument("--ci-run", help="take the durations of the tests this complete CI run measured")
    arg_parser.add_argument(
        "--samples", type=Path, help="read run folders from here instead of GitHub, keeping only the newest RUNS"
    )
    return arg_parser


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    candidates = {}
    collected = collected_tests(_ROOT)
    with tempfile.TemporaryDirectory() as tmp:
        if args.ci or args.ci_run:
            folder = args.samples or Path(tmp)
            if args.samples:
                keep_newest(folder, args.ci or 1, args.ci_run)
            else:
                run_ids = [args.ci_run] if args.ci_run else []
                run_ids = list(dict.fromkeys([*run_ids, *(ci_run_ids(args.ci) if args.ci else [])]))[: args.ci or 1]
                ci_download(run_ids, folder)
            for version in ("3.11", "3.12"):
                candidates[f".test_durations-{version}"] = ci_medians(folder, version, args.ci_run, collected)
                candidates[f".test_durations-slowest-{version}"] = ci_medians(
                    folder, version, args.ci_run, collected, slowest=True
                )
            merged = ci_medians(folder, "*", args.ci_run, collected)
            candidates[".test_durations-slowest"] = ci_medians(folder, "*", args.ci_run, collected, slowest=True)
        else:
            merged = median_durations(local_samples(Path(tmp)))
    candidates[".test_durations"] = merged
    for durations in candidates.values():
        validate_coverage(durations, collected)
    for name, durations in candidates.items():
        (_ROOT / name).write_text(json.dumps(durations, indent=4, sort_keys=True) + "\n")


def missing_shards(folder: Path, run: str) -> list[str]:
    found = (
        re.fullmatch(r"durations-(3\.\d+)-(\d+)", path.parent.name)
        for path in folder.glob(f"{run}/durations-*/durations.json")
    )
    kept = {(match[1], int(match[2])) for match in found if match}
    expected = [(version, shard) for version in VERSIONS for shard in range(1, SHARDS + 1)]
    return [f"durations-{version}-{shard}" for version, shard in expected if (version, shard) not in kept]


def cli(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    try:
        parsed = parser().parse_args(args)
    except SystemExit as error:
        if error.code:
            print(f"::error::refresh durations: invalid arguments {' '.join(args)}")
        raise
    try:
        main(args)
    except SystemExit as error:
        if not error.code:
            raise
        cause = str(error.code)
    except Exception as error:
        cause = str(error).strip() or type(error).__name__
    else:
        return
    if (
        parsed.samples
        and parsed.ci_run
        and parsed.samples.is_dir()
        and (lost := missing_shards(parsed.samples, parsed.ci_run))
    ):
        cause += f"; run {parsed.ci_run} kept no samples from {', '.join(lost)}"
    print(f"::error::refresh durations: {' '.join(cause.split())}")
    raise SystemExit(1)


if __name__ == "__main__":
    cli()
