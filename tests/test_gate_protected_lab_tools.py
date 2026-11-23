import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import ci_frozen, ci_runner_trust, ci_wiring, gate_protected

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = "e285593508ecbac8c01119645580a1d65e48b5e6"
STEP = "Install the lab verdict dependencies"
FLOATING = "python -m pip install --quiet PyYAML==6.0.3 jsonschema==4.25.1"
PINNED = (
    "python -m pip install --quiet --constraint grader/.github/ci-tools/parser.lock "
    "--constraint grader/.github/ci-tools/lint-tools.lock PyYAML==6.0.3 jsonschema==4.25.1"
)
REFUSED = "The head's lab verdict job differs from the reviewed rendering; the protected branch grades with its own."
Workflows = dict[str, dict[str, Any]]
Forms = tuple[Workflows, ci_wiring.Wiring, dict[str, Any]]
Grade = Callable[[Workflows, ci_wiring.Wiring, Workflows, ci_wiring.Wiring, object], list[str]]


@pytest.fixture(scope="module")
def grader() -> Grade:
    return gate_protected.grade


@pytest.fixture(params=["full", "small"])
def forms(request: pytest.FixtureRequest) -> Forms:
    workflows = ci_wiring.load(ROOT)
    if request.param == "small":
        workflows["test.yml"]["jobs"]["lab-verdict"] = {
            "runs-on": "reviewed-runner",
            "if": "github.event_name == 'pull_request'",
            "permissions": {"contents": "read"},
            "outputs": {"authoritative": "${{ steps.authority.outputs.authoritative }}"},
            "steps": [{"name": STEP, "run": FLOATING}],
        }
    return (
        workflows,
        json.loads((ROOT / ci_wiring.CONFIG).read_text()),
        json.loads((ROOT / ci_runner_trust.ACTORS).read_text()),
    )


def _install(workflows: Workflows, command: str) -> None:
    [step] = [step for step in workflows["test.yml"]["jobs"]["lab-verdict"]["steps"] if step.get("name") == STEP]
    step["run"] = command


def _grade(
    grader: Grade, base: Workflows, head: Workflows, config: ci_wiring.Wiring, actors: dict[str, Any]
) -> list[str]:
    before = copy.deepcopy((base, head, config, actors))
    result = grader(base, config, head, config, actors)
    assert (base, head, config, actors) == before
    return result


@pytest.mark.feature("protected-grader-admits-lab-tool-pins")
def test_the_lab_tool_pin_candidate_is_stored_under_its_announced_hash() -> None:
    registry = json.loads((ROOT / gate_protected.FROZEN).read_text())
    content = (ROOT / ci_frozen.CANDIDATES / CANDIDATE).read_bytes()
    assert CANDIDATE in registry["scripts/gate_protected.py"]
    assert (
        hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest()
        == CANDIDATE
    )


@pytest.mark.feature("protected-grader-admits-lab-tool-pins")
@pytest.mark.parametrize("install", [FLOATING, PINNED], ids=["floating", "pinned"])
def test_the_candidate_preserves_unchanged_full_and_small_lab_jobs(grader: Grade, forms: Forms, install: str) -> None:
    base, config, actors = forms
    _install(base, install)
    assert _grade(grader, base, copy.deepcopy(base), config, actors) == []


@pytest.mark.feature("protected-grader-admits-lab-tool-pins")
@pytest.mark.parametrize(
    "edit", ["drop", "forge", "if", "runs-on", "permissions", "outputs", "timeout-minutes", "step"]
)
def test_the_candidate_refuses_changes_to_the_reviewed_lab_job(grader: Grade, forms: Forms, edit: str) -> None:
    base, config, actors = forms
    head = copy.deepcopy(base)
    job = head["test.yml"]["jobs"]["lab-verdict"]
    if edit == "drop":
        head["test.yml"]["jobs"].pop("lab-verdict")
    elif edit == "forge":
        head["test.yml"]["jobs"]["lab-verdict"] = {"steps": [{"run": "true"}]}
    elif edit == "step":
        job["steps"].append({"run": "true"})
    else:
        job[edit] = "unreviewed"
    assert _grade(grader, base, head, config, actors) == [REFUSED]


@pytest.mark.feature("protected-grader-admits-lab-tool-pins")
@pytest.mark.parametrize(
    "install",
    [
        PINNED,
        PINNED.replace("--constraint grader/.github/ci-tools/parser.lock ", ""),
        PINNED.replace("--constraint grader/.github/ci-tools/lint-tools.lock ", ""),
        PINNED.replace("grader/.github/ci-tools/parser.lock", ".github/ci-tools/parser.lock"),
        PINNED.replace("grader/.github/ci-tools/lint-tools.lock", ".github/ci-tools/lint-tools.lock"),
        PINNED.replace("grader/", "head/"),
        PINNED.replace("parser.lock", "test.lock"),
        PINNED.replace("PyYAML==6.0.3", "PyYAML==6.0.2"),
        PINNED.replace("jsonschema==4.25.1", "jsonschema>=4.25.1"),
        PINNED + " requests==2.32.5",
        PINNED + "\ntrue",
        PINNED + " && true",
        PINNED + "; true",
        (
            "python -m pip install --quiet --constraint grader/.github/ci-tools/lint-tools.lock "
            "--constraint grader/.github/ci-tools/parser.lock PyYAML==6.0.3 jsonschema==4.25.1"
        ),
    ],
    ids=[
        "exact",
        "no-parser",
        "no-lint",
        "head-parser",
        "head-lint",
        "other-checkout",
        "other-lock",
        "version",
        "range",
        "package",
        "command",
        "and-command",
        "then-command",
        "swapped-locks",
    ],
)
def test_the_candidate_admits_only_the_exact_paired_grader_locks(grader: Grade, forms: Forms, install: str) -> None:
    base, config, actors = forms
    head = copy.deepcopy(base)
    _install(head, install)
    expected = [] if install == PINNED else [REFUSED]
    assert _grade(grader, base, head, config, actors) == expected


@pytest.mark.feature("protected-grader-admits-lab-tool-pins")
def test_a_pinned_base_refuses_a_head_that_removes_the_lab_constraints(grader: Grade, forms: Forms) -> None:
    base, config, actors = forms
    head = copy.deepcopy(base)
    _install(base, PINNED)
    assert _grade(grader, base, head, config, actors) == [REFUSED]


@pytest.mark.feature("protected-grader-admits-lab-tool-pins")
@pytest.mark.parametrize("forms", ["full"], indirect=True)
@pytest.mark.parametrize("install", [FLOATING, PINNED], ids=["floating", "pinned"])
@pytest.mark.parametrize("edit", ["changed", "dropped"])
def test_the_candidate_refuses_a_change_to_any_other_lab_step(
    grader: Grade, forms: Forms, install: str, edit: str
) -> None:
    base, config, actors = forms
    others = [
        index for index, step in enumerate(base["test.yml"]["jobs"]["lab-verdict"]["steps"]) if step.get("name") != STEP
    ]
    assert others
    for index in others:
        head = copy.deepcopy(base)
        _install(head, install)
        steps = head["test.yml"]["jobs"]["lab-verdict"]["steps"]
        if edit == "dropped":
            del steps[index]
        else:
            steps[index]["continue-on-error"] = True
        assert _grade(grader, base, head, config, actors) == [REFUSED], index


@pytest.mark.feature("protected-grader-admits-lab-tool-pins")
@pytest.mark.parametrize("install", [FLOATING, PINNED], ids=["floating", "pinned"])
@pytest.mark.parametrize("edit", ["env", "if", "continue-on-error", "twice"])
def test_the_candidate_refuses_any_other_change_to_the_install_step(
    grader: Grade, forms: Forms, install: str, edit: str
) -> None:
    base, config, actors = forms
    head = copy.deepcopy(base)
    _install(head, install)
    steps = head["test.yml"]["jobs"]["lab-verdict"]["steps"]
    [step] = [step for step in steps if step.get("name") == STEP]
    if edit == "twice":
        steps.append(copy.deepcopy(step))
    else:
        step[edit] = "unreviewed"
    assert _grade(grader, base, head, config, actors) == [REFUSED]


@pytest.mark.feature("protected-grader-admits-lab-tool-pins")
@pytest.mark.parametrize("install", [FLOATING, PINNED], ids=["floating", "pinned"])
def test_a_base_without_a_lab_job_refuses_a_head_that_adds_one(grader: Grade, forms: Forms, install: str) -> None:
    base, config, actors = forms
    head = copy.deepcopy(base)
    _install(head, install)
    base["test.yml"]["jobs"].pop("lab-verdict")
    assert REFUSED in _grade(grader, base, head, config, actors)
