import argparse
import difflib
import functools
import itertools
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from coverage import Coverage, CoverageData

GRADED = ("hooks/", "scripts/")
SEARCH = 60
HISTORY = 30

Source = Callable[[str], str | None]


class Unmeasured(Exception):
    pass


@dataclass
class Measurement:
    commit: str
    executed: dict[str, set[int]]
    source: Source


@dataclass
class Result:
    base: str
    lost: dict[str, list[int]]
    unstable: dict[str, list[int]] = field(default_factory=dict)


def executed(shards: list[Path]) -> dict[str, set[int]]:
    lines: dict[str, set[int]] = {}
    for shard in shards:
        data = CoverageData(basename=str(shard))
        data.read()
        measured = [path for path in data.measured_files() if path.startswith(GRADED) and data.lines(path)]
        if not measured:
            raise Unmeasured(f"shard {shard} measured no line under {', '.join(GRADED)}")
        for path in measured:
            lines.setdefault(path, set()).update(data.lines(path))
    return lines


@functools.cache
def line_map(old: str, new: str) -> dict[int, int]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    if old_lines == new_lines:
        return {n: n for n in range(1, len(old_lines) + 1)}
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    return {a + k + 1: b + k + 1 for a, b, size in matcher.get_matching_blocks() for k in range(size)}


def _lost(base: Measurement, head: dict[str, set[int]], head_source: Source) -> dict[str, list[int]]:
    lost = {}
    for path, ran in sorted(base.executed.items()):
        old, new = base.source(path), head_source(path)
        if old is None or new is None:
            continue
        mapped = line_map(old, new)
        gone = sorted(line for line in ran if line in mapped and mapped[line] not in head.get(path, set()))
        if gone:
            lost[path] = gone
    return lost


def _statuses(base: Measurement, path: str, lines: list[int], older: list[Measurement]) -> dict[int, list[bool]]:
    statuses: dict[int, list[bool]] = {line: [] for line in lines}
    for run in older:
        old = run.source(path)
        if old is None or path not in run.executed:
            continue
        mapped = line_map(base.source(path), old)
        for line in lines:
            if line in mapped:
                statuses[line].append(mapped[line] in run.executed[path])
    return statuses


def _flaky(history: list[bool]) -> bool:
    return any(not newer and older for i, newer in enumerate(history) for older in history[i + 1 :])


def grade(head: dict[str, set[int]], head_source: Source, runs: Iterator[Measurement]) -> Result:
    base = next(runs, None)
    if base is None:
        raise Unmeasured("no measured base run within the searched dev history")
    lost = _lost(base, head, head_source)
    if not lost:
        return Result(base.commit, {})
    older = list(itertools.islice(runs, HISTORY))
    result = Result(base.commit, {})
    for path, lines in lost.items():
        statuses = _statuses(base, path, lines, older)
        for line in lines:
            bucket = result.unstable if _flaky(statuses[line]) else result.lost
            bucket.setdefault(path, []).append(line)
    return result


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


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def _show(repo: Path, commit: str) -> Source:
    def source(path: str) -> str | None:
        shown = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, capture_output=True, text=True)
        return shown.stdout if shown.returncode == 0 else None

    return source


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout.strip()


def _passed_run(repo: Path, commit: str) -> str | None:
    tree = _git("rev-parse", f"{commit}^{{tree}}", cwd=repo).strip()
    found = _gh(
        "api",
        f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/artifacts?name=tests-passed-{tree}",
        "--jq",
        "[.artifacts[] | select(.expired | not) | select(.workflow_run.head_repository_id"
        " == .workflow_run.repository_id)] | first.workflow_run.id // empty",
    )
    return found or None


def _download(run: str, shards: int, into: Path) -> list[Path] | None:
    names = [arg for shard in range(1, shards + 1) for arg in ("--name", f"coverage-3.12-{shard}")]
    fetched = subprocess.run(
        ["gh", "run", "download", run, "--repo", os.environ["GITHUB_REPOSITORY"], *names, "--dir", str(into)],
        capture_output=True,
        text=True,
    )
    files = [into / f"coverage-3.12-{shard}" / ".coverage" for shard in range(1, shards + 1)]
    return files if fetched.returncode == 0 and all(f.is_file() for f in files) else None


def dev_runs(repo: Path, base: str, shards: int, scratch: Path) -> Iterator[Measurement]:
    commits = _git("rev-list", "--first-parent", f"--max-count={SEARCH}", base, cwd=repo).split()
    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(lambda commit: _passed_run(repo, commit), commits))
    measured = [(commit, run) for commit, run in zip(commits, runs) if run]
    fetch = _fetcher(repo, shards, scratch)
    first = next(((i, found) for i, pair in enumerate(measured) if (found := fetch(pair))), None)
    if first is None:
        return
    index, base = first
    yield base
    with ThreadPoolExecutor(max_workers=8) as pool:
        yield from filter(None, pool.map(fetch, measured[index + 1 : index + 1 + HISTORY]))


def _fetcher(repo: Path, shards: int, scratch: Path) -> Callable[[tuple[str, str]], Measurement | None]:
    def fetch(pair: tuple[str, str]) -> Measurement | None:
        commit, run = pair
        files = _download(run, shards, scratch / run)
        if not files:
            return None
        print(f"dev {commit[:12]} measured by run {run}")
        return Measurement(commit, executed(files), _show(repo, commit))

    return fetch


def _missed(head: Path, shards: list[Path], out: Path) -> dict[str, list[int]]:
    combined = CoverageData(basename=str(out.resolve() / ".coverage"))
    for shard in shards:
        part = CoverageData(basename=str(shard))
        part.read()
        combined.update(part)
    combined.write()
    coverage = Coverage(data_file=combined.base_filename(), config_file=False)
    coverage.load()
    cwd = Path.cwd()
    os.chdir(head)
    try:
        return {
            path: sorted(coverage.analysis2(path)[3])
            for path in sorted(combined.measured_files())
            if path.startswith(GRADED) and Path(path).is_file()
        }
    finally:
        os.chdir(cwd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail when the head stops running a line the base ran")
    parser.add_argument("--base", required=True, help="base revision")
    parser.add_argument("--head", type=Path, default=Path.cwd(), help="checkout of the head revision")
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--head-shards", type=Path, required=True, help="downloaded coverage-3.12-N artifacts")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    head_files = [args.head_shards / f"coverage-3.12-{n}" / ".coverage" for n in range(1, args.shards + 1)]
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        try:
            missing = [str(f) for f in head_files if not f.is_file()]
            if missing:
                raise Unmeasured(f"head shard coverage missing: {', '.join(missing)}")
            head = executed(head_files)
            runs = dev_runs(args.head, args.base, args.shards, Path(scratch))
            base = next(runs, None)
            result = grade(head, lambda path: _read(args.head / path), itertools.chain([base] if base else [], runs))
        except Unmeasured as exc:
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
