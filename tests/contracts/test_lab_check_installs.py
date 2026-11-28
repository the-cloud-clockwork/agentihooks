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


def _post_steps(workflow: Path) -> tuple[set[str], list[str]]:
    """Return the distributions the post job installs and the modules it runs with python -m."""
    installed: set[str] = set()
    entries: list[str] = []
    for step in yaml.safe_load(workflow.read_text())["jobs"]["post"]["steps"]:
        words = shlex.split(step.get("run", ""))
        if "install" in words:
            installed.update(_normal(word.partition("==")[0]) for word in words if "==" in word)
        elif words[:2] == ["python", "-m"]:
            entries.append(words[2])
    if not entries:
        raise LookupError(f"{workflow} runs no python -m entry")
    return installed, entries


def _source(root: Path, name: str) -> tuple[Path | None, bool]:
    base = root.joinpath(*name.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py", True
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py"), False
    return None, False


def _run_on_import(tree: ast.Module) -> Iterator[ast.Import | ast.ImportFrom]:
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
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
    """Name each third party module a workflow entry imports whose distribution the workflow does not install."""
    installed, entries = _post_steps(workflow)
    owners = importlib.metadata.packages_distributions()
    return {
        module: importer
        for entry in entries
        for module, importer in third_party_imports(root, entry, local).items()
        if not installed.intersection(_normal(owner) for owner in owners.get(module, ()))
    }


def _planted(root: Path, installs: str) -> Path:
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
        "\n"
        "def later() -> None:\n"
        "    import jsonschema\n"
    )
    workflow = root / "lab-check.yml"
    workflow.write_text(
        yaml.safe_dump(
            {
                "jobs": {
                    "post": {
                        "steps": [
                            {"run": f"python -m pip install --constraint lock {installs}"},
                            {"run": "python -m planted.entry"},
                        ]
                    }
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
    workflow = _planted(tmp_path, "requests==2.34.2")
    assert missing_installs(workflow, tmp_path, frozenset({"planted"})) == {"yaml": "planted.deep"}


@pytest.mark.feature("lab-check-installs-entry-imports")
def test_the_walk_passes_once_the_install_step_names_the_distribution(tmp_path: Path) -> None:
    workflow = _planted(tmp_path, "requests==2.34.2 PyYAML==6.0.3")
    assert missing_installs(workflow, tmp_path, frozenset({"planted"})) == {}


@pytest.mark.feature("lab-check-installs-entry-imports")
def test_a_workflow_without_an_entry_is_refused(tmp_path: Path) -> None:
    workflow = tmp_path / "lab-check.yml"
    workflow.write_text(
        yaml.safe_dump({"jobs": {"post": {"steps": [{"run": "python -m pip install requests==2.34.2"}]}}})
    )
    with pytest.raises(LookupError):
        missing_installs(workflow, tmp_path, frozenset({"planted"}))
