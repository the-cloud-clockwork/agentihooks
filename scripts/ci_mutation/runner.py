import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import tomlkit

from scripts.ci_mutation.clearances import load_clearances
from scripts.ci_mutation.mutant_shards import holds, split_names, unit_cost
from scripts.ci_mutation.report import evaluate, survivor_text
from scripts.ci_mutation.scope import select_tests
from scripts.ci_mutation.stats import SharedStats, load_parts, stats_key

IDENTITY = "scripts/ci_mutation/identity.py"


def run_process(command: list[str], cwd: Path, timeout: float, log: Path) -> int | None:
    with log.open("w") as stream:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return None


def failing_tests(log: Path) -> list[str]:
    if not log.is_file():
        return []
    named = {}
    for line in log.read_text(errors="replace").splitlines():
        kind, _, rest = line.partition(" ")
        test = rest.partition(" - ")[0]
        if kind in ("FAILED", "ERROR") and ".py" in test:
            named.setdefault(test, rest)
    return list(named.values())


def prepare_workspace(root: Path, work: Path, paths: list[str], tests: list[str]) -> None:
    for name in (
        "hooks",
        "scripts",
        "tests",
        "profiles",
        "docs",
        ".github",
        "evidence",
        "observability",
        "docker/swarm-node",
        ".agentihooks/conditions",
    ):
        source = root / name
        if source.is_dir():
            shutil.copytree(source, work / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", work.name))
    root_files = sorted(
        path.name
        for path in root.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and path.name != "pyproject.toml"
        and (path.name == ".test_durations" or not path.name.startswith("."))
    )
    for name in root_files:
        shutil.copy(root / name, work / name)
    pytest_args = ["-q", "-x", "-o", "addopts=", "-p", "pytest_asyncio.plugin"]
    # pytest would load the plugin from its own mutated copy, whose hooks raise in mutmut's forced fail run.
    if IDENTITY not in paths:
        pytest_args += ["-p", "scripts.ci_mutation.identity", *(f"--mutated-path={path}" for path in paths)]
    project = tomlkit.parse((root / "pyproject.toml").read_text())
    project["tool"]["mutmut"] = {
        "source_paths": ["hooks/", "scripts/"],
        "only_mutate": paths,
        "also_copy": [
            "profiles/",
            "docs/",
            ".github/",
            "evidence/",
            "observability/",
            "docker/swarm-node/",
            ".agentihooks/conditions/",
            *root_files,
        ],
        "pytest_add_cli_args_test_selection": tests,
        "pytest_add_cli_args": pytest_args,
    }
    (work / "pyproject.toml").write_text(tomlkit.dumps(project))


def mutate_files(
    root: Path,
    work: Path,
    selected: dict[str, tuple[set[int], list[str]]],
    deadline: float,
    shard: tuple[int, int],
    stats: SharedStats | None,
) -> tuple[dict[str, list[dict]], str]:
    tests = sorted({test for _, chosen in selected.values() for test in chosen})
    prepare_workspace(root, work, list(selected), tests)
    selection = work / "changed-lines.json"
    selection.write_text(
        json.dumps({path: {"lines": sorted(lines), "tests": chosen} for path, (lines, chosen) in selected.items()})
    )
    log = work / "run.log"
    shard_args = [str(part) for part in shard]
    stats_args = []
    if stats:
        key = stats_key(stats.head, selected)
        if stats.part:
            stats_args = ["collect", str(stats.folder / f"part-{stats.part[0]}.json"), key, *map(str, stats.part)]
        else:
            results, reason = load_parts(stats.folder, key)
            if reason:
                return {}, reason
            plan_path = stats.folder.parent / "plan.json"
            if plan_path.is_file():
                plan = json.loads(plan_path.read_text())
                if plan["head"] != stats.head or key not in plan["keys"]:
                    return {}, "measured mutation plan stale for this commit or selection"
                shutil.copy(plan_path, work / "shard-plan.json")
            (work / "shared-stats.json").write_text(json.dumps(results))
            stats_args = ["reuse", str(work / "shared-stats.json")]
    status = run_process(
        [sys.executable, "-m", "scripts.ci_mutation.selection", str(selection), *shard_args, *stats_args],
        work,
        deadline - time.monotonic(),
        log,
    )
    if status is None:
        return {}, "over budget"
    if status != 0:
        if failing := failing_tests(log):
            return {}, f"mutmut failed with exit {status}; failing tests: {'; '.join(failing)}; see {log}"
        return {}, f"mutmut failed with exit {status}; see {log}"
    if stats and stats.part:
        return {path: [] for path in selected}, ""
    result_path = work / "results.json"
    status = run_process(
        [sys.executable, "-m", "scripts.ci_mutation.report", str(result_path), *shard_args, *selected],
        work,
        deadline - time.monotonic(),
        work / "report.log",
    )
    if status is None:
        return {}, "over budget"
    if status != 0:
        return {}, f"report failed with exit {status}; see {work / 'report.log'}"
    return json.loads(result_path.read_text()), ""


def record_shard_measurement(
    report: dict, output: Path, stats: SharedStats | None, started: float, budget: float, shard: tuple[int, int]
) -> None:
    if stats is None or stats.part is not None or not (stats.folder / "plan.json").is_file():
        return
    plan = json.loads((stats.folder / "plan.json").read_text())
    owned = {name for name, assignment in plan["assignments"].items() if holds(assignment, shard[0])}
    completed = set()
    for path in output.glob("*/mutants/**/*.py.meta"):
        data = json.loads(path.read_text())
        completed.update(
            name for name, status in data["exit_code_by_key"].items() if status is not None and name in owned
        )
    graded = max(
        len(completed),
        sum(count for file in report["files"] for status, count in file["counts"].items() if status != "not checked")
        + sum(row["status"] != "not checked" for entry in report.get("units", []) for row in entry["rows"]),
    )
    exhausted = any(entry["reason"] == "over budget" for entry in report["not_mutated"])
    report["shard"] = {
        "index": shard[0],
        "total": shard[1],
        "budget_seconds": budget,
        "wall_seconds": time.monotonic() - started,
        "expected": len(owned),
        "graded": graded,
        "complete": graded == len(owned) and not report["not_mutated"],
        "budget_exhausted": exhausted,
        "estimated_worker_seconds": sum(unit_cost(plan, name, shard[0]) for name in owned),
    }
    print(json.dumps({"shard": report["shard"]}), flush=True)


def grade_shard_rows(
    report: dict, path: str, rows: list[dict], lines: set[int], cleared: dict, split: set[str]
) -> dict:
    if units := [row for row in rows if row["name"] in split]:
        report.setdefault("units", []).append({"path": path, "lines": sorted(lines), "rows": units})
    return evaluate(path, [row for row in rows if row["name"] not in split], lines, cleared)


def split_plan_names(stats: SharedStats | None) -> set[str]:
    if stats is None or not (stats.folder / "plan.json").is_file():
        return set()
    return split_names(json.loads((stats.folder / "plan.json").read_text()))


def run_gate(
    root: Path,
    changes: dict[str, set[int]],
    output: Path,
    budget: float,
    shard: tuple[int, int] = (0, 1),
    stats: SharedStats | None = None,
) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + budget
    cleared = load_clearances(root)
    split = split_plan_names(stats)
    report = {"files": [], "not_mutated": [], "failed": False}
    rows, reasons, selected, failing = {}, {}, {}, []
    for path, lines in changes.items():
        source = root / path
        if source.is_file() and not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(ast.parse(source.read_text()))
        ):
            rows[path] = []
        elif time.monotonic() >= deadline:
            reasons[path] = "over budget"
        elif tests := select_tests(root, Path(path)):
            selected[path] = (lines, tests)
        else:
            reasons[path] = "no matching or importing test modules"
    groups = [[path for path in selected if path != IDENTITY], [path for path in selected if path == IDENTITY]]
    for index, group in enumerate(group for group in groups if group):
        if time.monotonic() >= deadline:
            reasons.update(dict.fromkeys(group, "over budget"))
            continue
        work = Path(tempfile.mkdtemp(prefix=f"{index}-", dir=output))
        results, reason = mutate_files(
            root, work, {path: selected[path] for path in group}, deadline, shard, stats and stats.group(index)
        )
        if reason:
            reasons.update(dict.fromkeys(group, reason))
            failing += failing_tests(work / "run.log") if reason.startswith("mutmut failed") else []
        else:
            rows.update(results)
    report.update({"failing_tests": list(dict.fromkeys(failing))} if failing else {})
    for path, lines in changes.items():
        if path in reasons:
            report["not_mutated"].append({"path": path, "reason": reasons[path]})
            print(f"{path}: not mutated, {reasons[path]}", flush=True)
            report["failed"] = True
            continue
        result = grade_shard_rows(report, path, rows[path], lines, cleared, split)
        report["files"].append(result)
        if path in selected:
            print(json.dumps(result), flush=True)
        else:
            print(f"{path}: no mutable functions", flush=True)
        if result["failures"]:
            print(survivor_text(result))
            report["failed"] = True
    record_shard_measurement(report, output, stats, started, budget, shard)
    (output / "report.json").write_text(json.dumps(report) + "\n")
    return report
