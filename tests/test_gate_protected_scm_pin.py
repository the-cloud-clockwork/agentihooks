from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import gate_protected
from tests.test_gate_protected import _EQUIVALENCE_REFUSED, _equivalence, _git, _grade_equivalence

pytestmark = pytest.mark.unit
SCM_PIN = {"SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS": "0.dev${{ github.run_number }}"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "dev")
    return tmp_path


def _steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return workflow["jobs"]["ledger-equivalence"]["steps"]


def _install(workflow: dict[Any, Any]) -> dict[str, Any]:
    [install] = [step for step in _steps(workflow) if step.get("name") == "Install dependencies"]
    return install


def _unpinned(workflow: dict[Any, Any]) -> dict[Any, Any]:
    install = _install(workflow)
    install["env"] = {key: value for key, value in install["env"].items() if key not in SCM_PIN}
    return workflow


def _pinned(workflow: dict[Any, Any]) -> dict[Any, Any]:
    _install(_unpinned(workflow))["env"].update(SCM_PIN)
    return workflow


@pytest.mark.feature("protected-grader-accepts-equivalence-scm-pin")
def test_the_required_grader_accepts_the_scm_version_pin_on_the_equivalence_install(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _grade_equivalence(repo, _unpinned(_equivalence()), _pinned(_equivalence())) == 0
    assert "0 protected gate problems" in capsys.readouterr().out


@pytest.mark.feature("protected-grader-accepts-equivalence-scm-pin")
def test_the_required_grader_keeps_accepting_the_pinned_install_once_dev_carries_it(repo: Path) -> None:
    head = _pinned(_equivalence())
    head["jobs"]["ledger-equivalence"]["timeout-minutes"] = 20
    assert _grade_equivalence(repo, _pinned(_equivalence()), head) == 0


def _other_pin_value(workflow: dict[Any, Any]) -> None:
    _install(workflow)["env"].update(dict.fromkeys(SCM_PIN, "0.0.0"))


def _generic_pin(workflow: dict[Any, Any]) -> None:
    env = _install(workflow)["env"]
    env["SETUPTOOLS_SCM_PRETEND_VERSION"] = env.pop("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS")


def _extra_install_env(workflow: dict[Any, Any]) -> None:
    _install(workflow)["env"]["PYTHONPATH"] = "head"


def _pin_without_constraint(workflow: dict[Any, Any]) -> None:
    del _install(workflow)["env"]["UV_CONSTRAINT"]


def _pin_on_the_store_replay(workflow: dict[Any, Any]) -> None:
    [store] = [step for step in _steps(_unpinned(workflow)) if str(step.get("run")).endswith('equivalence.sh" store')]
    store["env"] = dict(SCM_PIN)


def _pin_on_the_job(workflow: dict[Any, Any]) -> None:
    _unpinned(workflow)["jobs"]["ledger-equivalence"]["env"].update(SCM_PIN)


@pytest.mark.feature("protected-grader-accepts-equivalence-scm-pin")
@pytest.mark.parametrize(
    "fault",
    [
        _other_pin_value,
        _generic_pin,
        _extra_install_env,
        _pin_without_constraint,
        _pin_on_the_store_replay,
        _pin_on_the_job,
    ],
)
def test_the_required_grader_refuses_any_other_equivalence_pin(
    repo: Path, capsys: pytest.CaptureFixture[str], fault: Callable[[dict[Any, Any]], None]
) -> None:
    head = _pinned(_equivalence())
    fault(head)
    assert _grade_equivalence(repo, _unpinned(_equivalence()), head) == 1
    assert _EQUIVALENCE_REFUSED in capsys.readouterr().out.splitlines()


def _small_equivalence(env: dict[str, str]) -> dict[str, dict[Any, Any]]:
    install = {"name": "Install dependencies", "env": env, "run": "uv pip install -e './head[dev]'"}
    store = {"run": 'bash "$GRADER/tests/swarm_ledger/equivalence.sh" store'}
    job = {"runs-on": "ubuntu-latest", "steps": [install, store]}
    return {"equivalence.yml": {True: {"pull_request": {"branches": ["dev"]}}, "jobs": {"ledger-equivalence": job}}}


@pytest.mark.feature("protected-grader-accepts-equivalence-scm-pin")
@pytest.mark.parametrize(
    ("env", "problems"),
    [
        ({"UV_CONSTRAINT": "c.txt"}, 0),
        ({"UV_CONSTRAINT": "c.txt", **SCM_PIN}, 0),
        ({"UV_CONSTRAINT": "c.txt", **dict.fromkeys(SCM_PIN, "0.0.0")}, 1),
        (dict(SCM_PIN), 1),
        ({"UV_CONSTRAINT": "c.txt", **SCM_PIN, "PYTHONPATH": "head"}, 1),
    ],
    ids=["unchanged", "pinned", "other_value", "constraint_dropped", "extra_key"],
)
def test_a_small_equivalence_form_admits_only_the_exact_scm_pin(env: dict[str, str], problems: int) -> None:
    base, head = _small_equivalence({"UV_CONSTRAINT": "c.txt"}), _small_equivalence(env)
    assert len(gate_protected._equivalence_problems(base, head)) == problems
