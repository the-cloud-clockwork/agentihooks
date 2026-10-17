import argparse
import ast
import json
import os
import subprocess
from pathlib import Path

from mutmut.utils.format_utils import get_mutant_name

from scripts.ci_mutation.plan import MUTANT_SECONDS, WORKERS, shard_count
from scripts.ci_mutation.scope import discover_changes, select_tests
from scripts.ci_mutation.selection import selected_mutant_names
from scripts.ci_mutation.stats import SharedStats, load_parts, stats_key


def measured_costs(names: list[str], results: list[dict]) -> dict[str, float]:
    tests, durations = {}, {}
    for result in results:
        for function, nodes in result["tests"].items():
            tests.setdefault(function, set()).update(nodes)
        for node, seconds in result["durations"].items():
            durations[node] = max(durations.get(node, 0.0), seconds)
    missing = max(MUTANT_SECONDS, max(durations.values(), default=0.0))
    functions = {function: sum(durations.get(node, missing) for node in nodes) for function, nodes in tests.items()}
    fallback = max(60.0, max(functions.values(), default=0.0))
    return {name: MUTANT_SECONDS + 1.25 * functions.get(name.rpartition("__mutmut_")[0], fallback) for name in names}


def plan_shards(costs: dict[str, float], target: float = 240, limit: int = 10) -> dict:
    seconds = sum(costs[name] for name in sorted(costs))
    count = shard_count(seconds, len(costs), target, limit)
    loads = [0.0] * count
    assignments = {}
    for name in sorted(costs, key=lambda name: (-costs[name], name)):
        index = min(range(count), key=lambda index: (loads[index], index))
        assignments[name] = index
        loads[index] += costs[name]
    return {
        "shards": count,
        "assignments": assignments,
        "costs": costs,
        "units": "worker seconds",
        "estimated_worker_seconds": seconds,
        "shard_worker_seconds": loads,
        "workers": WORKERS,
    }


def refine_plan(root: Path, selected: dict, stats: SharedStats) -> dict:
    costs, keys = {}, []
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
        costs.update(measured_costs(names, results))
    return {**plan_shards(costs), "keys": keys, "head": stats.head}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bases", required=True)
    parser.add_argument("--stats", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    changes = discover_changes(root, args.bases.split(","), "HEAD")
    selected = {path: (lines, tests) for path, lines in changes.items() if (tests := select_tests(root, Path(path)))}
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
