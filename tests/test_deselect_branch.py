import json

import pytest

from tests import dep_manifest

pytestmark = pytest.mark.unit

COLLECTED = ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one", "tests/test_b.py::new"]
DEV = {
    "run": "5",
    "commit": "c" * 40,
    "global": None,
    "invalidated": 2,
    "tests": ["tests/test_a.py::one", "tests/test_b.py::one"],
}
BRANCH = {
    "version": 1,
    "runs": [{"run": 7, "attempt": 1, "commit": "d" * 40, "head": "e" * 40, "tests": []}],
    "tests": ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_gone.py::x"],
}
EMPTY_BRANCH = {"version": 1, "runs": [], "tests": []}


def _deselect(tmp_path, *plans):
    args = ["deselect"]
    for index, plan in enumerate(plans):
        path = tmp_path / f"plan-{index}.json"
        path.write_text(json.dumps(plan))
        args += ["--plan", str(path)]
    (tmp_path / "collected.json").write_text(json.dumps(COLLECTED))
    out = tmp_path / "reused.json"
    assert dep_manifest.main([*args, "--collected", str(tmp_path / "collected.json"), "--out", str(out)]) == 0
    return json.loads(out.read_text())


@pytest.mark.parametrize("order", [(DEV, BRANCH), (BRANCH, DEV)])
def test_a_dev_and_a_branch_plan_deselect_their_union_and_name_the_branch_only_tests(tmp_path, capsys, order):
    assert _deselect(tmp_path, *order) == {
        "tests": ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one"],
        "files": ["tests/test_a.py"],
        "branch": ["tests/test_a.py::two"],
    }
    assert capsys.readouterr().out == (
        "Reusing 3 collected tests; 1 test files are not collected\n"
        "1 of them passed on earlier runs of this pull request\n"
    )


def test_a_single_dev_plan_keeps_its_earlier_output(tmp_path, capsys):
    assert _deselect(tmp_path, DEV) == dep_manifest.deselect(DEV["tests"], COLLECTED)
    assert capsys.readouterr().out == "Reusing 2 collected tests; 0 test files are not collected\n"


def test_a_branch_plan_alone_names_every_test_it_deselects(tmp_path):
    chosen = _deselect(tmp_path, BRANCH)
    assert chosen["branch"] == chosen["tests"] == ["tests/test_a.py::one", "tests/test_a.py::two"]


def test_an_empty_branch_plan_names_no_branch_test(tmp_path):
    assert _deselect(tmp_path, DEV, EMPTY_BRANCH) == {
        **dep_manifest.deselect(DEV["tests"], COLLECTED),
        "branch": [],
    }


def test_several_plans_without_a_branch_plan_name_no_branch_test(tmp_path):
    other = {**DEV, "tests": ["tests/test_a.py::two"]}
    assert _deselect(tmp_path, DEV, other) == {
        **dep_manifest.deselect([*DEV["tests"], *other["tests"]], COLLECTED),
        "branch": [],
    }


def test_branch_only_keeps_ids_no_dev_plan_holds():
    plans = [DEV, BRANCH, {"tests": ["tests/test_a.py::two"]}]
    assert dep_manifest.branch_only(plans, ["tests/test_a.py::one", "tests/test_a.py::two", "tests/x.py::y"]) == [
        "tests/x.py::y"
    ]


def test_the_plan_flag_is_required(tmp_path):
    with pytest.raises(SystemExit):
        dep_manifest.main(["deselect", "--collected", str(tmp_path / "c.json"), "--out", str(tmp_path / "o.json")])
