import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
PINS = ("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS", "SETUPTOOLS_SCM_PRETEND_VERSION")
EDITABLE = re.compile(r"\bpip install\b[^\n]*\s(?:-e|--editable)[\s=]")
FROZEN_BY_PROTECTED_GATE = frozenset({".github/workflows/equivalence.yml ledger-equivalence: Install dependencies"})


def _ci_files() -> list[Path]:
    """Return every workflow and composite action file under .github."""
    github = ROOT / ".github"
    return sorted([*github.glob("workflows/*.y*ml"), *github.glob("actions/**/action.y*ml")])


def _steps(document: dict) -> list[tuple[str, dict, dict]]:
    """Return each step with its job name and the env it inherits from its workflow and job."""
    if "jobs" not in document:
        return [("action", {}, step) for step in (document.get("runs") or {}).get("steps") or []]
    workflow_env = document.get("env") or {}
    return [
        (name, {**workflow_env, **(job.get("env") or {})}, step)
        for name, job in document["jobs"].items()
        for step in job.get("steps") or []
    ]


def _editable_installs() -> list[tuple[str, bool]]:
    """Return where each step with an editable pip install sits and whether it pins the SCM version."""
    found = []
    for path in _ci_files():
        for job, inherited, step in _steps(yaml.safe_load(path.read_text())):
            if EDITABLE.search((step.get("run") or "").replace("\\\n", " ")):
                env = {**inherited, **(step.get("env") or {})}
                found.append(
                    (f"{path.relative_to(ROOT)} {job}: {step.get('name', '')}", any(env.get(pin) for pin in PINS))
                )
    return found


def test_every_editable_install_pins_the_scm_pretend_version() -> None:
    installs = _editable_installs()
    assert installs
    unpinned = {where for where, pinned in installs if not pinned}
    assert FROZEN_BY_PROTECTED_GATE <= unpinned, f"now pinned: {sorted(FROZEN_BY_PROTECTED_GATE - unpinned)}"
    stray = sorted(unpinned - FROZEN_BY_PROTECTED_GATE)
    assert not stray, f"{len(stray)} of {len(installs)} editable installs run the SCM git scan: {stray}"
