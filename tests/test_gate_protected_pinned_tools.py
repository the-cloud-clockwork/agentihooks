from collections.abc import Callable
from typing import Any

import pytest
import yaml

from scripts import ci_reuse, gate_protected
from tests.test_gate_protected import _ROOT, _SPARSE, _full_preflight, _shallow_preflight, _workflow

pytestmark = pytest.mark.unit
DOUBLE = 'pip install "pyyaml>=6.0"'
SINGLE = "pip install 'pyyaml>=6.0'"
PINNED = "pip install --constraint .github/ci-tools/parser.lock PyYAML==6.0.3"
NEAR_MISSES = [
    "pip install --constraint .github/ci-tools/parser.lock PyYAML==6.0.2",
    "pip install --constraint .github/ci-tools/audit.lock PyYAML==6.0.3",
    "pip install PyYAML==6.0.3",
]
PINNED_SPARSE = _SPARSE + "/.github/ci-tools/\n"
REUSE_IF = "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
Form = Callable[[dict[Any, Any]], dict[Any, Any]]


def _install(step: dict[str, Any], template: str, install: str) -> dict[str, Any]:
    assert template in step["run"], "the template no longer carries this parser install"
    step["run"] = step["run"].replace(template, install)
    return step


def _preflight(form: Form, install: str, sparse: str | None) -> dict[Any, Any]:
    preflight = form(yaml.safe_load((_ROOT / ".github/workflows/branch-lanes.yml").read_text()))
    steps = preflight["jobs"]["grade"]["steps"]
    [parser] = [step for step in steps if step.get("name") == "Install workflow parser"]
    parser["run"] = f"python -m {install}"
    if sparse is not None:
        steps[0]["with"]["sparse-checkout"] = sparse
    return preflight


def _durations(retried: bool, bounded: bool, install: str) -> dict[Any, Any]:
    workflow = _workflow()
    workflow["jobs"]["reuse"] = {"if": REUSE_IF}
    steps = ci_reuse.retried_duration_provenance_steps() if retried else ci_reuse.duration_provenance_steps()
    if bounded:
        steps[0]["env"] = {"GIT_HTTP_LOW_SPEED_LIMIT": "1000", "GIT_HTTP_LOW_SPEED_TIME": "60"}
    [setup] = [step for step in steps if "-I -m pip install" in step.get("run", "")]
    _install(setup, SINGLE, install)
    workflow["jobs"]["select"] = {"steps": steps}
    workflow["jobs"]["gate"]["needs"].append("select")
    return workflow


@pytest.mark.feature("protected-grader-admits-pinned-tool-forms")
def test_the_pinned_tool_form_pins_each_floating_parser_install_and_keeps_every_other_value() -> None:
    other = _SPARSE + "/scripts/\n"
    value = {
        True: {"push": None},
        DOUBLE: SINGLE,
        "steps": [
            {"run": f"python -m {DOUBLE} && python -m {DOUBLE}", "timeout-minutes": 5, "if": None},
            {"run": f'"$RUNNER_TEMP/python" -I -m {SINGLE}\necho done\n', "continue-on-error": False},
            {"with": {"sparse-checkout": _SPARSE, "fetch-depth": 1}},
            {"with": {"sparse-checkout": other}},
        ],
    }
    assert gate_protected._pinned_tool_form(value) == {
        True: {"push": None},
        DOUBLE: PINNED,
        "steps": [
            {"run": f"python -m {PINNED} && python -m {PINNED}", "timeout-minutes": 5, "if": None},
            {"run": f'"$RUNNER_TEMP/python" -I -m {PINNED}\necho done\n', "continue-on-error": False},
            {"with": {"sparse-checkout": PINNED_SPARSE, "fetch-depth": 1}},
            {"with": {"sparse-checkout": other}},
        ],
    }


@pytest.mark.feature("protected-grader-admits-pinned-tool-forms")
@pytest.mark.parametrize("factory", [ci_reuse.reuse_job, ci_reuse.paged_reuse_job, ci_reuse.retried_reuse_job])
@pytest.mark.parametrize("install", [DOUBLE, PINNED, *NEAR_MISSES])
def test_the_protected_reuse_job_admits_its_parser_install_only_under_the_exact_pin(
    factory: Callable[[], dict[str, Any]], install: str
) -> None:
    job = factory()
    [step] = [step for step in job["steps"] if step.get("name") == "Install metadata dependencies"]
    _install(step, PINNED, install)
    assert (job in gate_protected.reuse_jobs()) is (install == PINNED)


@pytest.mark.feature("protected-grader-admits-pinned-tool-forms")
@pytest.mark.parametrize("retried", [False, True])
@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("install", [SINGLE, PINNED, *NEAR_MISSES])
def test_the_protected_durations_provenance_admits_its_parser_install_floating_or_under_the_exact_pin_only(
    retried: bool, bounded: bool, install: str
) -> None:
    base = _durations(False, False, SINGLE)
    head = _durations(retried, bounded, install)
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {})
    admitted = install in {SINGLE, PINNED}
    assert problems == ([] if admitted else ["The head skips reuse without protected durations provenance."])


@pytest.mark.feature("protected-grader-admits-pinned-tool-forms")
@pytest.mark.parametrize(
    ("form", "install", "sparse", "admitted"),
    [
        (_shallow_preflight, DOUBLE, None, True),
        (_full_preflight, DOUBLE, None, True),
        (_shallow_preflight, PINNED, PINNED_SPARSE, True),
        (_full_preflight, PINNED, None, True),
        (_shallow_preflight, PINNED, None, False),
        (_shallow_preflight, DOUBLE, PINNED_SPARSE, False),
        *((_shallow_preflight, near, PINNED_SPARSE, False) for near in NEAR_MISSES),
        *((_full_preflight, near, None, False) for near in NEAR_MISSES),
    ],
)
def test_the_branch_lane_preflight_admits_its_parser_install_floating_or_under_the_exact_pin_only(
    form: Form, install: str, sparse: str | None, admitted: bool
) -> None:
    trusted = _preflight(_shallow_preflight, DOUBLE, None)
    head = _preflight(form, install, sparse)
    problems = gate_protected._preflight_problems({"branch-lanes.yml": trusted}, {"branch-lanes.yml": head})
    assert problems == ([] if admitted else ["The head changes or removes the trusted branch lane preflight."])
