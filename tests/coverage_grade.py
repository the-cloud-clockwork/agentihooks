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


def _all_flaky(base: Measurement, lost: dict[str, list[int]], older: list[Measurement]) -> bool:
    return all(
        _flaky(history) for path, lines in lost.items() for history in _statuses(base, path, lines, older).values()
    )


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
    older: list[Measurement] = []
    for run in itertools.islice(runs, HISTORY):
        older.append(run)
        if _all_flaky(base, lost, older):
            break
    result = Result(base.commit, {})
    for path, lines in lost.items():
        statuses = _statuses(base, path, lines, older)
        for line in lines:
            bucket = result.unstable if _flaky(statuses[line]) else result.lost
            bucket.setdefault(path, []).append(line)
    return result
