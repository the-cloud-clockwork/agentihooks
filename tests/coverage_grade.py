import ast
import difflib
import functools
import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from coverage import CoverageData

GRADED = ("hooks/", "scripts/")
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
        measured = {path: data.lines(path) for path in data.measured_files() if path.startswith(GRADED)}
        measured = {path: ran for path, ran in measured.items() if ran}
        if not measured:
            raise Unmeasured(f"shard {shard} measured no line under {', '.join(GRADED)}")
        for path, ran in measured.items():
            lines.setdefault(path, set()).update(ran)
    return lines


@functools.lru_cache(maxsize=512)
def line_map(old: str, new: str) -> dict[int, int]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    if old_lines == new_lines:
        return {n: n for n in range(1, len(old_lines) + 1)}
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    return {a + k + 1: b + k + 1 for a, b, size in matcher.get_matching_blocks() for k in range(size)}


def _defined(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    kinds = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    return {node.name for node in ast.walk(tree) if isinstance(node, kinds) and not node.name.startswith("__")}


def _retained(old: str, new: str) -> bool:
    kept = [number for number, line in enumerate(old.splitlines(), 1) if line.strip()]
    mapped = line_map(old, new)
    return 2 * sum(number in mapped for number in kept) >= len(kept)


def pair_moves(gone: dict[str, str], added: dict[str, str]) -> dict[str, str]:
    defined = {path: _defined(new) for path, new in added.items()}
    pairs = {}
    for old_path, old in gone.items():
        names = _defined(old)
        scored = [
            (len(names & defined[path]), len(line_map(old, added[path])), path)
            for path in added
            if names & defined[path]
        ]
        if not scored:
            continue
        shared, _, path = max(scored)
        if 2 * shared >= len(names) and _retained(old, added[path]):
            pairs[old_path] = path
    return pairs


def _lost(
    base: Measurement, head: dict[str, set[int]], head_source: Source, renamed: dict[str, str]
) -> dict[str, list[int]]:
    lost = {}
    for path, ran in sorted(base.executed.items()):
        moved = renamed.get(path, path)
        old, new = base.source(path), head_source(moved)
        if old is None or new is None:
            continue
        mapped = line_map(old, new)
        gone = sorted(line for line in ran if line in mapped and mapped[line] not in head.get(moved, set()))
        if gone:
            lost[path] = gone
    return lost


def _statuses(base_source: str, path: str, lines: list[int], run: Measurement) -> dict[int, bool]:
    old = run.source(path)
    if old is None or path not in run.executed:
        return {}
    mapped = line_map(base_source, old)
    return {line: mapped[line] in run.executed[path] for line in lines if line in mapped}


def _flaky(history: list[bool]) -> bool:
    return any(not newer and older for i, newer in enumerate(history) for older in history[i + 1 :])


def _histories(
    base: Measurement, lost: dict[str, list[int]], runs: Iterator[Measurement]
) -> dict[str, dict[int, list[bool]]]:
    histories = {path: {line: [] for line in lines} for path, lines in lost.items()}
    sources = {path: base.source(path) for path in lost}
    for run in itertools.islice(runs, HISTORY):
        for path, lines in lost.items():
            for line, ran in _statuses(sources[path], path, lines, run).items():
                histories[path][line].append(ran)
        if all(_flaky(history) for by_line in histories.values() for history in by_line.values()):
            break
    return histories


def grade(
    head: dict[str, set[int]],
    head_source: Source,
    runs: Iterator[Measurement],
    renamed: dict[str, str] | None = None,
) -> Result:
    base = next(runs, None)
    if base is None:
        raise Unmeasured("no measured base run within the searched dev history")
    lost = _lost(base, head, head_source, renamed or {})
    if not lost:
        return Result(base.commit, {})
    result = Result(base.commit, {})
    for path, by_line in _histories(base, lost, runs).items():
        for line, history in by_line.items():
            bucket = result.unstable if _flaky(history) else result.lost
            bucket.setdefault(path, []).append(line)
    return result
