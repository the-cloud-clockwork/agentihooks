import json
from pathlib import Path

import pytest
import yaml

from scripts import ci_wiring

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


def _gate_workflow(**jobs):
    return {
        True: {"pull_request": {"branches": ["dev"]}, "merge_group": None, "push": {"branches": ["dev"]}},
        "jobs": {
            "unit": {"runs-on": "ubuntu-latest"},
            "lint": {"runs-on": "ubuntu-latest"},
            "gate": {"name": "Gate — Required", "needs": ["unit", "lint"]},
            **jobs,
        },
    }


def _write(root: Path, workflows: dict, config: dict) -> None:
    folder = root / ".github/workflows"
    folder.mkdir(parents=True)
    for name, workflow in workflows.items():
        (folder / name).write_text(yaml.safe_dump(workflow, allow_unicode=True))
    (root / ".github/gate-wiring.json").write_text(json.dumps(config))


def test_the_repository_wiring_is_green(capsys):
    assert ci_wiring.main(["--root", str(_ROOT)]) == 0
    assert "0 wiring problems" in capsys.readouterr().out


def test_a_wired_gate_has_no_problems():
    assert ci_wiring.check({"test.yml": _gate_workflow()}, {}) == []


def test_a_planted_job_outside_the_needs_is_red(tmp_path, capsys):
    _write(tmp_path, {"test.yml": _gate_workflow(planted={"runs-on": "ubuntu-latest"})}, {})
    assert ci_wiring.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "1 workflows, 4 jobs, 1 wiring problems" in out
    assert "::error::test.yml/planted runs on pull requests but is not a need of Gate — Required." in out


def test_a_declared_non_gate_passes_and_a_contradicting_one_is_red():
    workflow = _gate_workflow(record={"needs": ["unit"]})
    assert ci_wiring.check({"test.yml": workflow}, {"not_gates": {"test.yml/record": "records"}}) == []
    assert ci_wiring.check({"test.yml": workflow}, {"not_gates": {"test.yml/unit": "x", "test.yml/record": "r"}}) == [
        "test.yml/unit is a need of Gate — Required and also declared a non gate."
    ]


def test_a_stale_non_gate_is_red():
    assert ci_wiring.check(
        {"test.yml": _gate_workflow()}, {"not_gates": {"test.yml/gone": "x", "other.yml/unit": "y"}}
    ) == [
        "test.yml/gone is declared a non gate but is no job of test.yml.",
        "other.yml/unit is declared a non gate but is no job of test.yml.",
    ]


def test_an_exemption_without_a_reason_is_red():
    workflows = {"test.yml": _gate_workflow(record={}), "smoke.yml": {"on": "pull_request", "jobs": {}}}
    config = {"not_gates": {"test.yml/record": ""}, "outside_gate": {"smoke.yml": ""}}
    assert ci_wiring.check(workflows, config) == [
        "test.yml/record is declared without a reason.",
        "smoke.yml is declared without a reason.",
    ]


@pytest.mark.parametrize("count", [0, 2])
def test_exactly_one_gate_must_exist(count):
    workflows = {f"w{index}.yml": _gate_workflow() for index in range(count)}
    assert ci_wiring.check(workflows, {"not_gates": {"x": ""}}) == [
        "x is declared without a reason.",
        f"{count} jobs are named Gate — Required; exactly one must be.",
    ]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_the_gate_must_run_on_pull_requests_and_the_merge_queue(event):
    workflow = _gate_workflow()
    del workflow[True][event]
    assert ci_wiring.check({"test.yml": workflow}, {}) == [
        f"test.yml holds Gate — Required but does not run on {event}."
    ]


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("key", ["paths", "paths-ignore"])
def test_a_core_gate_path_filter_is_red(event, key):
    workflow = _gate_workflow()
    workflow[True][event][key] = ["docs/**"]
    assert ci_wiring.check({"test.yml": workflow}, {}) == [
        f"test.yml filters its {event} trigger by {key}, so a core gate skips some changes."
    ]


def test_a_merge_group_path_filter_is_not_a_trigger_github_reads():
    workflow = _gate_workflow()
    workflow[True]["merge_group"] = {"paths": ["docs/**"]}
    assert ci_wiring.check({"test.yml": workflow}, {}) == []


@pytest.mark.parametrize("on", ["pull_request", ["pull_request"], {"pull_request_target": None}, {"merge_group": {}}])
def test_another_pull_request_workflow_is_red_unless_declared(on):
    workflows = {"test.yml": _gate_workflow(), "smoke.yml": {"on": on, "jobs": {"smoke": {}}}}
    assert ci_wiring.check(workflows, {}) == [
        "smoke.yml runs on pull requests outside test.yml, so its jobs cannot be needs of Gate — Required."
    ]
    assert ci_wiring.check(workflows, {"outside_gate": {"smoke.yml": "scoped"}}) == []


def test_workflows_off_the_pull_request_path_need_no_declaration():
    workflows = {
        "test.yml": _gate_workflow(),
        "called.yml": {"on": {"workflow_call": None}, "jobs": {"smoke": {}}},
        "nightly.yml": {"on": {"schedule": [{"cron": "0 0 * * *"}]}, "jobs": {"sweep": {}}},
        "empty.yml": {},
    }
    assert ci_wiring.check(workflows, {}) == []


def test_a_stale_outside_declaration_is_red():
    workflows = {"test.yml": _gate_workflow(), "called.yml": {"on": "workflow_call", "jobs": {}}}
    assert ci_wiring.check(workflows, {"outside_gate": {"called.yml": "was scoped", "gone.yml": "removed"}}) == [
        "called.yml is declared outside the gate but no longer runs on pull requests.",
        "gone.yml is declared outside the gate but does not exist.",
    ]


def test_needs_may_be_a_single_job_name():
    workflow = _gate_workflow()
    workflow["jobs"]["gate"]["needs"] = "unit"
    assert ci_wiring.check({"test.yml": workflow}, {"not_gates": {"test.yml/lint": "x"}}) == []


def test_load_reads_both_yaml_extensions_and_empty_files(tmp_path):
    folder = tmp_path / ".github/workflows"
    folder.mkdir(parents=True)
    (folder / "a.yml").write_text("on: push\n")
    (folder / "b.yaml").write_text("on: pull_request\n")
    (folder / "c.yml").write_text("")
    (folder / "notes.md").write_text("on: pull_request\n")
    assert ci_wiring.load(tmp_path) == {"a.yml": {True: "push"}, "b.yaml": {True: "pull_request"}, "c.yml": {}}


def test_triggers_reads_every_form():
    assert ci_wiring.triggers({True: "push"}) == {"push": {}}
    assert ci_wiring.triggers({"on": ["push", "pull_request"]}) == {"push": {}, "pull_request": {}}
    assert ci_wiring.triggers({True: {"push": None, "pull_request": {"paths": ["a"]}}}) == {
        "push": {},
        "pull_request": {"paths": ["a"]},
    }
    assert ci_wiring.triggers({}) == {}


def test_every_gate_need_in_the_workflow_is_wired_into_the_repository_gate():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    needs = set(workflow["jobs"]["gate-required"]["needs"])
    assert {"wiring", "brain-smoke"} <= needs
    assert workflow["jobs"]["wiring"]["steps"][-1]["run"] == "python -m scripts.ci_wiring"
    assert "needs" not in workflow["jobs"]["wiring"]
