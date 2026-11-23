import json
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scripts.ci_lab import intake, scheduler
from scripts.ci_lab.units import Priority
from tests.ci_lab import test_scheduler as fixtures
from tests.ci_lab.test_intake import head
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import staticless as staticless


@pytest.mark.feature("lab-plant-test-units")
@pytest.mark.parametrize(("event", "plant"), [("plant", True), ("pull_request", False), ("push", False)])
def test_only_a_plant_event_marks_its_grade_request(event: str, plant: bool) -> None:
    request = intake.request_from(head(event, "nestorcolt", "pushed"))
    assert asdict(request)["plant"] is plant


@pytest.mark.feature("lab-plant-test-units")
@pytest.mark.parametrize("plant", [False, True])
def test_a_stored_grade_request_keeps_its_plant_marker(plant: bool) -> None:
    payload = asdict(intake.request_from(head("pull_request", "nestorcolt", "synchronize")))
    payload["plant"] = plant
    restored = scheduler._request(payload)
    assert asdict(restored)["plant"] is plant
    assert asdict(restored) == payload


@pytest.mark.feature("lab-plant-test-units")
def test_a_grade_request_stored_before_plant_markers_loads_as_an_ordinary_request() -> None:
    payload = asdict(intake.request_from(head("pull_request", "nestorcolt", "synchronize")))
    payload.pop("plant", None)
    restored = scheduler._request(payload)
    assert asdict(restored) == {**payload, "plant": False}


@pytest.mark.feature("lab-plant-test-units")
def test_a_plant_keeps_its_marker_while_held_before_expansion(lab: Callable[..., fixtures.Run]) -> None:
    run = lab({fixtures.HEAD_A: fixtures.pull_plan({})}, quiet_ms=500)
    run.push(fixtures.HEAD_A, event="plant")
    run.tick(1000)
    assert run.lab.expander.seen == []
    held = json.loads(next(iter(run.lab.client.hvals(scheduler.WAITING))))
    assert held["request"]["plant"] is True
    run.tick(1500)
    assert asdict(run.lab.expander.seen[0])["plant"] is True


@pytest.mark.feature("lab-plant-test-units")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize(("listed", "added"), [(False, True), (True, False), (True, True)])
def test_a_pull_request_plant_grades_only_its_added_and_changed_test_modules(
    tmp_path: Path, listed: bool, added: bool
) -> None:
    suite = fixtures._suite(tmp_path, fixtures.DELIVERY["lab"])
    fixtures._git(suite.repo, "switch", "-q", "-c", "plant", suite.base)
    edits = {"scripts/alpha.py": fixtures.ALPHA.replace("return 2", "return 7")}
    if listed:
        edits["tests/test_alpha.py"] = "def test_first():\n    assert True\n\n\ndef test_other():\n    pass\n"
    if added:
        edits[fixtures.GAMMA] = "def test_third():\n    pass\n"
    commit = fixtures._commit(suite.repo, edits)
    port = fixtures.Port(fixtures.SUITE_MANIFEST)
    request = replace(suite.request(Priority.PULL_REQUEST, commit), plant=True)
    found = port.expander(tmp_path).expand(request)
    expected = [(fixtures.FIRST_TEST,), (fixtures.OTHER_TEST,)] if listed else []
    if added:
        expected.append((fixtures.GAMMA,))
    assert [unit.members for unit in found.gates["unit"]] == expected
    assert port.looked == []
    assert found.manifest == "dev"
    assert found.grader == suite.base
    assert found.tree == fixtures._tree(suite, commit)
    assert set(found.gates) == {*scheduler.PULL_REQUIRED, "unit"}


@pytest.mark.feature("lab-plant-test-units")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize("deleted", [False, True])
def test_a_plant_with_no_added_or_changed_test_module_grades_no_test_unit(tmp_path: Path, deleted: bool) -> None:
    suite = fixtures._suite(tmp_path, fixtures.DELIVERY["lab"])
    fixtures._git(suite.repo, "switch", "-q", "-c", "plant", suite.base)
    if deleted:
        fixtures._git(suite.repo, "rm", "tests/test_alpha.py")
    commit = fixtures._commit(suite.repo, {"scripts/alpha.py": fixtures.ALPHA.replace("return 2", "return 7")})
    request = replace(suite.request(Priority.PULL_REQUEST, commit), plant=True)
    found = fixtures.Port(fixtures.SUITE_MANIFEST).expander(tmp_path).expand(request)
    assert found.gates["unit"] == ()


@pytest.mark.feature("lab-plant-test-units")
@pytest.mark.usefixtures("staticless")
def test_a_plant_unit_job_posts_its_check(tmp_path: Path, lab: Callable[..., fixtures.Run]) -> None:
    suite = fixtures._suite(tmp_path, fixtures.DELIVERY["lab"])
    commit = fixtures._commit(suite.repo, {fixtures.GAMMA: "def test_third():\n    pass\n"})
    request = replace(suite.request(Priority.PULL_REQUEST, commit), plant=True)
    found = fixtures.Port(fixtures.SUITE_MANIFEST).expander(tmp_path).expand(request)
    run = lab({fixtures.HEAD_A: found})
    run.push(fixtures.HEAD_A, event="plant")
    run.tick(1000)
    assert run.lab.poster.moves(fixtures.HEAD_A, "unit") == [("queued", None)]
