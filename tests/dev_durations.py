import argparse
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path

_ROOT = Path(__file__).parent.parent
ARTIFACTS = "repos/{owner}/{repo}/actions/artifacts?name=durations-merged&per_page=100"


def _gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], cwd=_ROOT, check=True, capture_output=True, text=True).stdout


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
    if committed.is_file():
        (_ROOT / ".test_durations").write_bytes(committed.read_bytes())
    run = source_run(os.environ["GITHUB_RUN_ID"])
    if not run:
        print("Using committed durations")
        return
    with tempfile.TemporaryDirectory() as tmp:
        download(run, Path(tmp))
        durations = adopt(Path(tmp), version)
    (_ROOT / ".test_durations").write_text(json.dumps(durations, indent=4, sort_keys=True) + "\n")
    print(f"Using {len(durations)} durations from green dev run {run}")


if __name__ == "__main__":
    main()
