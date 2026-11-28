"""The Lab check workflow installs every third party distribution its check poster imports at module level."""

import ast
import importlib.metadata
import re
import shlex
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/lab-check.yml"
LOCAL = frozenset({"scripts", "hooks", "tests"})


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _jobs(workflow: Path) -> list[tuple[set[str], list[str]]]:
    """Return, per job, the distributions its run lines install and the modules they run with python -m."""
    jobs: list[tuple[set[str], list[str]]] = []
    for job in yaml.safe_load(workflow.read_text())["jobs"].values():
        installed: set[str] = set()
        entries: list[str] = []
        for step in job.get("steps", []):
            for line in step.get("run", "").splitlines():
                words = shlex.split(line)
                if "install" in words:
                    installed.update(_normal(word.partition("==")[0]) for word in words if "==" in word)
                elif words[:2] == ["python", "-m"] and len(words) > 2:
                    entries.append(words[2])
        jobs.append((installed, entries))
    if not any(entries for _, entries in jobs):
        raise LookupError(f"{workflow} runs no python -m entry")
    return jobs


def _source(root: Path, name: str) -> tuple[Path | None, bool]:
    base = root.joinpath(*name.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py", True
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py"), False
    return None, False


def _type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _run_on_import(tree: ast.Module) -> Iterator[ast.Import | ast.ImportFrom]:
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.If) and _type_checking(node.test):
            pending.extend(node.orelse)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        pending.extend(ast.iter_child_nodes(node))


def _targets(node: ast.Import | ast.ImportFrom, module: str, package: bool) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        anchor = module.split(".")[: None if package else -1]
        anchor = anchor[: len(anchor) - node.level + 1]
        base = ".".join([*anchor, *filter(None, [node.module])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")]


def third_party_imports(root: Path, entry: str, local: frozenset[str]) -> dict[str, str]:
    """Map each third party top level module that importing entry runs to a local module importing it."""
    if _source(root, entry)[0] is None:
        raise LookupError(f"{entry} has no source under {root}")
    seen: set[str] = set()
    found: dict[str, str] = {}
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        parts = name.split(".")
        pending.extend(".".join(parts[:end]) for end in range(1, len(parts)))
        path, package = _source(root, name)
        if path is None:
            continue
        for node in _run_on_import(ast.parse(path.read_text())):
            for target in _targets(node, name, package):
                top = target.partition(".")[0]
                if top in local:
                    pending.append(target)
                elif top and top not in sys.stdlib_module_names:
                    found.setdefault(top, name)
    return found


def missing_installs(workflow: Path, root: Path, local: frozenset[str]) -> dict[str, str]:
    """Name each third party module a job's entry imports whose distribution that job does not install."""
    owners = importlib.metadata.packages_distributions()
    missing: dict[str, str] = {}
    for installed, entries in _jobs(workflow):
        for entry in entries:
            for module, importer in third_party_imports(root, entry, local).items():
                if module not in owners:
                    raise LookupError(f"no installed distribution provides {module}, imported by {importer}")
                if not installed.intersection(_normal(owner) for owner in owners[module]):
                    missing[module] = importer
    return missing


def _planted(root: Path, installs: str, other_installs: str) -> Path:
    package = root / "planted"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "entry.py").write_text("from planted import deep\n")
    (package / "deep.py").write_text(
        "import json\n"
        "from typing import TYPE_CHECKING\n"
        "\n"
        "import yaml\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    import orjson\n"
        "\n"
        "if not TYPE_CHECKING:\n"
        "    import jwt\n"
        "\n"
        "\n"
        "def later() -> None:\n"
        "    import requests\n"
    )
    (package / "other.py").write_text("import jsonschema\n")
    workflow = root / "lab-check.yml"
    install = "python -m pip install --upgrade pip\npython -m pip install --constraint lock"
    workflow.write_text(
        yaml.safe_dump(
            {
                "jobs": {
                    "post": {
                        "steps": [
                            {"run": f"{install} {installs}"},
                            {"run": "set -e\npython -m planted.entry"},
                        ]
                    },
                    "other": {"steps": [{"run": f"{install} {other_installs}\npython -m planted.other"}]},
                }
            }
        )
    )
    return workflow


@pytest.mark.feature("lab-check-installs-entry-imports")
def test_the_lab_check_workflow_installs_every_distribution_its_poster_imports() -> None:
    assert missing_installs(WORKFLOW, ROOT, LOCAL) == {}


@pytest.mark.feature("lab-check-installs-entry-imports")
def test_the_walk_names_a_module_level_import_reached_through_a_local_module(tmp_path: Path) -> None:
    workflow = _planted(tmp_path, "requests==2.34.2", "")
    assert missing_installs(workflow, tmp_path, frozenset({"planted"})) == {
        "yaml": "planted.deep",
        "jwt": "planted.deep",
        "jsonschema": "planted.other",
    }


@pytest.mark.feature("lab-check-installs-entry-imports")
def test_the_walk_passes_once_the_install_step_names_the_distribution(tmp_path: Path) -> None:
    workflow = _planted(tmp_path, "PyYAML==6.0.3 PyJWT==2.15.1", "jsonschema==4.26.0")
    assert missing_installs(workflow, tmp_path, frozenset({"planted"})) == {}


@pytest.mark.feature("lab-check-installs-entry-imports")
def test_a_workflow_without_an_entry_is_refused(tmp_path: Path) -> None:
    workflow = tmp_path / "lab-check.yml"
    workflow.write_text(
        yaml.safe_dump({"jobs": {"post": {"steps": [{"run": "python -m pip install requests==2.34.2"}]}}})
    )
    with pytest.raises(LookupError):
        missing_installs(workflow, tmp_path, frozenset({"planted"}))


@pytest.mark.feature("lab-check-installs-entry-imports")
def test_a_module_no_installed_distribution_provides_is_refused(tmp_path: Path) -> None:
    package = tmp_path / "planted"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "entry.py").write_text("import planted_absent_distribution\n")
    workflow = tmp_path / "lab-check.yml"
    workflow.write_text(yaml.safe_dump({"jobs": {"post": {"steps": [{"run": "python -m planted.entry"}]}}}))
    with pytest.raises(LookupError, match="planted_absent_distribution"):
        missing_installs(workflow, tmp_path, frozenset({"planted"}))
