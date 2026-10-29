import argparse
import ast
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from scripts.ci_mutation.mutant_shards import function_name, holds
from scripts.ci_mutation.stats import SharedStats


def plan_fingerprint(plan: dict) -> str:
    content = {key: value for key, value in plan.items() if key != "probe"}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def probe_plan(plan: dict, shard: tuple[int, int]) -> dict:
    if shard[1] != plan["shards"] or not 0 <= shard[0] < shard[1]:
        raise ValueError("early mutation shard does not match the measured plan")
    names = sorted(name for name in plan["assignments"] if function_name(name) in plan["splits"])
    assignments = {name: [] for name in plan["assignments"]}
    assignments.update({name: index % shard[1] for index, name in enumerate(names)})
    return {
        **{key: value for key, value in plan.items() if key != "probe"},
        "assignments": assignments,
        "probe_mode": True,
    }


def probe_results(plan: dict) -> tuple[dict[str, str], float]:
    if "probe" not in plan:
        return {}, 0
    probe = plan["probe"]
    expected = {name for name in plan["assignments"] if function_name(name) in plan["splits"]}
    seconds = probe["seconds"]
    if (
        probe["plan"] != plan_fingerprint(plan)
        or set(probe["results"]) != expected
        or set(probe["results"].values()) - {"killed", "survived"}
        or not math.isfinite(seconds)
        or seconds < 0
    ):
        raise ValueError("early mutation results are stale, incomplete or invalid")
    return probe["results"], seconds


def read_probe_results(path: Path) -> tuple[dict[str, str], float]:
    return probe_results(json.loads(path.read_text())) if path.is_file() else ({}, 0)


def probe_elapsed(stats: SharedStats | None) -> float:
    return read_probe_results(stats.folder / "plan.json")[1] if stats and stats.part is None else 0


def mark_probe_results(work: Path, results: dict) -> dict:
    early, _ = read_probe_results(work / "shard-plan.json")
    for values in results.values() if early else ():
        for row in values:
            if early.get(row["name"]) == "killed":
                row["reused_early_kill"] = True
    return results


def reuse_kills(run_tests: Callable[..., int], early: dict[str, str]) -> Callable[..., int]:
    def run_cached(self, *, mutant_name, tests):
        if mutant_name is not None and early.get(mutant_name) == "killed":
            return 1
        return run_tests(self, mutant_name=mutant_name, tests=tests)

    return run_cached


def worker_failure(work: Path, reason: str) -> RuntimeError:
    log = work / "run.log"
    detail = log.read_text()[-12000:] if log.is_file() else ""
    return RuntimeError(f"early mutation failed: {reason}\n{detail}")


def merge_probes(plan: dict, records: list[dict]) -> dict:
    total = plan["shards"]
    if len(records) != total or {record["index"] for record in records} != set(range(total)):
        raise ValueError("early mutation results are missing or duplicated")
    results = {}
    for record in records:
        early = probe_plan(plan, (record["index"], total))
        expected = {name for name, assignment in early["assignments"].items() if holds(assignment, record["index"])}
        if (
            record["plan"] != plan_fingerprint(plan)
            or record["total"] != total
            or set(record["results"]) != expected
            or not math.isfinite(record["seconds"])
            or record["seconds"] < 0
        ):
            raise ValueError("early mutation results do not match their revision, selection or shard")
        results.update(record["results"])
    merged = {
        **plan,
        "probe": {
            "plan": plan_fingerprint(plan),
            "seconds": max(record["seconds"] for record in records),
            "results": results,
        },
    }
    probe_results(merged)
    return merged


def run_probe(root: Path, selected: dict, stats: SharedStats, shard: tuple[int, int], budget: float) -> dict:
    from scripts.ci_mutation.runner import mutate_files

    started = time.monotonic()
    plan = json.loads((stats.folder / "plan.json").read_text())
    early = probe_plan(plan, shard)
    owned = {name for name, assignment in early["assignments"].items() if holds(assignment, shard[0])}
    rows = {}
    if owned:
        output = root / ".mutation-probe" / str(shard[0])
        output.mkdir(parents=True, exist_ok=True)
        copied = output / "stats"
        shutil.copytree(stats.folder, copied, dirs_exist_ok=True)
        (copied / "plan.json").write_text(json.dumps(early))
        shared = SharedStats(copied, stats.head)
        groups = [
            [path for path in selected if path != "scripts/ci_mutation/identity.py"],
            [path for path in selected if path == "scripts/ci_mutation/identity.py"],
        ]
        for index, group in enumerate(group for group in groups if group):
            prefixes = tuple(path.removesuffix(".py").replace("/", ".") + "." for path in group)
            if not any(name.startswith(prefixes) for name in owned):
                continue
            work = output / f"group-{index}"
            work.mkdir(exist_ok=True)
            results, reason = mutate_files(
                root, work, {path: selected[path] for path in group}, started + budget, shard, shared.group(index)
            )
            if reason:
                raise worker_failure(work, reason)
            rows.update({row["name"]: row["status"] for values in results.values() for row in values})
    seconds = time.monotonic() - started
    if set(rows) != owned or set(rows.values()) - {"killed", "survived"} or seconds > budget:
        raise ValueError("early mutation did not completely grade its assigned mutants within budget")
    return {"plan": plan_fingerprint(plan), "index": shard[0], "total": shard[1], "seconds": seconds, "results": rows}


def main() -> int:
    from scripts.ci_mutation.scope import discover_changes, select_tests

    parser = argparse.ArgumentParser()
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--bases")
    parser.add_argument("--shard", type=int)
    parser.add_argument("--shards", type=int)
    parser.add_argument("--budget", type=float, default=1080)
    parser.add_argument("--merge", action="store_true")
    args = parser.parse_args()
    plan_path = args.stats / "plan.json"
    if args.merge:
        records = [json.loads(path.read_text()) for path in args.stats.glob("probe-*.json")]
        plan = merge_probes(json.loads(plan_path.read_text()), records)
        plan_path.write_text(json.dumps(plan))
        print(
            f"Shared early mutations: {len(plan['probe']['results'])}; charged seconds: {plan['probe']['seconds']:.3f}"
        )
        return 0
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    root = Path.cwd()
    if os.environ.get("MUTATION_STATS_CACHE"):
        os.environ["MUTATION_STATS_ROOT"] = str(root)
    changes = discover_changes(root, args.bases.split(","), "HEAD")
    planned = root / ".mutation-selection.json"
    selection = json.loads(planned.read_text()) if planned.is_file() else None
    selected = {
        path: (lines, tests)
        for path, lines in changes.items()
        if (tests := selection[path] if selection is not None else select_tests(root, Path(path)))
        if any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(ast.parse((root / path).read_text()))
        )
    }
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    record = run_probe(root, selected, SharedStats(args.stats, head), (args.shard, args.shards), args.budget)
    (args.stats / f"probe-{args.shard}.json").write_text(json.dumps(record))
    print(json.dumps(record), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
