import ast
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
CUTOVER_MODULES = (
    "scripts/swarm/cli.py",
    "scripts/swarm/lease.py",
    "scripts/swarm/ledger_client.py",
    "scripts/swarm/ledger_cutover.py",
    "scripts/swarm/ledger_read.py",
    "scripts/swarm/store.py",
    "scripts/swarm_ledger/adapters/swarm.py",
)


def workflow(name: str) -> dict[Any, Any]:
    """Load one workflow file."""
    return yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())


def selected(paths: list[str], tmp_path: Path) -> str:
    """Run the writer lock feature suite selection on a pull request changing these paths and return its output."""
    steps = workflow("writer-lock")["jobs"]["inputs"]["steps"]
    step = next(step for step in steps if step.get("id") == "features")
    (tmp_path / "feature-paths").write_bytes(b"".join(path.encode() + b"\0" for path in paths))
    output = tmp_path / "output"
    output.unlink(missing_ok=True)
    git = tmp_path / "git"
    git.write_text(
        '#!/usr/bin/env bash\nif [[ "$*" == *merge-base* ]]; then echo base; else cat "$RUNNER_TEMP/feature-paths"; fi\n'
    )
    git.chmod(0o755)
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "PR_BASE": "base",
            "PR_HEAD": "head",
            "FEATURE_INPUTS": step["env"]["FEATURE_INPUTS"],
        },
        check=True,
    )
    return output.read_text()


def module_file(dotted: str) -> str | None:
    """Return the repository file of a dotted module or package name, or None when it names neither."""
    base = ROOT.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate.relative_to(ROOT).as_posix()
    return None


def imported_files() -> set[str]:
    """Return the file of each module a feature suite file imports by absolute name, without following its imports."""
    found: set[str] = set()
    for path in (ROOT / "tests/features").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                files = [module_file(alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                files = [module_file(f"{node.module}.{alias.name}") or module_file(node.module) for alias in node.names]
            else:
                continue
            found.update(file for file in files if file)
    return found


@pytest.mark.feature("feature-suite-pull-request-tier")
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("scripts/swarm_ledger/repository/postgres.py", "true"),
        ("scripts/swarm_ledger/storage.py", "true"),
        ("scripts/swarm_ledger/ledger_server.py", "true"),
        ("scripts/swarm_ledger/adapters/swarm.py", "true"),
        ("scripts/swarm/store.py", "true"),
        ("scripts/swarm/lease.py", "true"),
        ("scripts/swarm/ledger_client.py", "true"),
        ("scripts/swarm/ledger_read.py", "true"),
        ("scripts/swarm/tick.py", "true"),
        ("scripts/swarm/tick_watch.py", "true"),
        ("scripts/swarm/scheduled_tick.py", "true"),
        ("scripts/tick/engine.py", "true"),
        ("scripts/tick/steps/agents.py", "true"),
        ("scripts/scheduler/core.py", "true"),
        ("scripts/swarm/controller.py", "true"),
        ("scripts/install.py", "true"),
        ("scripts/handoff/envelope.py", "true"),
        ("tests/features/test_tick_pass.py", "true"),
        (".github/workflows/feature-suite.yml", "true"),
        (".github/workflows/writer-lock.yml", "true"),
        ("scripts/swarm_ledger/palette.css", "false"),
        ("scripts/swarm_ledger/home.html", "false"),
        ("scripts/hub/admission.py", "false"),
        ("tests/swarm_ledger/test_postgres_repository.py", "false"),
        ("docs/reference/delivery-tiers.md", "false"),
        ("README.md", "false"),
    ],
)
def test_feature_suite_selection(path: str, expected: str, tmp_path: Path) -> None:
    assert selected([path], tmp_path) == f"due={expected}\n"


@pytest.mark.feature("feature-suite-pull-request-tier")
def test_each_ledger_and_swarm_module_the_ledger_write_cutover_changed_selects_the_feature_suite(
    tmp_path: Path,
) -> None:
    assert [module for module in CUTOVER_MODULES if selected([module], tmp_path) != "due=true\n"] == []


@pytest.mark.feature("feature-suite-pull-request-tier")
def test_every_module_the_feature_suite_imports_directly_selects_it(tmp_path: Path) -> None:
    files = imported_files()
    assert {
        "scripts/swarm/store.py",
        "scripts/swarm_ledger/repository/postgres.py",
        "tests/swarm/test_tick.py",
    } <= files
    assert [file for file in sorted(files) if selected([file], tmp_path) != "due=true\n"] == []


@pytest.mark.feature("feature-suite-pull-request-tier")
def test_the_feature_suite_feeds_the_required_gate_on_selected_pull_requests() -> None:
    tests = workflow("test")["jobs"]
    assert "writer-lock" in tests["gate-required"]["needs"]
    assert "github.event_name == 'pull_request'" in tests["writer-lock"]["if"]
    lock = workflow("writer-lock")["jobs"]
    [step] = [step for step in lock["inputs"]["steps"] if step.get("id") == "features"]
    assert step["if"] == "github.event_name == 'pull_request'"
    assert lock["inputs"]["outputs"]["features"] == "${{ steps.features.outputs.due }}"
    caller = lock["features"]
    assert caller["needs"] == ["inputs"]
    assert caller["if"] == "github.event_name == 'pull_request' && needs.inputs.outputs.features == 'true'"
    assert caller["uses"] == "./.github/workflows/feature-suite.yml"
