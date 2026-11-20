from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import gate_protected
from tests.test_gate_protected import _EQUIVALENCE_REFUSED, _equivalence, _equivalence_steps, _git, _grade_equivalence

pytestmark = pytest.mark.unit
SCM_PIN = {"SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS": "0.dev${{ github.run_number }}"}
UNPINNED = {"UV_CONSTRAINT": "c.txt"}
PINNED = {**UNPINNED, **SCM_PIN}
OTHER_VALUE = {**UNPINNED, **dict.fromkeys(SCM_PIN, "0.0.0")}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "dev")
    return tmp_path


def _install(workflow: dict[Any, Any]) -> dict[str, Any]:
    [install] = [step for step in _equivalence_steps(workflow) if step.get("name") == "Install dependencies"]
    return install


def _unpinned(workflow: dict[Any, Any]) -> dict[Any, Any]:
    install = _install(workflow)
    install["env"] = {key: value for key, value in install["env"].items() if key not in SCM_PIN}
    return workflow


def _pinned(workflow: dict[Any, Any]) -> dict[Any, Any]:
    _install(_unpinned(workflow))["env"].update(SCM_PIN)
    return workflow


def _tuned(workflow: dict[Any, Any]) -> dict[Any, Any]:
    [upload] = [step for step in _equivalence_steps(workflow) if "retry-upload" in str(step.get("uses"))]
    upload["with"]["retention-days"] = 1
    return workflow


@pytest.mark.feature("protected-grader-accepts-equivalence-scm-pin")
@pytest.mark.parametrize(
    ("base", "head"),
    [(_unpinned, _pinned), (_pinned, _unpinned), (_pinned, _pinned)],
    ids=["dev_without_pin_head_adds_it", "dev_with_pin_head_branched_before_it", "dev_and_head_with_pin"],
)
def test_the_required_grader_accepts_a_tuned_equivalence_install_with_or_without_the_scm_pin(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    base: Callable[[dict[Any, Any]], dict[Any, Any]],
    head: Callable[[dict[Any, Any]], dict[Any, Any]],
) -> None:
    assert _grade_equivalence(repo, base(_equivalence()), _tuned(head(_equivalence()))) == 0
    assert "0 protected gate problems" in capsys.readouterr().out


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
    steps = _equivalence_steps(_unpinned(workflow))
    [store] = [step for step in steps if str(step.get("run")).endswith('equivalence.sh" store')]
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


def _small_equivalence(env: dict[str, str], retention: int = 7) -> dict[str, dict[Any, Any]]:
    install = {"name": "Install dependencies", "env": env, "run": "uv pip install -e './head[dev]'"}
    store = {"run": 'bash "$GRADER/tests/swarm_ledger/equivalence.sh" store'}
    upload = {"uses": "./head/.github/actions/retry-upload", "with": {"retention-days": retention}}
    job = {"runs-on": "ubuntu-latest", "steps": [install, store, upload]}
    return {"equivalence.yml": {True: {"pull_request": {"branches": ["dev"]}}, "jobs": {"ledger-equivalence": job}}}


@pytest.mark.feature("protected-grader-accepts-equivalence-scm-pin")
@pytest.mark.parametrize(
    ("base", "head", "problems"),
    [
        (UNPINNED, UNPINNED, 0),
        (UNPINNED, PINNED, 0),
        (PINNED, UNPINNED, 0),
        (PINNED, PINNED, 0),
        (UNPINNED, OTHER_VALUE, 1),
        (PINNED, OTHER_VALUE, 1),
        (UNPINNED, dict(SCM_PIN), 1),
        (UNPINNED, {**PINNED, "PYTHONPATH": "head"}, 1),
    ],
    ids=[
        "unchanged",
        "pin_added",
        "branched_before_the_pin",
        "pin_kept",
        "other_value",
        "other_value_on_a_pinned_dev",
        "constraint_dropped",
        "extra_key",
    ],
)
def test_a_small_equivalence_form_admits_the_install_with_or_without_the_exact_scm_pin(
    base: dict[str, str], head: dict[str, str], problems: int
) -> None:
    graded = gate_protected._equivalence_problems(_small_equivalence(base), _small_equivalence(head, retention=1))
    assert len(graded) == problems


@pytest.mark.feature("protected-grader-accepts-equivalence-scm-pin")
def test_the_scm_pin_form_sets_or_drops_the_pin_on_every_pip_install_step_of_a_copy() -> None:
    checkout: dict[str, Any] = {"uses": "actions/checkout@v4"}
    install: dict[str, Any] = {"env": dict(OTHER_VALUE), "run": "uv pip install -e ./head"}
    bare: dict[str, Any] = {"run": "pip install -e ./base"}
    store: dict[str, Any] = {"run": 'bash "$GRADER/tests/swarm_ledger/equivalence.sh" store'}
    assert gate_protected._scm_pin_form([checkout, install, bare, store], SCM_PIN) == [
        checkout,
        {"env": PINNED, "run": "uv pip install -e ./head"},
        {"env": SCM_PIN, "run": "pip install -e ./base"},
        store,
    ]
    assert gate_protected._scm_pin_form([install], {}) == [{"env": UNPINNED, "run": "uv pip install -e ./head"}]
    assert (install["env"], bare) == (OTHER_VALUE, {"run": "pip install -e ./base"})
