import argparse
import subprocess
from pathlib import Path

from tests.coverage_baseline import _executed

REPO = "the-cloud-clockwork/agentihooks"
SHARDS = 8


def _gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True)


def differences(runs: dict[str, dict[str, list[int]]]) -> list[tuple[str, int, list[str], list[str]]]:
    found = []
    for module in sorted(set().union(*runs.values())):
        ran = {run: set(lines.get(module, ())) for run, lines in runs.items()}
        for line in sorted(set().union(*ran.values()) - set.intersection(*ran.values())):
            found.append(
                (module, line, [run for run in ran if line in ran[run]], [run for run in ran if line not in ran[run]])
            )
    return found


def report(runs: list[str], found: list[tuple[str, int, list[str], list[str]]]) -> str:
    names = ", ".join(runs)
    if not found:
        return f"Every measured line ran the same way across runs {names}"
    rows = [
        f"{module}:{line} ran in {', '.join(ran)}, missed in {', '.join(missed)}" for module, line, ran, missed in found
    ]
    noun = "line differs" if len(found) == 1 else "lines differ"
    return "\n".join([*rows, f"{len(found)} measured {noun} across runs {names}"])


def _kept(run: str) -> int:
    unexpired = ".artifacts[] | select(.expired | not) | .name"
    names = set(_gh("api", f"repos/{REPO}/actions/runs/{run}/artifacts?per_page=100", "--jq", unexpired).split())
    return sum(f"coverage-3.12-{n}" in names for n in range(1, SHARDS + 1))


def _download(run: str, work: Path) -> list[Path]:
    dest = work / run
    args = ["run", "download", run, "--repo", REPO, "--dir", str(dest)]
    for n in range(1, SHARDS + 1):
        args += ["--name", f"coverage-3.12-{n}"]
    _gh(*args)
    return [dest / f"coverage-3.12-{n}" / ".coverage" for n in range(1, SHARDS + 1)]


def compare(runs: list[str], work: Path) -> list[tuple[str, int, list[str], list[str]]]:
    if len(runs) < 2:
        raise ValueError("compare two or more runs of one commit")
    commits = {run: _gh("api", f"repos/{REPO}/actions/runs/{run}", "--jq", ".head_sha").strip() for run in runs}
    if len(set(commits.values())) != 1:
        raise ValueError(f"runs measure different commits: {commits}")
    for run in runs:
        kept = _kept(run)
        if kept != SHARDS:
            raise ValueError(f"run {run} kept {kept} of {SHARDS} coverage shards")
    return differences({run: _executed(_download(run, work)) for run in runs})


def latest(depth: int) -> list[str]:
    for sha in _gh("api", f"repos/{REPO}/commits?sha=dev&per_page={depth}", "--jq", ".[].sha").split():
        listed = _gh(
            "api",
            f"repos/{REPO}/actions/workflows/test.yml/runs?head_sha={sha}&status=completed&per_page=100",
            "--jq",
            ".workflow_runs[].id",
        ).split()
        runs = [run for run in listed if _kept(run) == SHARDS]
        if len(runs) >= 2:
            return runs
    raise ValueError(
        f"no dev commit among the newest {depth} has two Tests runs that kept all {SHARDS} coverage shards"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="list measured lines whose executed state differs across full runs")
    parser.add_argument("--work", type=Path, default=Path("coverage-stability"), help="where shards download")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("runs", help="compare named Tests runs of one commit").add_argument("ids", nargs="+")
    commands.add_parser("latest", help="compare the newest dev commit's Tests runs").add_argument(
        "--depth", type=int, default=30
    )
    args = parser.parse_args(argv)
    try:
        runs = args.ids if args.command == "runs" else latest(args.depth)
        found = compare(runs, args.work)
    except (subprocess.CalledProcessError, ValueError, OSError) as exc:
        print(f"::error::Cannot compare coverage across runs: {exc}")
        return 1
    print(report(runs, found))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
