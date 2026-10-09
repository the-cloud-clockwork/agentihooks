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
from scripts.ci_mutation.report import evaluate, survivor_text
from scripts.ci_mutation.scope import select_tests

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


def prepare_workspace(root: Path, work: Path, paths: list[str], tests: list[str]) -> None:
    for name in (
        "hooks",
        "scripts",
        "tests",
        "profiles",
        "docs",
        ".github",
        "evidence",
        "docker/swarm-node",
        ".agentihooks/conditions",
    ):
        source = root / name
        if source.is_dir():
            shutil.copytree(source, work / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", work.name))
    for name in (".test_durations", "Swarm-v2.md"):
        if (root / name).is_file():
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
            "Swarm-v2.md",
            "docker/swarm-node/",
            ".agentihooks/conditions/",
            ".test_durations",
        ],
        "pytest_add_cli_args_test_selection": tests,
        "pytest_add_cli_args": pytest_args,
    }
    (work / "pyproject.toml").write_text(tomlkit.dumps(project))


def mutate_files(
    root: Path, work: Path, selected: dict[str, tuple[set[int], list[str]]], deadline: float
) -> tuple[dict[str, list[dict]], str]:
    tests = sorted({test for _, chosen in selected.values() for test in chosen})
    prepare_workspace(root, work, list(selected), tests)
    selection = work / "changed-lines.json"
    selection.write_text(
        json.dumps({path: {"lines": sorted(lines), "tests": chosen} for path, (lines, chosen) in selected.items()})
    )
    log = work / "run.log"
    status = run_process(
        [sys.executable, "-m", "scripts.ci_mutation.selection", str(selection)], work, deadline - time.monotonic(), log
    )
    if status is None:
        return {}, "over budget"
    if status != 0:
        return {}, f"mutmut failed with exit {status}; see {log}"
    result_path = work / "results.json"
    status = run_process(
        [sys.executable, "-m", "scripts.ci_mutation.report", str(result_path), *selected],
        work,
        deadline - time.monotonic(),
        work / "report.log",
    )
    if status is None:
        return {}, "over budget"
    if status != 0:
        return {}, f"report failed with exit {status}; see {work / 'report.log'}"
    return json.loads(result_path.read_text()), ""


def run_gate(root: Path, changes: dict[str, set[int]], output: Path, budget: float) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + budget
    cleared = load_clearances(root)
    report = {"files": [], "not_mutated": [], "failed": False}
    rows, reasons, selected = {}, {}, {}
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
        results, reason = mutate_files(root, work, {path: selected[path] for path in group}, deadline)
        if reason:
            reasons.update(dict.fromkeys(group, reason))
        else:
            rows.update(results)
    for path, lines in changes.items():
        if path in reasons:
            report["not_mutated"].append({"path": path, "reason": reasons[path]})
            print(f"{path}: not mutated, {reasons[path]}", flush=True)
            report["failed"] = True
            continue
        result = evaluate(path, rows[path], lines, cleared)
        report["files"].append(result)
        if path in selected:
            print(json.dumps(result), flush=True)
        else:
            print(f"{path}: no mutable functions", flush=True)
        if result["failures"]:
            print(survivor_text(result))
            report["failed"] = True
    (output / "report.json").write_text(json.dumps(report) + "\n")
    return report
