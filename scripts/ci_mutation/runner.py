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


def echo_cache_report(log: Path) -> None:
    for line in log.read_text(errors="replace").splitlines() if log.is_file() else []:
        if line.startswith("Mutation stats cache:"):
            print(line)


def path_parts(node: ast.AST, bindings: dict[str, ast.AST]) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return [""]
        if node.id not in bindings:
            return []
        return path_parts(bindings[node.id], {key: value for key, value in bindings.items() if key != node.id})
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [part for value in node.elts for part in path_parts(value, bindings)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [
            str(Path(left) / right)
            for left in path_parts(node.left, bindings)
            for right in path_parts(node.right, bindings)
        ]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"Path", "str"} and node.args:
        return path_parts(node.args[0], bindings)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "resolve":
        return path_parts(node.func.value, bindings)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return path_parts(node.value, bindings)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
        return path_parts(node.value.value, bindings)
    return []


def read_paths(call: ast.Call, bindings: dict[str, ast.AST]) -> list[str]:
    if isinstance(call.func, ast.Name) and call.func.id == "open" and call.args:
        return path_parts(call.args[0], bindings)
    if not isinstance(call.func, ast.Attribute):
        return []
    operation = call.func.attr
    if operation in {"run", "Popen", "call", "check_call", "check_output"}:
        return path_parts(call.args[0], bindings) if call.args else []
    if operation not in {"read_text", "read_bytes", "open", "glob", "rglob", "iterdir", "is_file", "exists"}:
        return []
    paths = path_parts(call.func.value, bindings)
    if operation in {"glob", "rglob", "iterdir"}:
        patterns = ["*"] if operation == "iterdir" else path_parts(call.args[0], bindings)
        prefix = "**/" if operation == "rglob" else ""
        return [str(Path(path) / (prefix + pattern)) for path in paths for pattern in patterns]
    return paths


def fixture_literals(node: ast.AST) -> list[str]:
    nodes = list(ast.walk(node))
    bindings = {
        target.id: assignment.value
        for assignment in nodes
        if isinstance(assignment, ast.Assign)
        for target in assignment.targets
        if isinstance(target, ast.Name)
        and target.id not in {part.id for part in ast.walk(assignment.value) if isinstance(part, ast.Name)}
    }
    functions = {function.name: function for function in nodes if isinstance(function, ast.FunctionDef)}
    calls = [part for part in nodes if isinstance(part, ast.Call)]
    parameters = {}
    for call in calls:
        if isinstance(call.func, ast.Name) and call.func.id in functions:
            for parameter, value in zip(functions[call.func.id].args.args, call.args):
                if parameter.arg not in {part.id for part in ast.walk(value) if isinstance(part, ast.Name)}:
                    parameters.setdefault(parameter.arg, []).append(value)
        if isinstance(call.func, ast.Attribute) and call.func.attr == "parametrize" and len(call.args) >= 2:
            names = path_parts(call.args[0], bindings)
            if len(names) == 1:
                bindings[names[0]] = call.args[1]
    bindings.update({name: ast.List(elts=values) for name, values in parameters.items()})
    return [path for call in calls for path in read_paths(call, bindings)]


def fixture_candidates(root: Path, work: Path, literal: str) -> list[Path]:
    relative = Path(literal)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return []
    if any(len(part) > 255 for part in relative.parts) or relative == Path("pyproject.toml"):
        return []
    if (work / relative).exists():
        return []
    target = root / relative
    if any(part.is_symlink() for part in (target, *target.parents) if part != root):
        return []
    return list(root.glob(literal))


def fixture_files(root: Path, work: Path, tests: list[str]) -> list[str]:
    sources = {root / test.split("::")[0] for test in tests}
    for source in tuple(sources):
        sources.update(parent / "conftest.py" for parent in source.parents if parent.is_relative_to(root))
    files = set()
    for source in sorted(sources):
        if not source.is_file():
            continue
        for literal in fixture_literals(ast.parse(source.read_text())):
            for candidate in fixture_candidates(root, work, literal):
                name = candidate.relative_to(root)
                if any(part in {".git", "__pycache__", work.name} for part in name.parts):
                    continue
                if candidate.name.startswith(".env") and candidate.suffix not in {
                    ".example",
                    ".sample",
                    ".template",
                    ".dist",
                }:
                    continue
                if any(part.is_symlink() for part in (candidate, *candidate.parents) if part != root):
                    continue
                if candidate.is_file() and candidate.suffix != ".pyc" and not (work / name).exists():
                    files.add(name.as_posix())
    return sorted(files)


# mutmut copies the workspace into mutants/ before any test runs, so the workspace itself is only read.
def link_or_copy(source: str, target: str) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def prepare_workspace(root: Path, work: Path, paths: list[str], tests: list[str]) -> None:
    for name in (
        "hooks",
        "scripts",
        "tests",
        "profiles",
        "docs",
        ".github",
        ".semgrep",
        "evidence",
        "observability",
        "docker/swarm-node",
        ".agentihooks/conditions",
        "deploy/helm/agentihooks-swarm/ci",
    ):
        source = root / name
        if source.is_dir():
            shutil.copytree(
                source,
                work / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", work.name),
                copy_function=link_or_copy,
            )
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
    extra_files = fixture_files(root, work, tests)
    for name in extra_files:
        (work / name).parent.mkdir(parents=True, exist_ok=True)
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
            ".semgrep/",
            "evidence/",
            "observability/",
            "docker/swarm-node/",
            ".agentihooks/conditions/",
            "deploy/helm/agentihooks-swarm/ci/",
            *root_files,
            *(f"{name}/" for name in sorted({Path(path).parts[0] for path in extra_files})),
        ],
        "pytest_add_cli_args_test_selection": tests,
        "pytest_add_cli_args": pytest_args,
    }
    (work / "pyproject.toml").write_text(tomlkit.dumps(project))


def seed_measured_shard(stats: SharedStats, key: str, work: Path) -> str:
    plan_path = stats.folder.parent / "plan.json"
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text())
        if plan["head"] != stats.head or key not in plan["keys"]:
            return "measured mutation plan stale for this commit or selection"
        shutil.copy(plan_path, work / "shard-plan.json")
    if (killers := stats.folder.parent / "killers").is_dir():
        shutil.copytree(killers, work / "killers")
    return ""


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
            if reason := seed_measured_shard(stats, key, work):
                return {}, reason
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
        echo_cache_report(log)
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
    selection_path = root / ".mutation-selection.json"
    selection = json.loads(selection_path.read_text()) if selection_path.is_file() else None
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
        elif tests := (selection[path] if selection is not None else select_tests(root, Path(path))):
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
