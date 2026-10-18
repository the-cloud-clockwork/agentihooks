import argparse
import ast
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from scripts.ci_mutation.browser import needs_browser
from scripts.ci_mutation.scope import discover_changes, graded_green, own_bases, select_tests
from scripts.ci_mutation.selection import changed_mutations, file_seconds, part_files, stats_shards
from scripts.ci_test_plan import find_record
from tests.dep_manifest import line_units

# Upper quartile of measured worker seconds per mutant; stored test timings did not predict the cost.
MUTANT_SECONDS = 5.6
WORKERS = 4


def estimate(
    root: Path,
    changes: dict[str, set[int]],
    selection: dict[str, list[str]] | None = None,
    manifest: dict | None = None,
) -> tuple[float, int, list[str]]:
    mutants, tests = 0, set()
    for path, lines in changes.items():
        chosen = select_tests(root, Path(path))
        if manifest is not None:
            chosen = sorted({*chosen, *recorded_tests(root, manifest, path, lines)})
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


def recorded_tests(root: Path, manifest: dict, path: str, lines: set[int]) -> set[str]:
    by_line = line_units((root / path).read_text())
    changed = {f"{path}::{by_line[line]}" for line in lines if line in by_line}
    units = {index for index, unit in enumerate(manifest["units"]) if unit in changed}
    files = {test.partition("::")[0] for test, found in manifest["tests"].items() if units.intersection(found["units"])}
    return {name for name in files if Path(name).name.startswith("test_") and (root / name).is_file()}


def shard_count(seconds: float, mutants: int, target: float, limit: int) -> int:
    return max(1, min(limit, mutants, math.ceil(seconds / (WORKERS * target))))


def slowest_bucket(root: Path, tests: list[str], parts: int) -> float:
    seconds = file_seconds(root, tests)
    buckets = [
        bucket
        for index in range(parts)
        for bucket in stats_shards(root, part_files(root, tests, (index, parts)), WORKERS)
    ]
    return max(sum(seconds[path] for path in bucket) for bucket in buckets)


def stats_part_count(root: Path, tests: list[str], target: float, limit: int) -> tuple[int, float]:
    walls = [slowest_bucket(root, tests, parts) for parts in range(1, limit + 1)]
    # One file never splits, so past the floor another part only adds a job.
    reachable = max(target, min(walls))
    parts = next(parts for parts, wall in enumerate(walls, 1) if wall <= reachable)
    return parts, walls[parts - 1]


def validate_tests(root: Path, tests: list[str]) -> None:
    from tests.test_collection_imports import SDK_GROUP, loads_heavy, loads_sdk_when_run

    for test in tests:
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
    parser.add_argument("--stats-target", type=float, default=30)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--repository")
    args = parser.parse_args()
    root = Path.cwd()
    bases = own_bases(root, args.base, args.head, graded_green)
    print(f"Mutation bases: {' '.join(bases)}")
    changes = discover_changes(root, bases, args.head)
    manifest = None
    if args.repository:
        found = find_record(args.repository, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if found is None:
            print("No dev test dependency manifest found; tests are selected by import only.")
        else:
            run, manifest = found
            print(f"Recorded coverage from dev run {run['id']} adds the tests that executed a changed function.")
    selection = {}
    seconds, mutants, tests = estimate(root, changes, selection, manifest)
    validate_tests(root, tests)
    browser = str(needs_browser(root, tests)).lower()
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
