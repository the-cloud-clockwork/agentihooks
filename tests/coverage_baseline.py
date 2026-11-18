import argparse
import json
import subprocess
from pathlib import Path

from coverage.exceptions import NotPython

from tests.coverage_grade import statements_of
from tests.coverage_summary import NAME as SUMMARY

OWN_DATABASE = ".coverage"


def _git(repo: Path, revision: str) -> str:
    return subprocess.check_output(["git", "rev-parse", revision], cwd=repo, text=True).strip()


def read(path: Path, tree: str | None = None) -> dict:
    if not path.is_file():
        raise ValueError(
            "baseline cache missing: a failed dev run saves no baseline; merge a measured dev revision instead of rerunning"
        )
    value = json.loads(path.read_text())
    if tree and value["tree"] != tree:
        raise ValueError("baseline cache tree differs from the base tree")
    return value


def _executed(summaries: list[Path]) -> dict[str, list[int]]:
    """Union the lines each shard's own database ran, as the executed lines summary of that shard records them."""
    lines: dict[str, set[int]] = {}
    for summary in summaries:
        if not summary.is_file():
            raise ValueError(f"baseline shard summary missing: {summary}")
        measured = json.loads(summary.read_text()).get(OWN_DATABASE)
        if not measured:
            raise ValueError(f"baseline shard {summary} measured no line under hooks or scripts")
        for path, ran in measured.items():
            lines.setdefault(path, set()).update(ran)
    return {path: sorted(ran) for path, ran in lines.items()}


def record(repo: Path, shards: list[Path], target: Path, previous: Path | None = None) -> None:
    executed = _executed(shards)
    for path, lines in executed.items():
        statements = statements_of((repo / path).read_text(), lines)
        for line in sorted(set(lines) - set(statements)):
            print(f"::warning::{path}:{line} is not executable in the current source; dropped from baseline")
        executed[path] = sorted(set(statements.values()))
    history = []
    if previous and previous.is_file():
        old = read(previous)
        history = [{key: value for key, value in old.items() if key != "history"}, *old["history"]][:30]
    value = {
        "commit": _git(repo, "HEAD"),
        "tree": _git(repo, "HEAD^{tree}"),
        "shards": len(shards),
        "executed": executed,
        "history": history,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value))
    print(f"Measured coverage baseline tree {value['tree']} with {len(shards)} shards")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="record a passed CI coverage baseline")
    parser.add_argument("--head", type=Path, default=Path.cwd())
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--head-summaries", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args(argv)
    try:
        shards = [args.head_summaries / f"executed-lines-3.12-{n}" / SUMMARY for n in range(1, args.shards + 1)]
        downloaded = sorted(folder.name for folder in args.head_summaries.glob("executed-lines-3.12-*"))
        if downloaded != sorted(shard.parent.name for shard in shards):
            raise ValueError(f"downloaded shard summaries {downloaded} are not the {args.shards} shards the run split")
        record(args.head, shards, args.out, args.previous)
    except (subprocess.CalledProcessError, KeyError, ValueError, OSError, NotPython) as exc:
        print(f"::error::Cannot record the coverage baseline: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
