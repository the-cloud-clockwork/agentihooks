import re
import shlex
from pathlib import Path

import pytest
import yaml

from scripts.gate_protected import EQUIVALENCE

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
PIN = "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS"
VALUE = "0.dev${{ github.run_number }}"
INSTALLER = re.compile(r"\bpip3?\s+install\b|\buv\s+sync\b")
SEPARATORS = {"&&", "||", ";", "|", "&"}


def _is_editable_install(run: str) -> bool:
    """Return whether a step script installs a checkout in editable mode, which runs the SCM version scan."""
    for line in run.replace("\\\n", " ").splitlines():
        if not INSTALLER.search(line):
            continue
        tokens = shlex.split(line, comments=True)
        for index, token in enumerate(tokens[:-1]):
            options = []
            for option in tokens[index + 2 :]:
                if option in SEPARATORS:
                    break
                options.append(option)
            if token in ("pip", "pip3") and tokens[index + 1] == "install":
                if any(option == "--editable" or option.startswith(("-e", "--editable=")) for option in options):
                    return True
            if token == "uv" and tokens[index + 1] == "sync" and "--no-install-project" not in options:
                return True
    return False


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
    """Return where each editable install step sits and whether it pins the SCM version to the run number."""
    found = []
    for path in _ci_files():
        for job, inherited, step in _steps(yaml.safe_load(path.read_text())):
            if _is_editable_install(step.get("run") or ""):
                env = {**inherited, **(step.get("env") or {})}
                found.append((f"{path.relative_to(ROOT)} {job}: {step.get('name', '')}", env.get(PIN) == VALUE))
    return found


def _frozen_by_protected_gate() -> set[str]:
    """Return the editable install steps the protected gate grades from dev in the ledger equivalence prefix."""
    file, job = EQUIVALENCE
    steps = yaml.safe_load((ROOT / ".github/workflows" / file).read_text())["jobs"][job]["steps"]
    graded = [index for index, step in enumerate(steps) if "$GRADER" in str(step.get("run"))]
    prefix = steps[: graded[-1] + 1] if graded else []
    return {
        f".github/workflows/{file} {job}: {step.get('name', '')}"
        for step in prefix
        if _is_editable_install(step.get("run") or "")
    }


@pytest.mark.parametrize(
    ("run", "editable"),
    [
        ("uv pip install --system -e '.[dev,all]'", True),
        ('uv pip install --python "$HOME/venv/bin/python" -e ".[all]"', True),
        ("pip install -e.[dev]", True),
        ("pip3 install --editable=./head", True),
        ("python -m pip install --editable ./head", True),
        ("uv pip install --system \\\n  -e '.[dev,all]'", True),
        ("uv sync --frozen", True),
        ("uv sync --no-install-project", False),
        ("uv pip install semgrep==1.177.0", False),
        ('[[ -e "$PLAYWRIGHT_BROWSERS_PATH" ]] && pip install ruff', False),
        ("uv build --wheel", False),
        ("# uv pip install -e .", False),
    ],
)
def test_editable_install_detection(run: str, editable: bool) -> None:
    assert _is_editable_install(run) is editable


def test_every_editable_install_pins_the_scm_pretend_version() -> None:
    installs = _editable_installs()
    assert installs
    frozen = _frozen_by_protected_gate()
    stray = sorted(where for where, pinned in installs if not pinned and where not in frozen)
    assert not stray, f"{len(stray)} of {len(installs)} editable installs run the SCM git scan: {stray}"
