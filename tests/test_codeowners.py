import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE_PATHS = [
    ".github/workflows/test.yml",
    ".github/rulesets/dev-no-delete.json",
    ".github/CODEOWNERS",
    "scripts/ci_mutation/runner.py",
    "scripts/gates/base.py",
    "mutation-cleared.txt",
    "delivery.yaml",
]


def _rules():
    rules = []
    for line in (ROOT / ".github" / "CODEOWNERS").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            pattern, *owners = line.split()
            rules.append((pattern, owners))
    return rules


def _owners(path):
    found = []
    for pattern, owners in _rules():
        anchored = pattern.lstrip("/")
        if (pattern.endswith("/") and path.startswith(anchored)) or path == anchored:
            found = owners
    return found


def _workflow_modules():
    text = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    modules = set(re.findall(r"python -m ((?:scripts|tests)(?:\.\w+)+)", text))
    paths = []
    for module in sorted(modules):
        base = ROOT.joinpath(*module.split("."))
        path = base / "__init__.py" if base.is_dir() else base.with_suffix(".py")
        assert path.is_file(), module
        paths.append(path.relative_to(ROOT).as_posix())
    return paths


def test_tests_workflow_runs_gate_modules():
    assert "scripts/ci_wiring.py" in _workflow_modules()


@pytest.mark.parametrize("path", GATE_PATHS + _workflow_modules())
def test_gate_path_has_a_code_owner(path):
    assert _owners(path), f"{path} has no code owner in .github/CODEOWNERS"


def test_unlisted_path_has_no_owner():
    assert _owners("hooks/hook_manager.py") == []
