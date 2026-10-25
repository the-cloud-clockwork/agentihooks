import argparse
import ast
import json
import math
import os
import subprocess
import time
import zipfile
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path

from scripts.ci_cpu_quota import worker_count as quota_workers
from scripts.ci_mutation.browser import needs_browser
from scripts.ci_mutation.scope import discover_changes, graded_green, own_bases, select_tests, test_file
from scripts.ci_mutation.selection import changed_mutations, file_seconds, split_parts, stats_shards
from scripts.ci_test_plan import find_record
from tests.dep_manifest import GLOBAL_FILES, MEASURED, _spans, changed_units, line_units, node
from tests.dep_manifest import changes as recorded_changes

# Upper quartile of measured worker seconds per mutant; stored test timings did not predict the cost.
MUTANT_SECONDS = 5.6
WORKERS = 4
RECORD_RETRY_SECONDS = 10
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
        if path.startswith("tests/"):
            # A changed helper, conftest or fixture can move what any test reaches.
            if not (path.endswith(".py") and Path(path).name.startswith("test_")):
                return None
            tests.add(path)
        elif path in {*GLOBAL_FILES, ".github/workflows/test.yml"} or (
            path.endswith(".py") and not path.startswith(MEASURED)
        ):
            return None
        elif path.endswith(".py"):
            units.update(index[unit] for name in changed_units(old, new) if (unit := f"{path}::{name}") in index)
        else:
            paths.add(path)
    return tests, units, paths


def changed_recorded_units(root: Path, manifest: dict, changes: dict[str, set[int]]) -> tuple[set[int], set[str]]:
    index = {unit: number for number, unit in enumerate(manifest["units"])}
    units, unrecorded = set(), set()
    for path, lines in changes.items():
        spans = _spans(ast.parse((root / path).read_text()))
        for line in lines:
            if inside := [span for span in spans if span[0] <= line <= span[2]]:
                unit = f"{path}::{max(inside)[3]}"
                if unit in index:
                    units.add(index[unit])
                else:
                    unrecorded.add(path)
    return units, unrecorded


def covering_tests(manifest: dict, units: set[int], paths: set[str]) -> set[str]:
    # A child process loads the selection light bootstrap, whose trampoline hit records nothing, so a test reaching
    # code only there never enters mutmut's statistics and grades nothing.
    return {
        node(test).partition("[")[0]
        for test, found in manifest["tests"].items()
        if units.intersection(found["units"]) or paths.intersection(found.get("files", ()))
    }


def recorded_lines(root: Path, manifest: dict, changes: dict[str, set[int]]) -> tuple[dict[str, set[int]], set[int]]:
    lines = {path: set(found) for path, found in changes.items()}
    fallback = set()
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
        by_line = line_units(old)
        if any(line not in by_line for line in affected) or "<module>" in changed_units(old, new):
            fallback.update(number for unit, number in index.items() if unit.startswith(f"{path}::"))
        lines[path] = affected | {mapping[line] for line in changes.get(path, ()) if line in mapping}
    return lines, fallback


def line_covering_tests(root: Path, manifest: dict, changes: dict[str, set[int]], paths: set[str]) -> set[str]:
    lines, fallback = recorded_lines(root, manifest, changes)
    return {
        node(test).partition("[")[0]
        for test, found in manifest["tests"].items()
        if test not in manifest["lines"]
        or fallback.intersection(found["units"])
        or paths.intersection(found.get("files", ()))
        or any(changed.intersection(manifest["lines"][test].get(path, ())) for path, changed in lines.items())
    }


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
        for index in range(parts)
        for bucket in stats_shards(root, split_parts(tests, seconds, (index, parts)), shard_workers(), seconds)
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
