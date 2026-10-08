import argparse
import json
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

from tests.duration_coverage import IncompleteDurations, collected_tests, validate_coverage

_ROOT = Path(__file__).parent.parent
ARTIFACTS = "repos/{owner}/{repo}/actions/artifacts?name=durations-merged&per_page=100"
ATTEMPTS = 4


def _gh(args: list[str], sleep=time.sleep) -> str:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return subprocess.run(["gh", *args], cwd=_ROOT, check=True, capture_output=True, text=True).stdout
        except subprocess.CalledProcessError as error:
            print(f"gh {args[0]} failed, attempt {attempt} of {ATTEMPTS}: {error.stderr.strip()}", flush=True)
            if attempt == ATTEMPTS:
                raise
            sleep(5 * attempt)


def source_run(run_id: str, gh=_gh) -> str:
    # Every shard and re-run of one run must split on the same file, so a failure fails the shard, never falls back.
    created = gh(["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}", "--jq", ".created_at"]).strip()
    jq = (
        '.artifacts[] | select(.expired | not) | select(.workflow_run.head_branch == "dev")'
        f' | select(.created_at < "{created}") | "\\(.created_at) \\(.workflow_run.id)"'
    )
    earlier = gh(["api", "--paginate", ARTIFACTS, "--jq", jq]).split("\n")
    return max((line.split() for line in earlier if line), default=["", ""])[1]


def download(run: str, folder: Path) -> None:
    _gh(["run", "download", run, "--name", "durations-merged", "--dir", str(folder)])


def _durations(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or not data
        or not all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in data.values())
    ):
        raise ValueError(f"{path.name} holds no durations")
    return data


def adopt(folder: Path, version: str) -> dict[str, float]:
    measured = folder / f".test_durations-{version}"
    return {**_durations(folder / ".test_durations"), **(_durations(measured) if measured.exists() else {})}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="the Python version whose durations this shard splits on")
    version = parser.parse_args(argv).version
    committed = _ROOT / f".test_durations-{version}"
    if not committed.is_file():
        committed = _ROOT / ".test_durations"
    collected = collected_tests(_ROOT)
    run = source_run(os.environ["GITHUB_RUN_ID"])
    if run:
        with tempfile.TemporaryDirectory() as tmp:
            download(run, Path(tmp))
            durations = adopt(Path(tmp), version)
        try:
            validate_coverage(durations, collected)
        except IncompleteDurations as error:
            print(f"Refusing durations from dev run {run}: {error}")
        else:
            (_ROOT / ".test_durations").write_text(json.dumps(durations, indent=4, sort_keys=True) + "\n")
            print(f"Using {len(durations)} durations from green dev run {run}")
            return
    try:
        validate_coverage(_durations(committed), collected)
    except IncompleteDurations:
        committed = _ROOT / ".test_durations"
        validate_coverage(_durations(committed), collected)
    if committed != _ROOT / ".test_durations":
        (_ROOT / ".test_durations").write_bytes(committed.read_bytes())
    print("Using committed durations")


if __name__ == "__main__":
    main()
