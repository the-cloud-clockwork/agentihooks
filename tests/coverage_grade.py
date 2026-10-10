import ast
import difflib
import functools
import itertools
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
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


def pair_moves(
    gone: dict[str, str], added: dict[str, str], head: Iterable[str] = ()
) -> dict[str, str | tuple[str, ...]]:
    defined = {path: _defined(new) for path, new in added.items()}
    counts = Counter(name for source in head for name in _defined(source))
    generic = {name for name, count in counts.items() if count > 1}
    pairs = {}
    for old_path, old in gone.items():
        names = _defined(old) - generic
        shared = {path: names & defined[path] for path in added}
        matched = set().union(*shared.values())
        if len(matched) >= 2 and 2 * len(matched) >= len(names):
            paths = tuple(sorted(path for path, found in shared.items() if found))
            pairs[old_path] = paths[0] if len(paths) == 1 else paths
    return pairs


def _module_definitions(source: str) -> dict[str, ast.AST]:
    pending = list(ast.parse(source).body)
    defined = {}
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            defined[node.name] = node
        else:
            pending.extend(ast.iter_child_nodes(node))
    return defined


@functools.lru_cache(maxsize=512)
def _split_map(old: str, new: str) -> dict[int, int]:
    old_nodes, new_nodes = _module_definitions(old), _module_definitions(new)
    old_lines, new_lines = old.splitlines(), new.splitlines()
    mapped = {}
    for name in old_nodes.keys() & new_nodes.keys():
        before, after = old_nodes[name], new_nodes[name]
        old_body = "\n".join(old_lines[before.lineno - 1 : before.end_lineno])
        new_body = "\n".join(new_lines[after.lineno - 1 : after.end_lineno])
        mapped.update({a + before.lineno - 1: b + after.lineno - 1 for a, b in line_map(old_body, new_body).items()})
    return mapped


def _lost(
    base: Measurement, head: dict[str, set[int]], head_source: Source, renamed: dict[str, str | tuple[str, ...]]
) -> dict[str, list[int]]:
    lost = {}
    for path, ran in sorted(base.executed.items()):
        moved = renamed.get(path, path)
        targets = (moved,) if isinstance(moved, str) else moved
        gone = set()
        for target in targets:
            old, new = base.source(path), head_source(target)
            if old is None or new is None:
                continue
            mapped = line_map(old, new) if isinstance(moved, str) else _split_map(old, new)
            gone.update(line for line in ran if line in mapped and mapped[line] not in head.get(target, set()))
        if gone:
            lost[path] = sorted(gone)
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
    renamed: dict[str, str | tuple[str, ...]] | None = None,
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
