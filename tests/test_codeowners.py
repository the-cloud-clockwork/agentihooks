import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE_ROOTS = [".github", "scripts/gates", "scripts/ci_mutation", "mutation-clearances"]
GATE_FILES = [
    "mutation-cleared.txt",
    "tests/test_codeowners.py",
    "tests/test_rulesets.py",
    "tests/test_ci_gate_workflow.py",
    "tests/test_count_floor.py",
    "tests/test_shard_check.py",
]
PLANNED_GATE_FILES = [
    "delivery.yaml",
    ".semgrep/agentihooks-rules.yml",
    "scripts/gate_size.py",
    "scripts/check_wiring.py",
    "tests/registry/MODULES.json",
    "tests/LOCKED_MANIFEST.json",
    "tests/COVERAGE_BASELINE.json",
    "tests/SIZE_ALLOWLIST.json",
]
OWNER = re.compile(r"@[\w-]+(/[\w.-]+)?")


def _rules():
    rules = []
    for line in (ROOT / ".github" / "CODEOWNERS").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            pattern, *owners = line.split()
            rules.append((pattern, owners))
    return rules


def _matches(pattern, path):
    body = re.escape(pattern.strip("/")).replace(r"\*", "[^/]*")
    return re.fullmatch(body + ("/.*" if pattern.endswith("/") else ""), path) is not None


def _owners(path):
    found = []
    for pattern, owners in _rules():
        if _matches(pattern, path):
            found = owners
    return found


def _workflow_modules():
    modules = set()
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        modules |= set(re.findall(r"python -m ((?:scripts|tests)(?:\.\w+)+)", workflow.read_text()))
    paths = []
    for module in sorted(modules):
        base = ROOT.joinpath(*module.split("."))
        path = base / "__init__.py" if base.is_dir() else base.with_suffix(".py")
        assert path.is_file(), module
        paths.append(path.relative_to(ROOT).as_posix())
    return paths


def test_workflows_run_gate_modules():
    assert {"scripts/ci_wiring.py", "tests/leaks.py"} <= set(_workflow_modules())


def test_every_rule_is_anchored_and_names_a_user_or_team():
    for pattern, owners in _rules():
        assert pattern.startswith("/"), pattern
        assert owners and all(OWNER.fullmatch(owner) for owner in owners), (pattern, owners)


@pytest.mark.parametrize("root", GATE_ROOTS)
def test_every_file_under_a_gate_root_has_a_code_owner(root):
    files = [p.relative_to(ROOT).as_posix() for p in (ROOT / root).rglob("*") if p.is_file()]
    assert files
    assert [path for path in files if not _owners(path)] == []


@pytest.mark.parametrize("path", GATE_FILES + _workflow_modules())
def test_gate_path_has_a_code_owner(path):
    assert (ROOT / path).is_file(), path
    assert _owners(path), f"{path} has no code owner in .github/CODEOWNERS"


@pytest.mark.parametrize("path", PLANNED_GATE_FILES)
def test_planned_gate_path_has_a_code_owner(path):
    assert _owners(path), f"{path} has no code owner in .github/CODEOWNERS"


@pytest.mark.parametrize("path", ["hooks/hook_manager.py", "scripts/gatesx.py", "tests/registry.py"])
def test_unlisted_path_has_no_owner(path):
    assert _owners(path) == []
