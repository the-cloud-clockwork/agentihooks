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

from scripts.ci_mutation.report import evaluate
from scripts.ci_mutation.scope import select_tests


def run_process(command: list[str], cwd: Path, timeout: float, log: Path) -> int | None:
    with log.open("w") as stream:
        process = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return None


def prepare_workspace(root: Path, work: Path, path: str, tests: list[str]) -> None:
    for name in ("hooks", "scripts", "tests", "profiles", "docs", ".github"):
        source = root / name
        if source.is_dir():
            shutil.copytree(source, work / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    project = tomlkit.parse((root / "pyproject.toml").read_text())
    project["tool"]["mutmut"] = {
        "source_paths": ["hooks/", "scripts/"],
        "only_mutate": [path],
        "also_copy": ["profiles/", "docs/", ".github/"],
        "pytest_add_cli_args_test_selection": tests,
        "pytest_add_cli_args": ["-q", "-x", "-o", "addopts=", "-p", "pytest_asyncio.plugin"],
    }
    (work / "pyproject.toml").write_text(tomlkit.dumps(project))


def mutate_file(root: Path, path: str, work: Path, tests: list[str], deadline: float) -> tuple[list[dict], str]:
    prepare_workspace(root, work, path, tests)
    log = work / "run.log"
    status = run_process(
        [sys.executable, "-m", "mutmut", "run", "--max-children", "1"], work, deadline - time.monotonic(), log
    )
    if status is None:
        return [], "over budget"
    if status != 0:
        return [], f"mutmut failed with exit {status}; see {log}"
    result_path = work / "results.json"
    status = run_process(
        [sys.executable, "-m", "scripts.ci_mutation.report", path, str(result_path)],
        work,
        deadline - time.monotonic(),
        work / "report.log",
    )
    if status is None:
        return [], "over budget"
    if status != 0:
        return [], f"report failed with exit {status}; see {work / 'report.log'}"
    return json.loads(result_path.read_text()), ""


def run_gate(root: Path, changes: dict[str, set[int]], output: Path, budget: float) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + budget
    clearances = root / "mutation-cleared.txt"
    cleared = json.loads(clearances.read_text()) if clearances.exists() else {}
    report = {"files": [], "not_mutated": [], "failed": False}
    for index, (path, lines) in enumerate(changes.items()):
        source = root / path
        if source.is_file() and not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(ast.parse(source.read_text()))
        ):
            report["files"].append(evaluate(path, [], lines, cleared))
            print(f"{path}: no mutable functions", flush=True)
            continue
        if time.monotonic() >= deadline:
            reason = "over budget"
        else:
            tests = select_tests(root, Path(path))
            if not tests:
                reason = "no matching or importing test modules"
            else:
                work = Path(tempfile.mkdtemp(prefix=f"{index}-", dir=output))
                rows, reason = mutate_file(root, path, work, tests, deadline)
        if reason:
            report["not_mutated"].append({"path": path, "reason": reason})
            print(f"{path}: not mutated, {reason}", flush=True)
            report["failed"] = True
            continue
        result = evaluate(path, rows, lines, cleared)
        report["files"].append(result)
        print(json.dumps(result), flush=True)
        if result["failures"]:
            report["failed"] = True
    (output / "report.json").write_text(json.dumps(report) + "\n")
    return report
