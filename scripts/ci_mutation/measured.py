import argparse
import ast
import json
import math
import os
import subprocess
from pathlib import Path

from mutmut.utils.format_utils import get_mutant_name

from scripts.ci_mutation.killers import KillMemory
from scripts.ci_mutation.mutant_shards import function_name
from scripts.ci_mutation.plan import MUTANT_SECONDS, shard_count, shard_workers
from scripts.ci_mutation.scope import discover_changes, select_tests
from scripts.ci_mutation.selection import selected_mutant_names
from scripts.ci_mutation.stats import SharedStats, load_parts, stats_key

# Traced test seconds: every unit of a split mutant reruns the fastest tests, where most mutants die, then one slice.
HEAD_SECONDS = 20.0
SLICE_SECONDS = 120.0


def function_tests(results: list[dict]) -> tuple[dict[str, set[str]], dict[str, float], float]:
    tests, durations = {}, {}
    timed = [result for result in results if result.get("cpu") and result["durations"]]
    # A cached entry per selected path can repeat a test another overlapping path also holds.
    walls = {}
    for result in timed:
        for node, seconds in result["durations"].items():
            walls[node] = max(walls.get(node, 0.0), seconds)
    cpu = sum(result["cpu"] for result in timed)
    # Stats fork a bucket per visible CPU onto a runner whose quota is smaller, so recorded walls wait on each other.
    contention = max(1.0, sum(walls.values()) / cpu) if cpu else 1.0
    for result in results:
        for function, nodes in result["tests"].items():
            tests.setdefault(function, set()).update(nodes)
        for node, seconds in result["durations"].items():
            durations[node] = max(durations.get(node, 0.0), seconds / contention)
    return tests, durations, max([MUTANT_SECONDS, *durations.values()])


def measured_costs(names: list[str], results: list[dict], memory: KillMemory | None = None) -> dict[str, float]:
    tests, durations, missing = function_tests(results)
    functions = {function: sum(durations.get(node, missing) for node in nodes) for function, nodes in tests.items()}
    fallback = max(60.0, max(functions.values(), default=0.0))
    costs = {}
    for name in names:
        function = name.rpartition("__mutmut_")[0]
        # The recorded killer runs first and mutmut stops at the first failing test.
        killer = memory.killer(name) if memory is not None else None
        seconds = (
            durations.get(killer, missing) if killer in tests.get(function, ()) else functions.get(function, fallback)
        )
        costs[name] = MUTANT_SECONDS + 1.25 * seconds
    return costs


def split_tests(nodes: set[str], seconds: dict[str, float], limit: int) -> dict | None:
    ordered = sorted(nodes, key=lambda node: (seconds[node], node))
    head, spent = [], 0.0
    while ordered and (not head or spent + seconds[ordered[0]] <= HEAD_SECONDS):
        head.append(ordered.pop(0))
        spent += seconds[head[-1]]
    count = min(limit, math.ceil(sum(seconds[node] for node in ordered) / SLICE_SECONDS))
    if count < 2:
        return None
    slices, loads = [[] for _ in range(count)], [0.0] * count
    for node in sorted(ordered, key=lambda node: (-seconds[node], node)):
        index = min(range(count), key=lambda index: (loads[index], index))
        slices[index].append(node)
        loads[index] += seconds[node]
    return {
        "head": sorted(head),
        "slices": [sorted(chosen) for chosen in slices],
        "costs": [MUTANT_SECONDS + 1.25 * (spent + load) for load in loads],
    }


def measured_splits(names: list[str], results: list[dict], limit: int = 10) -> dict[str, dict]:
    tests, durations, missing = function_tests(results)
    splits = {}
    for function in sorted({function_name(name) for name in names} & tests.keys()):
        seconds = {node: durations.get(node, missing) for node in tests[function]}
        if split := split_tests(tests[function], seconds, limit):
            splits[function] = split
    return splits


def plan_shards(costs: dict[str, float], target: float = 240, limit: int = 10, splits: dict | None = None) -> dict:
    splits = splits or {}
    units = {name: splits[function]["costs"] for name in costs if (function := function_name(name)) in splits}
    costs = {name: sum(units[name]) if name in units else cost for name, cost in costs.items()}
    items = [(cost, name, None) for name, cost in costs.items() if name not in units]
    items += [(cost, name, position) for name, unit in units.items() for position, cost in enumerate(unit)]
    seconds = sum(costs[name] for name in sorted(costs))
    widest = max(map(len, units.values()), default=1)
    count = max(min(limit, widest), shard_count(seconds, len(items), target, limit))
    loads = [0.0] * count
    assignments = {name: [None] * len(unit) for name, unit in units.items()}
    for cost, name, position in sorted(items, key=lambda item: (-item[0], item[1])):
        taken = assignments[name] if position is not None else []
        index = min((index for index in range(count) if index not in taken), key=lambda index: (loads[index], index))
        if position is None:
            assignments[name] = index
        else:
            assignments[name][position] = index
        loads[index] += cost
    return {
        "shards": count,
        "assignments": assignments,
        "costs": costs,
        "units": "worker seconds",
        "estimated_worker_seconds": seconds,
        "shard_worker_seconds": loads,
        "workers": shard_workers(),
        "splits": {function: splits[function] for function in sorted({function_name(name) for name in units})},
    }


def refine_plan(root: Path, selected: dict, stats: SharedStats) -> dict:
    costs, splits, keys = {}, {}, []
    selected = {
        path: value
        for path, value in selected.items()
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(ast.parse((root / path).read_text()))
        )
    }
    groups = [
        [path for path in selected if path != "scripts/ci_mutation/identity.py"],
        [path for path in selected if path == "scripts/ci_mutation/identity.py"],
    ]
    for index, group in enumerate(group for group in groups if group):
        scope = {path: selected[path] for path in group}
        key = stats_key(stats.head, scope)
        keys.append(key)
        results, reason = load_parts(stats.group(index).folder, key)
        if reason:
            raise ValueError(reason)
        names = []
        for path, (lines, _) in scope.items():
            generated = selected_mutant_names(path, (root / path).read_text(), lines)
            names.extend(get_mutant_name(Path(path), name) for name in generated)
        costs.update(measured_costs(names, results, KillMemory(stats.folder / "killers")))
        splits.update(measured_splits(names, results))
    return {**plan_shards(costs, splits=splits), "keys": keys, "head": stats.head}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bases", required=True)
    parser.add_argument("--stats", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    changes = discover_changes(root, args.bases.split(","), "HEAD")
    planned = root / ".mutation-selection.json"
    selection = json.loads(planned.read_text()) if planned.is_file() else None
    selected = {
        path: (lines, tests)
        for path, lines in changes.items()
        if (tests := selection[path] if selection is not None else select_tests(root, Path(path)))
    }
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    plan = refine_plan(root, selected, SharedStats(args.stats, head))
    (args.stats / "plan.json").write_text(json.dumps(plan))
    print(f"Measured mutation worker seconds: {plan['estimated_worker_seconds']:.1f}")
    print(f"Measured mutation shards: {plan['shards']}; assigned mutants: {len(plan['assignments'])}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"shards={json.dumps(list(range(plan['shards'])))}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
