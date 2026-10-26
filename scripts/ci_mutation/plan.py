import argparse
import ast
import json
import math
import os
import re
import subprocess
import time
import zipfile
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import cache
from itertools import accumulate
from pathlib import Path

import yaml

from scripts.ci_cpu_quota import worker_count as quota_workers
from scripts.ci_mutation.browser import needs_browser
from scripts.ci_mutation.scope import discover_changes, graded_green, own_bases, select_tests, test_file
from scripts.ci_mutation.selection import changed_mutations, file_seconds, partition, stats_shards
from scripts.ci_test_plan import find_record
from tests.dep_manifest import (
    GLOBAL_FILES,
    MEASURED,
    _git,
    _show,
    _spans,
    changed_units,
    line_units,
    node,
)
from tests.dep_manifest import changes as recorded_changes

# Upper quartile of measured worker seconds per mutant; stored test timings did not predict the cost.
MUTANT_SECONDS = 5.6
WORKERS = 4
RECORD_RETRY_SECONDS = 10
TEST_WORKFLOW = ".github/workflows/test.yml"
COLLECTING_JOBS = {
    TEST_WORKFLOW: ("unit", "unit-selected", "mutation-stats"),
    ".github/workflows/mutation-preflight.yml": ("stats",),
}
SCHEDULING_KEYS = {"needs", "if", "timeout-minutes", "outputs"}
RECORD_ERRORS = (subprocess.SubprocessError, OSError, ValueError, KeyError, zipfile.BadZipFile)


def shard_workers() -> int:
    quota = os.environ.get("MUTATION_SHARD_CPU_QUOTA")
    return quota_workers(WORKERS, float(quota) if quota else None)


def estimate(
    root: Path,
    changes: dict[str, set[int]],
    selection: dict[str, list[str]] | None = None,
    manifest: dict | None = None,
    everything: bool = False,
) -> tuple[float, int, list[str]]:
    mutants, tests = 0, set()
    drift = record_drift(root, manifest) if manifest is not None else None
    if drift is not None:
        stale, drifted, paths = drift
        units, unrecorded = changed_recorded_units(root, manifest, changes)
        if "lines" in manifest:
            covering = line_covering_tests(root, manifest, changes, paths)
        else:
            covering = covering_tests(manifest, units | drifted, paths)
        recorded = {}
        for test in manifest["tests"]:
            if not manifest["tests"][test]["units"] or "lines" in manifest and test not in manifest["lines"]:
                stale.add(test_file(test))
                continue
            path, _, name = node(test).partition("[")[0].partition("::")
            recorded.setdefault(path, set()).add(name)
    for path, lines in changes.items():
        chosen = every_test(root) if everything else select_tests(root, Path(path))
        if manifest is not None:
            chosen = sorted({*chosen, *recorded_tests(root, manifest, path, lines)})
        if drift is not None and path not in unrecorded:
            chosen = narrow(root, chosen, covering, recorded, stale)
        if selection is not None:
            selection[path] = chosen
        if not chosen:
            if any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for node in ast.walk(ast.parse((root / path).read_text()))
            ):
                raise SystemExit(f"{path}: not mutated, no matching or importing test modules")
            continue
        tests.update(chosen)
        _, mutations = changed_mutations(path, (root / path).read_text(), lines)
        # mutmut emits only mutants inside a function or method.
        mutants += sum(1 for mutation in mutations if mutation.contained_by_top_level_function)
    return mutants * MUTANT_SECONDS, mutants, sorted(tests)


def every_test(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py") if path.is_file())


def record_failure(error: Exception) -> str:
    detail = getattr(error, "stderr", None)
    if isinstance(detail, bytes):
        detail = detail.decode(errors="replace")
    return f"{type(error).__name__}: {(detail or '').strip() or error}"


def read_record(repository: str) -> tuple[tuple[dict, dict] | None, str]:
    before = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for attempt in (1, 2):
        try:
            return find_record(repository, before), ""
        except RECORD_ERRORS as error:
            reason = record_failure(error)
            print(f"Reading the recorded dev coverage failed on attempt {attempt}: {reason}")
        if attempt == 1:
            time.sleep(RECORD_RETRY_SECONDS)
    return None, reason


def recorded_tests(root: Path, manifest: dict, path: str, lines: set[int]) -> set[str]:
    by_line = line_units((root / path).read_text())
    changed = {f"{path}::{by_line[line]}" for line in lines if line in by_line}
    units = {index for index, unit in enumerate(manifest["units"]) if unit in changed}
    files = {test.partition("::")[0] for test, found in manifest["tests"].items() if units.intersection(found["units"])}
    return {name for name in files if Path(name).name.startswith("test_") and (root / name).is_file()}


def record_drift(root: Path, manifest: dict) -> tuple[set[str], set[int], set[str]] | None:
    if manifest.get("commit") is None:
        return None
    index = {unit: number for number, unit in enumerate(manifest["units"])}
    tests, units, paths = set(), set(), set()
    for path, old, new in recorded_changes(root, manifest["commit"], "HEAD"):
        if path.startswith(".github/workflows/"):
            if workflow_changed(root, manifest, path):
                return None
            paths.add(path)
        elif path.startswith("tests/") and path.endswith(".py"):
            reached = helper_tests(root, path)
            if reached is None:
                return None
            tests.update(reached)
        elif path in GLOBAL_FILES or (path.endswith(".py") and not path.startswith(MEASURED)):
            return None
        elif path.endswith(".py"):
            units.update(index[unit] for name in changed_units(old, new) if (unit := f"{path}::{name}") in index)
        else:
            paths.add(path)
    return tests, units, paths


def workflow_changed(root: Path, manifest: dict, path: str) -> bool:
    if manifest.get("version") != 2 or path.endswith(".py"):
        return True
    return path in COLLECTING_JOBS and collection_changed(
        _show(root, manifest["commit"], path), _show(root, "HEAD", path), COLLECTING_JOBS[path]
    )


def collection_changed(old: str | None, new: str | None, jobs: tuple[str, ...]) -> bool:
    # Only the jobs that run the recorded tests, and the jobs whose outputs they read, decide what a test reaches.
    try:
        before, after = (yaml.safe_load(text) for text in (old, new))
        if not all(isinstance(workflow["jobs"][jobs[0]], dict) for workflow in (before, after)):
            return True
        if any(before.get(key) != after.get(key) for key in ("env", "defaults")):
            return True
        named = set(jobs)
        for workflow in (before, after):
            for name in jobs:
                needs = workflow["jobs"].get(name, {}).get("needs", [])
                named.update([needs] if isinstance(needs, str) else needs)
        for name in named:
            old_job, new_job = (
                {key: value for key, value in workflow["jobs"].get(name, {}).items() if key not in SCHEDULING_KEYS}
                for workflow in (before, after)
            )
            if old_job != new_job:
                return True
        return False
    except (TypeError, KeyError, AttributeError, yaml.YAMLError):
        return True


def helper_tests(root: Path, path: str) -> set[str] | None:
    importers = _importers(root)
    reached = [path]
    for current in reached:
        module = current.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        keys = {module, current}
        if Path(current).name in ("conftest.py", "__init__.py"):
            folder = f"{Path(current).parent.as_posix()}/"
            found = {name for name in importers["*"] if name.startswith(folder)}
        else:
            keys.update((Path(current).stem, Path(current).name))
            found = set()
        found.update(name for key in keys for name in importers.get(key, ()))
        reached.extend(sorted(found.difference(reached)))
    if any(not found.startswith("tests/") for found in reached):
        return None
    return {found for found in reached if Path(found).name.startswith("test_")}


@cache
def _importers(root: Path) -> dict[str, set[str]]:
    found = {"*": set()}
    for path in _git(root, "ls-files", "--", "hooks/*.py", "scripts/*.py", "tests/*.py").splitlines():
        found["*"].add(path)
        for key in _reach_keys(path, (root / path).read_text(errors="replace")):
            found.setdefault(key, set()).add(path)
    return found


def _reach_keys(path: str, source: str) -> set[str]:
    tokens = (token.rstrip(".") for token in re.split(r"[^\w./-]+", source))
    keys = {token for token in tokens if token.endswith(".py")}
    keys.update(Path(key).name for key in list(keys))
    names = [*re.findall(r"\b(?:tests|hooks|scripts)(?:\.\w+)+", source), *_imported(path, source)]
    for name in names:
        keys.update(accumulate(name.split("."), lambda prefix, part: f"{prefix}.{part}"))
    return keys


def _imported(path: str, source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    package = path.removesuffix(".py").replace("/", ".").split(".")
    names = []
    for found in ast.walk(tree):
        if isinstance(found, ast.Import):
            names.extend(alias.name for alias in found.names)
        elif isinstance(found, ast.ImportFrom):
            parent = package[: len(package) - found.level] if found.level else []
            base = ".".join([*parent, *filter(None, [found.module])])
            names.append(base)
            names.extend(f"{base}.{alias.name}" if base else alias.name for alias in found.names)
    return names


def changed_recorded_units(root: Path, manifest: dict, changes: dict[str, set[int]]) -> tuple[set[int], set[str]]:
    index = {unit: number for number, unit in enumerate(manifest["units"])}
    units, unrecorded = set(), set()
    for path, lines in changes.items():
        tree = ast.parse((root / path).read_text())
        spans = _spans(tree)
        new = new_functions(_show(root, manifest["commit"], path), tree)
        for line in lines:
            if inside := [span for span in spans if span[0] <= line <= span[2]]:
                unit = f"{path}::{max(inside)[3]}"
                if unit in index:
                    units.add(index[unit])
                elif max(inside)[3].partition(".")[0] not in new:
                    unrecorded.add(path)
    return units, unrecorded


def new_functions(old: str | None, tree: ast.Module) -> set[str]:
    if old is None:
        return set()
    try:
        known = _known_names(ast.parse(old))
    except SyntaxError:
        return set()
    return {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not statement.decorator_list
        and statement.name not in known
        and any(
            isinstance(found, ast.Name) and found.id == statement.name
            for other in tree.body
            if other is not statement
            for found in ast.walk(other)
        )
    }


def covering_tests(manifest: dict, units: set[int], paths: set[str]) -> set[str]:
    # A child process loads the selection light bootstrap, whose trampoline hit records nothing, so a test reaching
    # code only there never enters mutmut's statistics and grades nothing.
    return {
        node(test).partition("[")[0]
        for test, found in manifest["tests"].items()
        if units.intersection(found["units"]) or paths.intersection(found.get("files", ()))
    }


def _statement(node: ast.stmt) -> str | tuple:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return (node.name, *map(ast.dump, node.decorator_list))
    if isinstance(node, ast.ClassDef):
        heads = [*node.bases, *node.keywords, *node.decorator_list]
        return (node.name, *map(ast.dump, heads), *map(_statement, node.body))
    return ast.dump(node)


def _module_key(node: ast.stmt) -> str | tuple:
    if isinstance(node, ast.ClassDef):
        heads = [*node.bases, *node.keywords, *node.decorator_list]
        kept = (
            item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            or isinstance(item, (ast.Assign, ast.AnnAssign))
            and isinstance(item.value, ast.Dict)
        )
        return (node.name, *map(ast.dump, heads), *map(_module_key, kept))
    return _statement(node)


def _module_keys(source: str) -> list:
    try:
        return [_module_key(node) for node in ast.parse(source).body]
    except SyntaxError:
        return [source]


def binds_only_new_names(old: str, new: str) -> bool:
    try:
        before, after = ast.parse(old), ast.parse(new)
    except SyntaxError:
        return False
    pending, bound = list(before.body), set()
    for statement in after.body:
        match = next((kept for kept in pending[:1] if _statement(kept) == _statement(statement)), None)
        if match is None:
            match = _extended_table(pending[:1], statement)
        if match is None and isinstance(statement, ast.ImportFrom):
            match = _extended_import(pending, statement)
            present = {ast.dump(alias) for alias in match.names} if match else set()
            bound.update(_bound(alias) for alias in statement.names if ast.dump(alias) not in present)
        elif match is None and isinstance(statement, ast.Import):
            bound.update(_bound(alias) for alias in statement.names)
        elif (
            match is None
            and isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not statement.decorator_list
        ):
            bound.add(statement.name)
        elif (
            match is None
            and isinstance(statement, ast.Assign)
            and all(isinstance(t, ast.Name) for t in statement.targets)
        ):
            bound.update(target.id for target in statement.targets)
        elif match is None:
            return False
        if match is not None:
            pending.remove(match)
    return not pending and "*" not in bound and not bound & _known_names(before)


def _extended_table(candidates: list[ast.stmt], statement: ast.stmt) -> ast.Assign | None:
    return next(
        (
            kept
            for kept in candidates
            if isinstance(kept, ast.Assign)
            and isinstance(statement, ast.Assign)
            and all(isinstance(target, ast.Name) for target in statement.targets)
            and list(map(ast.dump, kept.targets)) == list(map(ast.dump, statement.targets))
            and _adds_entries(kept.value, statement.value)
        ),
        None,
    )


def _adds_entries(old: ast.expr, new: ast.expr) -> bool:
    if type(old) is not type(new) or not isinstance(old, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
        return False
    before, after = (
        list(zip(table.keys, table.values)) if isinstance(table, ast.Dict) else [(None, item) for item in table.elts]
        for table in (old, new)
    )
    kept = [_entry(entry) for entry in before]
    added, position = [], 0
    for entry in after:
        if kept[position : position + 1] == [_entry(entry)]:
            position += 1
        else:
            added.append(entry)
    if (
        position < len(kept)
        or not added
        or any(isinstance(node, ast.Call) for entry in added for node in ast.walk(entry[1]))
    ):
        return False
    if isinstance(old, ast.Dict):
        if not all(isinstance(key, ast.Constant) for key in [*old.keys, *(key for key, _ in added)]):
            return False
        keys = [key.value for key, _ in added]
        return len(set(keys)) == len(keys) and not set(keys) & {key.value for key in old.keys}
    return isinstance(old, ast.Set) or list(map(_entry, after[: len(kept)])) == kept


def _entry(entry: tuple[ast.expr | None, ast.expr]) -> tuple[str, ...]:
    return tuple(map(ast.dump, filter(None, entry)))


def _bound(alias: ast.alias) -> str:
    return (alias.asname or alias.name).partition(".")[0]


def _extended_import(pending: list[ast.stmt], statement: ast.ImportFrom) -> ast.ImportFrom | None:
    names = {ast.dump(alias) for alias in statement.names}
    return next(
        (
            kept
            for kept in pending[:1]
            if isinstance(kept, ast.ImportFrom)
            and (kept.module, kept.level) == (statement.module, statement.level)
            and {ast.dump(alias) for alias in kept.names} <= names
        ),
        None,
    )


def _known_names(tree: ast.Module) -> set[str]:
    known = set()
    for found in ast.walk(tree):
        if isinstance(found, ast.Name):
            known.add(found.id)
        elif isinstance(found, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            known.add(found.name)
        elif isinstance(found, (ast.Import, ast.ImportFrom)):
            known.update(_bound(alias) for alias in found.names)
    return known


def recorded_lines(
    root: Path, manifest: dict, changes: dict[str, set[int]]
) -> tuple[dict[str, set[int]], set[int], set[tuple[str, str]]]:
    lines = {path: set(found) for path, found in changes.items()}
    fallback, tables = set(), set()
    index = {unit: number for number, unit in enumerate(manifest["units"])}
    for path, old, new in recorded_changes(root, manifest["commit"], "HEAD"):
        if not path.endswith(".py") or not path.startswith(MEASURED):
            continue
        if old is None or new is None:
            fallback.update(number for unit, number in index.items() if unit.startswith(f"{path}::"))
            continue
        mapping, affected = {}, set()
        for kind, a, b, c, d in SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False).get_opcodes():
            if kind == "equal" or kind == "replace" and b - a == d - c:
                mapping.update(zip(range(c + 1, d + 1), range(a + 1, b + 1)))
                if kind == "replace":
                    affected.update(range(a + 1, b + 1))
            else:
                for source, start, end in ((old, a, b), (new, c, d)):
                    for first, _, last, name in _spans(ast.parse(source)):
                        if first <= end and start < last and (unit := f"{path}::{name}") in index:
                            fallback.add(index[unit])
        headers = {
            line: index[unit]
            for first, body, _, name in _spans(ast.parse(old))
            if (unit := f"{path}::{name}") in index
            for line in range(first, body)
        }
        fallback.update(headers[line] for line in affected if line in headers)
        if not binds_only_new_names(old, new) and _module_keys(old) != _module_keys(new):
            fallback.update(number for unit, number in index.items() if unit.startswith(f"{path}::"))
        extended = extended_tables(old, new)
        tables.update((path, name) for name in extended)
        lines[path] = (
            affected
            | {mapping[line] for line in changes.get(path, ()) if line in mapping}
            | table_reads(ast.parse(old), extended)
        )
    return lines, fallback, tables


def extended_tables(old: str, new: str) -> dict[str, bool]:
    before = list(ast.parse(old).body)
    return {
        target.id: isinstance(statement.value, ast.Dict)
        for statement in ast.parse(new).body
        if _extended_table(before, statement)
        for target in statement.targets
    }


def table_reads(tree: ast.Module, tables: dict[str, bool]) -> set[int]:
    # A lookup that raises on a missing key reaches a new entry only for a test that passes that new key.
    guarded = {
        id(node)
        for found in ast.walk(tree)
        if isinstance(found, ast.Try) and found.handlers
        for statement in found.body
        for node in ast.walk(statement)
    }
    keyed = {
        id(found.value) for found in ast.walk(tree) if isinstance(found, ast.Subscript) and id(found) not in guarded
    }
    return {
        found.lineno
        for found in ast.walk(tree)
        if isinstance(found, ast.Name)
        and found.id in tables
        and isinstance(found.ctx, ast.Load)
        and not (tables[found.id] and id(found) in keyed)
    }


def line_covering_tests(root: Path, manifest: dict, changes: dict[str, set[int]], paths: set[str]) -> set[str]:
    lines, fallback, tables = recorded_lines(root, manifest, changes)
    return {
        node(test).partition("[")[0]
        for test, found in manifest["tests"].items()
        if test not in manifest["lines"]
        or fallback.intersection(found["units"])
        or paths.intersection(found.get("files", ()))
        or any(changed.intersection(manifest["lines"][test].get(path, ())) for path, changed in lines.items())
        or any(
            path in manifest["lines"][test] and re.search(rf"\b{name}\b", file_text(root / test_file(test)))
            for path, name in tables
        )
    }


@cache
def file_text(path: Path) -> str:
    return path.read_text() if path.is_file() else ""


@cache
def defined_tests(path: Path) -> set[str]:
    return {name for _, _, _, name in _spans(ast.parse(path.read_text())) if name.rpartition(".")[2].startswith("test")}


def narrow(
    root: Path, chosen: list[str], covering: set[str], recorded: dict[str, set[str]], stale: set[str]
) -> list[str]:
    kept = set()
    for path in chosen:
        defined = {name.replace(".", "::") for name in defined_tests(root / path)}
        if path in stale or not defined <= recorded.get(path, set()):
            kept.add(path)
        else:
            kept.update(test for test in covering if test_file(test) == path)
    return sorted(kept) or chosen


def shard_count(seconds: float, mutants: int, target: float, limit: int) -> int:
    return max(1, min(limit, mutants, math.ceil(seconds / (shard_workers() * target))))


def slowest_bucket(root: Path, tests: list[str], parts: int, seconds: dict[str, float] | None = None) -> float:
    if seconds is None:
        seconds = file_seconds(root, tests)
    buckets = [
        bucket
        for chosen in partition(tests, seconds, parts)
        for bucket in stats_shards(root, chosen, shard_workers(), seconds)
    ]
    return max(sum(seconds[path] for path in bucket) for bucket in buckets)


def stats_part_count(root: Path, tests: list[str], target: float, limit: int) -> tuple[int, float]:
    seconds = file_seconds(root, tests, split=True)
    tests = sorted(seconds)
    walls = [slowest_bucket(root, tests, parts, seconds) for parts in range(1, limit + 1)]
    reachable = max(target, min(walls))
    parts = next(parts for parts, wall in enumerate(walls, 1) if wall <= reachable)
    return parts, walls[parts - 1]


def validate_tests(root: Path, tests: list[str]) -> None:
    from tests.test_collection_imports import SDK_GROUP, loads_heavy, loads_sdk_when_run

    for test in sorted({test_file(entry) for entry in tests}):
        path = root / test
        if loads_heavy(test.removesuffix(".py").replace("/", "."), set()):
            raise SystemExit(f"{test}: loads the MCP SDK while collecting")
        source = path.read_text()
        if loads_sdk_when_run(source) and SDK_GROUP not in source:
            raise SystemExit(f"{test}: loads the MCP SDK without the mcp-sdk worker group")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--target", type=float, default=120)
    parser.add_argument("--stats-target", type=float, default=60)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--repository")
    args = parser.parse_args()
    root = Path.cwd()
    bases = own_bases(root, args.base, args.head, graded_green)
    print(f"Mutation bases: {' '.join(bases)}")
    changes = discover_changes(root, bases, args.head)
    manifest, failure = None, ""
    if args.repository and not changes:
        print("No change remains to mutate, so the recorded dev coverage is not read.")
    elif args.repository:
        found, failure = read_record(args.repository)
        if failure:
            print(
                "::warning title=Mutation selection fallback::The recorded dev coverage could not be read, so every"
                f" test module is selected for each changed file: {failure}"
            )
        elif found is None:
            print("No dev test dependency manifest found; tests are selected by import only.")
        else:
            run, manifest = found
            print(f"Recorded coverage from dev run {run['id']} adds the tests that executed a changed function.")
    selection = {}
    seconds, mutants, tests = estimate(root, changes, selection, manifest, bool(failure))
    validate_tests(root, tests)
    browser = str(needs_browser(root, sorted({test_file(test) for test in tests}))).lower()
    (root / ".mutation-selection.json").write_text(json.dumps(selection) + "\n")
    count = shard_count(seconds, mutants, args.target, args.limit)
    parts, stats = stats_part_count(root, tests, args.stats_target, args.limit)
    print(
        f"Changed line mutants: {mutants}\nEstimated mutation seconds: {seconds:.0f}, slowest stats bucket: {stats:.0f}"
    )
    print(f"Mutation shards: {count}\nStats parts: {parts}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"shards={json.dumps(list(range(count)))}\n")
            stream.write(f"stats_parts={json.dumps(list(range(parts)))}\n")
            stream.write(f"bases={','.join(bases)}\n")
            stream.write(f"changed={len(changes)}\n")
            stream.write(f"browser={browser}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
