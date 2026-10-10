import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from coverage import Coverage, CoverageData

from tests.coverage_grade import GRADED, Measurement, Result, Unmeasured, executed, grade, line_map, pair_moves
from tests.coverage_history import dev_runs, git, renamed

__all__ = ["Measurement", "Result", "Unmeasured", "executed", "grade", "line_map", "main", "pair_moves", "report"]


def report(result: Result, missed: dict[str, list[int]], covered: dict[str, int], base_covered: dict[str, int]) -> str:
    out = [f"Graded against the measured base {result.base}."]
    for path, lines in sorted(result.lost.items()):
        out += [f"{path}:{line} ran on base {result.base} and no head test runs it" for line in lines]
    for path, lines in sorted(result.unstable.items()):
        out += [f"{path}:{line} lost, cleared: older dev runs missed it after covering it" for line in lines]
    for path in sorted(set(covered) | set(missed)):
        listed = ", ".join(str(line) for line in missed.get(path, [])) or "none"
        out.append(f"{path}: covered {covered.get(path, 0)} (base {base_covered.get(path, 0)}), missed {listed}")
    return "\n".join(out) + "\n"


def _missed(head: Path, shards: list[Path], out: Path) -> dict[str, list[int]]:
    combined = CoverageData(basename=str(out.resolve() / ".coverage"))
    for shard in shards:
        part = CoverageData(basename=str(shard))
        part.read()
        combined.update(part)
    combined.write()
    coverage = Coverage(data_file=combined.base_filename(), config_file=False)
    coverage.set_option("run:relative_files", True)
    coverage.load()
    cwd = Path.cwd()
    os.chdir(head)
    try:
        modules = sorted(str(path) for root in GRADED for path in Path(root).rglob("*.py"))
        return {path: sorted(coverage.analysis2(path)[3]) for path in modules}
    finally:
        os.chdir(cwd)


def _grade(args: argparse.Namespace, head_files: list[Path], scratch: Path) -> tuple[dict, Measurement, Result]:
    missing = [str(f) for f in head_files if not f.is_file()]
    if missing:
        raise Unmeasured(f"head shard coverage missing: {', '.join(missing)}")
    head = executed(head_files)
    wanted = git("rev-parse", args.base, cwd=args.head).strip()
    if getattr(args, "base_baseline", None):
        runs = _cached_runs(args.head, wanted, args.shards, args.base_baseline)
    else:
        runs = dev_runs(args.head, wanted, args.shards, scratch)
    base = next(runs, None)
    if base is None or base.commit != wanted:
        raise Unmeasured(f"no passed Tests run with coverage holds the base {wanted}")
    moved = renamed(args.head, wanted)
    result = grade(head, lambda path: _read(args.head / path), _chain(base, runs), moved)
    return head, base, result


def _chain(first: Measurement, rest):
    yield first
    yield from rest


def _cached_runs(repo: Path, commit: str, shards: int, path: Path):
    from tests.coverage_baseline import read
    from tests.coverage_history import _show

    value = read(path, git("rev-parse", f"{commit}^{{tree}}", cwd=repo).strip(), shards)
    for index, snapshot in enumerate([value, *value["history"]]):
        revision = snapshot["commit"] if index else commit
        lines = {path: set(lines) for path, lines in snapshot["executed"].items()}
        if not lines:
            raise Unmeasured("baseline cache contains no measured lines")
        yield Measurement(revision, lines, _show(repo, revision))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail when the head stops running a line the base ran")
    parser.add_argument("--base", required=True, help="base revision")
    parser.add_argument("--head", type=Path, default=Path.cwd(), help="checkout of the head revision")
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--head-shards", type=Path, required=True, help="downloaded coverage-3.12-N artifacts")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base-baseline", type=Path, help="exact base measurement restored from the Actions cache")
    args = parser.parse_args(argv)
    head_files = [args.head_shards / f"coverage-3.12-{n}" / ".coverage" for n in range(1, args.shards + 1)]
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        try:
            head, base, result = _grade(args, head_files, Path(scratch))
        except (Unmeasured, subprocess.CalledProcessError, KeyError, ValueError, OSError) as exc:
            print(f"::error::The coverage ratchet cannot grade: {exc}")
            return 1
    text = report(
        result,
        _missed(args.head, head_files, args.out),
        {path: len(lines) for path, lines in head.items()},
        {path: len(lines) for path, lines in base.executed.items()},
    )
    (args.out / "report.txt").write_text(text)
    print(text)
    lost = sum(len(lines) for lines in result.lost.values())
    if lost:
        print(
            f"::error::{lost} lines the base ran are no longer run by any head test; see the coverage-ratchet report."
        )
        return 1
    return 0


def _read(path: Path) -> str | None:
    return path.read_text() if path.is_file() else None


if __name__ == "__main__":
    sys.exit(main())
