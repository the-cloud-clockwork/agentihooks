import ast
import difflib
import functools
import itertools
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from coverage import CoverageData
from coverage.parser import PythonParser

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
    unconfirmed: dict[str, dict[int, int]] = field(default_factory=dict)


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
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
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
        old = base.source(path)
        base_statements = statements_of(old, ran) if old is not None else {}
        for target in targets:
            new = head_source(target)
            if old is None or new is None:
                continue
            mapped = line_map(old, new) if isinstance(moved, str) else _split_map(old, new)
            head_statements = statements_of(new, [mapped[line] for line in base_statements if line in mapped])
            covered = set(statements_of(new, head.get(target, set())).values())
            gone.update(
                statement
                for line, statement in base_statements.items()
                if line in mapped and head_statements.get(mapped[line]) not in covered
            )
        if gone:
            lost[path] = sorted(gone)
    return lost


def statements_of(source: str, lines: Iterable[int]) -> dict[int, int]:
    parser = _parsed(source)
    return {line: parser.first_line(line) for line in lines if parser.first_line(line) in parser.statements}


@functools.lru_cache(maxsize=64)
def _parsed(source: str) -> PythonParser:
    parser = PythonParser(text=source)
    parser.parse_source()
    return parser


def _statuses(base_source: str, path: str, lines: list[int], run: Measurement, old: str | None) -> dict[int, bool]:
    if old is None or path not in run.executed:
        return {}
    mapped = line_map(base_source, old)
    ran = set(statements_of(old, run.executed[path]).values())
    run_statements = statements_of(old, [mapped[line] for line in lines if line in mapped])
    return {line: run_statements.get(mapped[line]) in ran for line in lines if line in mapped}


def _flaky(history: list[bool]) -> bool:
    return any(not newer and older for i, newer in enumerate(history) for older in history[i + 1 :])


def _histories(
    base: Measurement, lost: dict[str, list[int]], runs: Iterator[Measurement]
) -> tuple[dict[str, dict[int, list[bool]]], dict[str, dict[int, list[bool]]]]:
    histories = {path: {line: [] for line in lines} for path, lines in lost.items()}
    versions = {path: {line: [] for line in lines} for path, lines in lost.items()}
    sources = {path: base.source(path) for path in lost}
    for run in itertools.islice(runs, HISTORY):
        for path, lines in lost.items():
            old = run.source(path)
            for line, ran in _statuses(sources[path], path, lines, run, old).items():
                histories[path][line].append(ran)
                if old == sources[path]:
                    versions[path][line].append(ran)
        if all(
            _flaky(history) and any(versions[path][line])
            for path, by_line in histories.items()
            for line, history in by_line.items()
        ):
            break
    return histories, versions


def _imports(source: str | None, path: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(source or "")):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            package = path.split("/")
            parent = ".".join(package[: -node.level]) if node.level else ""
            module = ".".join(part for part in (parent, node.module) if part)
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def _protected(
    base: Measurement,
    head_source: Source,
    changed_tests: set[str],
    paths: dict[str, list[int]],
    renamed: dict[str, str | tuple[str, ...]],
) -> set[str]:
    imports = set()
    for test in changed_tests:
        imports.update(_imports(base.source(test), test))
        imports.update(_imports(head_source(test), test))
    protected = set()
    for path in paths:
        targets = renamed.get(path, path)
        targets = (targets,) if isinstance(targets, str) else targets
        for target in (path, *targets):
            module = target.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            if any(name == module or name.startswith(module + ".") for name in imports):
                protected.add(path)
    return protected


def grade(
    head: dict[str, set[int]],
    head_source: Source,
    runs: Iterator[Measurement],
    renamed: dict[str, str | tuple[str, ...]] | None = None,
    *,
    changed_tests: set[str] | None = None,
) -> Result:
    base = next(runs, None)
    if base is None:
        raise Unmeasured("no measured base run within the searched dev history")
    lost = _lost(base, head, head_source, renamed or {})
    if not lost:
        return Result(base.commit, {})
    protected = _protected(base, head_source, changed_tests or set(), lost, renamed or {})
    result = Result(base.commit, {path: lines for path, lines in lost.items() if path in protected})
    eligible = {path: lines for path, lines in lost.items() if path not in protected}
    if not eligible:
        return result
    histories, versions = _histories(base, eligible, runs)
    for path, by_line in histories.items():
        for line, history in by_line.items():
            if len(versions[path][line]) >= 2 and not any(versions[path][line]):
                result.unconfirmed.setdefault(path, {})[line] = len(versions[path][line])
            elif _flaky(history):
                result.unstable.setdefault(path, []).append(line)
            else:
                result.lost.setdefault(path, []).append(line)
    return result
