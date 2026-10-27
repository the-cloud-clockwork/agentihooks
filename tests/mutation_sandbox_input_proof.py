import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from xml.etree import ElementTree

import pytest

from scripts.ci_cpu_quota import quota_cpus, worker_count
from scripts.ci_mutation.runner import prepare_workspace

REPORT_ARTIFACT = "proof-reports"
SPLIT_ALGORITHM = "least_duration"


def nested_report(result: subprocess.CompletedProcess, junit: Path) -> str:
    log = junit.with_suffix(".log")
    log.write_text(result.stdout + result.stderr)
    counts = "no junit report"
    if junit.is_file():
        root = ElementTree.parse(junit).getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        counts = " ".join(
            f"{key} {sum(int(suite.get(key, 0)) for suite in suites)}"
            for key in ("tests", "failures", "errors", "skipped")
        )
    return (
        f"nested pytest exited {result.returncode}; {counts}; "
        f"read {junit.name} and {log.name} in the {REPORT_ARTIFACT} artifact"
    )


@pytest.mark.parametrize(
    "stage, group",
    [
        pytest.param(stage, group, id=f"{stage}-{group}")
        for stage in ("source", "workspace", "mutants")
        for group in range(1, 9)
    ],
)
def test_full_selection_reads_the_same_repository_inputs(tmp_path, stage, group):
    root = Path(__file__).resolve().parents[1]
    tests = sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py"))
    baseline = root
    if stage != "source":
        work = tmp_path / "workspace"
        work.mkdir()
        prepare_workspace(root, work, ["hooks/config.py"], tests)
        baseline = work
        if stage == "mutants":
            baseline = work / "mutants"
            baseline.mkdir()
            config = tomllib.loads((work / "pyproject.toml").read_text())["tool"]["mutmut"]
            for name in ["hooks", "scripts", "tests", "pyproject.toml", *config["also_copy"]]:
                source = work / name
                if source.is_dir():
                    shutil.copytree(source, baseline / name, dirs_exist_ok=True)
                elif source.is_file():
                    (baseline / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, baseline / name)
    reports = Path(os.environ.get("PROOF_REPORT_DIR") or tmp_path)
    reports.mkdir(parents=True, exist_ok=True)
    junit = reports / f"{stage}-{group}.xml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            "-n",
            str(worker_count(os.cpu_count() or 1, quota_cpus())),
            "--splits",
            "8",
            "--splitting-algorithm",
            SPLIT_ALGORITHM,
            "--group",
            str(group),
            "--dist",
            "loadgroup",
            "--tb=short",
            "--junitxml",
            str(junit),
        ],
        cwd=baseline,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )
    returncode = result.returncode
    assert returncode == 0, nested_report(result, junit)


def test_statistics_keep_declared_inputs_and_the_trusted_published_map(tmp_path, monkeypatch):
    import ast
    import time

    from scripts.ci_mutation.runner import mutate_files
    from scripts.ci_mutation.stats import SharedStats

    root = Path(__file__).resolve().parents[1]
    path = "scripts/install.py"
    main = next(
        node
        for node in ast.parse((root / path).read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    line = next(node.lineno for node in ast.walk(main) if isinstance(node, ast.Call))
    selected = [
        "tests/test_ci_input_key.py::test_every_declared_input_exists_and_covers_its_own_workflow",
        "tests/swarm/test_redis_commands.py::test_the_published_map_drops_calls_on_receivers_that_hold_no_redis_client",
        "tests/test_compose_stack.py::test_swarm_init_ships_the_repository_stack_byte_for_byte",
        "tests/test_ci_semgrep.py::test_scan_reads_the_domain_rules_folder_that_holds_rule_files",
        "tests/test_tailnet_compose.py",
    ]
    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("MUTATION_STATS_ROOT", str(root))
    stats = SharedStats(tmp_path / "stats", "sandbox-control", (0, 1))
    rows, reason = mutate_files(root, work, {path: ({line}, selected)}, time.monotonic() + 600, (0, 1), stats)
    assert not reason, reason + "\n" + (work / "run.log").read_text()
    assert rows == {path: []}
