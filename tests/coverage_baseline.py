import argparse
import json
import subprocess
from pathlib import Path

from coverage import CoverageData


def _git(repo: Path, revision: str) -> str:
    return subprocess.check_output(["git", "rev-parse", revision], cwd=repo, text=True).strip()


def read(path: Path, tree: str | None = None, shards: int | None = None) -> dict:
    if not path.is_file():
        raise ValueError(
            "baseline cache missing: a failed dev run saves no baseline; merge a measured dev revision instead of rerunning"
        )
    value = json.loads(path.read_text())
    if tree and value["tree"] != tree:
        raise ValueError("baseline cache tree differs from the base tree")
    if shards is not None and value["shards"] != shards:
        raise ValueError("baseline cache shards differ from the measured suite")
    return value


def _executed(shards: list[Path]) -> dict[str, list[int]]:
    lines = {}
    for shard in shards:
        if not shard.is_file():
            raise ValueError(f"baseline shard coverage missing: {shard}")
        data = CoverageData(basename=str(shard))
        data.read()
        measured = {path: data.lines(path) for path in data.measured_files() if path.startswith(("hooks/", "scripts/"))}
        measured = {path: ran for path, ran in measured.items() if ran}
        if not measured:
            raise ValueError(f"baseline shard {shard} measured no line under hooks or scripts")
        for path, ran in measured.items():
            lines.setdefault(path, set()).update(ran)
    return {path: sorted(ran) for path, ran in lines.items()}


def record(repo: Path, shards: list[Path], target: Path, previous: Path | None = None) -> None:
    history = []
    if previous and previous.is_file():
        old = read(previous)
        history = [{key: value for key, value in old.items() if key != "history"}, *old["history"]][:30]
    value = {
        "commit": _git(repo, "HEAD"),
        "tree": _git(repo, "HEAD^{tree}"),
        "shards": len(shards),
        "executed": _executed(shards),
        "history": history,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value))
    print(f"Measured coverage baseline tree {value['tree']} with {len(shards)} shards")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="record a passed CI coverage baseline")
    parser.add_argument("--head", type=Path, default=Path.cwd())
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--head-shards", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args(argv)
    try:
        shards = [args.head_shards / f"coverage-3.12-{n}" / ".coverage" for n in range(1, args.shards + 1)]
        record(args.head, shards, args.out, args.previous)
    except (subprocess.CalledProcessError, KeyError, ValueError, OSError) as exc:
        print(f"::error::Cannot record the coverage baseline: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
