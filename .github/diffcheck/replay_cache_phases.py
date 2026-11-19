import functools
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

root, evidence, folder, code = (Path(value).resolve() for value in sys.argv[1:5])
sys.path.insert(0, str(code))

from scripts.ci_mutation import stats_cache as sc  # noqa: E402
from scripts.ci_mutation.stats_dependencies import StoredStats, cap  # noqa: E402

totals: dict[str, float] = {}


def timed(label, function, *args, **kwargs):
    start = time.perf_counter()
    try:
        return function(*args, **kwargs)
    finally:
        totals[label] = totals.get(label, 0.0) + time.perf_counter() - start


def show(label: str, seconds: float) -> None:
    print(f"{label}: {seconds:.1f} s", flush=True)


changes_raw = json.loads((evidence / "changed-lines.json").read_text())
changes = {path: sorted(change["lines"]) for path, change in changes_raw.items()}
start = time.perf_counter()
fresh = [json.loads((evidence / f"stats-{index}.json").read_text()) for index in range(3)]
show("read three bucket outputs", time.perf_counter() - start)
missing = [entry for result in fresh for entry in result["files"]]
print(f"entries {len(missing)}, distinct {len(set(missing))}", flush=True)


def context_parts():
    tracked = subprocess.check_output(sc.TRACKED, cwd=root, text=True).split("\0")
    sources = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in tracked
        if path and (path in sc.CONFIGURATION or path.startswith("scripts/ci_mutation/")) and (root / path).is_file()
    }
    versions = sc.distribution_versions(root)
    context = dict(zip(sc.CONTEXT_FIELDS, (sc.SCHEMA, sources, ["-q"], [sys.version, sys.platform, versions])))
    context["inventory"] = sorted(filter(None, tracked))
    modules = {sc.module_name(Path(path)): path for path in tracked if Path(path).suffix == ".py"}
    resources = {}
    for path in tracked:
        if path and path not in sc.UNKEYED and (root / path).is_file():
            parts = Path(path).parts
            for index in range(len(parts)):
                resources.setdefault("/".join(parts[index:]), set()).add(path)
            for parent in Path(path).parents:
                resources.setdefault(str(parent) + "/", set()).add(path)
    return tracked, context, modules, resources


start = time.perf_counter()
tracked, context, modules, resources = context_parts()
show(f"context, {len(tracked)} tracked, {len(modules)} modules, {len(resources)} resource keys", time.perf_counter() - start)

folder.mkdir(parents=True, exist_ok=True)
inputs = sc.StatsInputs(root, modules, resources, changes)
stored = StoredStats(root, folder, context, changes)
save_start = time.perf_counter()
functions = {function for result in fresh for function in result["tests"]}
sliced = timed("fresh_by_path", sc.fresh_by_path, missing, fresh)
saved = 0
input_counts = []
for path in missing:
    result, covered = timed("per_test_stats", sc.per_test_stats, path, sliced[path], len(missing), functions)
    if result is not None:
        dependencies = timed("for_test", inputs.for_test, path, covered, fresh)
        discovery = timed("discovery", inputs.discovery, dependencies)
        timed("save", stored.save, path, result, dependencies, covered is not None, discovery)
        saved += 1
        input_counts.append(len(dependencies))
timed("cap", cap, folder)
show(f"SAVE PHASE total ({saved} of {len(missing)} entries saved, {len(functions)} functions)", time.perf_counter() - save_start)
for label in ("fresh_by_path", "per_test_stats", "for_test", "discovery", "save", "cap"):
    show(f"  {label}", totals.get(label, 0.0))
if input_counts:
    print(f"  inputs per saved record: min {min(input_counts)} max {max(input_counts)}", flush=True)

totals.clear()
inputs = sc.StatsInputs(root, modules, resources, changes)
stored = StoredStats(root, folder, context, changes)
load_start = time.perf_counter()
timed("prune", stored.prune)
original_matches = inputs.matches


def matches(path, recorded, measured, discovery):
    return timed("matches", original_matches, path, recorded, measured, discovery)


hits = 0
for path in missing:
    record = timed("load", stored.load, path, functools.partial(matches, path))
    hits += record is not None
show(f"LOOKUP PHASE total ({hits} hits of {len(missing)}, misses {dict(stored.misses)})", time.perf_counter() - load_start)
for label in ("prune", "load", "matches"):
    show(f"  {label}", totals.get(label, 0.0))
show("  load without matches", totals.get("load", 0.0) - totals.get("matches", 0.0))
