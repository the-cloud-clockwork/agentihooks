import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).parent.parent
ARTIFACTS = "repos/{owner}/{repo}/actions/artifacts?name=durations-merged&per_page=20"


def _gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], cwd=_ROOT, check=True, capture_output=True, text=True).stdout


def source_run(run_id: str, gh=_gh) -> str:
    # Every shard and re-run of one run must split on the same file; one published mid-run would differ.
    created = gh(["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}", "--jq", ".created_at"]).strip()
    jq = (
        '[.artifacts[] | select(.expired | not) | select(.workflow_run.head_branch == "dev")'
        f' | select(.created_at < "{created}")] | sort_by(.created_at) | last | .workflow_run.id // empty'
    )
    return gh(["api", ARTIFACTS, "--jq", jq]).strip()


def download(run: str, folder: Path) -> None:
    _gh(["run", "download", run, "--name", "durations-merged", "--dir", str(folder)])


def _durations(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text())
    valid = isinstance(data, dict) and data
    if not valid or not all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in data.values()):
        raise ValueError(f"{path.name} holds no durations")
    return data


def adopt(folder: Path, version: str) -> dict[str, float]:
    return {**_durations(folder / ".test_durations"), **_durations(folder / f".test_durations-{version}")}


def main(argv: list[str] | None = None) -> None:
    (version,) = sys.argv[1:] if argv is None else argv
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
