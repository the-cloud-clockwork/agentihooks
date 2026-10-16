import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CLIENT = [*sorted((ROOT / "scripts/codex_session").glob("*.py")), ROOT / "scripts/codex_app_server_probe.py"]
FIRST_PARTY = {"scripts", "hooks", "profiles"}


def _normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _imported_modules(path):
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def _third_party_modules():
    modules = set().union(*(_imported_modules(path) for path in CLIENT))
    return sorted(modules - set(sys.stdlib_module_names) - FIRST_PARTY)


def _client_distributions():
    owners = packages_distributions()
    missing = [module for module in _third_party_modules() if module not in owners]
    assert missing == [], f"no installed distribution provides {missing}"
    return {_normalize(dist) for module in _third_party_modules() for dist in owners[module]}


def _requirement_name(requirement):
    return _normalize(re.split(r"[<>=!~\[;\s]", requirement, maxsplit=1)[0])


def test_runtime_dependencies_cover_every_third_party_import_of_the_app_server_client():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime = {_requirement_name(requirement) for requirement in project["dependencies"]}
    assert sorted(_client_distributions() - runtime) == []


def test_the_worker_lock_pins_every_third_party_import_of_the_app_server_client():
    lock = (ROOT / "docker/swarm-node/requirements.lock").read_text(encoding="utf-8")
    pinned = {
        _normalize(line.split("==")[0]) for line in lock.splitlines() if "==" in line and not line.startswith(" ")
    }
    assert sorted(_client_distributions() - pinned) == []
