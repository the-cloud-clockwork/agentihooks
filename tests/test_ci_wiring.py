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


def test_the_root_defaults_to_the_working_directory(tmp_path, monkeypatch, capsys):
    _write(tmp_path, {"test.yml": _gate_workflow(planted={})}, {})
    monkeypatch.chdir(tmp_path)
    assert ci_wiring.main([]) == 1
    assert "test.yml/planted runs on pull requests" in capsys.readouterr().out


def test_help_names_what_the_check_enforces(capsys):
    with pytest.raises(SystemExit):
        ci_wiring.main(["--help"])
    assert "fail unless every pull request job is a need of Gate — Required" in capsys.readouterr().out


def test_a_wired_gate_has_no_problems():
    assert ci_wiring.check({"test.yml": _gate_workflow()}, {}) == []


def test_a_planted_job_outside_the_needs_is_red(tmp_path, capsys):
    _write(tmp_path, {"test.yml": _gate_workflow(planted={"runs-on": "ubuntu-latest"})}, {})
    assert ci_wiring.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "1 workflows, 4 jobs, 1 wiring problems" in out
    assert "::error::test.yml/planted runs on pull requests but is not a need of Gate — Required." in out


def test_a_declared_non_gate_passes_and_one_naming_a_need_is_red():
    workflow = _gate_workflow(record={"needs": ["unit"]})
    assert ci_wiring.check({"test.yml": workflow}, {"not_gates": {"test.yml/record": "records"}}) == []
    assert ci_wiring.check({"test.yml": workflow}, {"not_gates": {"test.yml/unit": "x", "test.yml/record": "r"}}) == [
        "test.yml/unit is declared a non gate but names no pull request job of test.yml outside the needs of Gate — Required."
    ]


def test_a_stale_non_gate_is_red():
    workflow = _gate_workflow(refresh={"if": "github.event_name == 'push'"})
    config = {"not_gates": {"test.yml/gone": "x", "other.yml/unit": "y", "test.yml/refresh": "z"}}
    assert ci_wiring.check({"test.yml": workflow}, config) == [
        f"{key} is declared a non gate but names no pull request job of test.yml outside the needs of Gate — Required."
        for key in ("test.yml/gone", "other.yml/unit", "test.yml/refresh")
    ]


@pytest.mark.parametrize(
    ("condition", "runs"),
    [
        ("github.event_name == 'push'", False),
        ("${{ github.event_name == 'schedule' }}", False),
        ("${{github.event_name == 'workflow_dispatch'}}", False),
        (" github.event_name == 'push' ", False),
        ("github.event_name == 'pull_request'", True),
        ("github.event_name == 'pull_request_target'", True),
        ("${{ github.event_name == 'merge_group' }}", True),
        ("github.event_name == 'push' || github.event_name == 'pull_request'", True),
        ("github.event_name != 'push'", True),
        ("always()", True),
        (None, True),
    ],
)
def test_only_a_bare_off_pull_request_event_condition_takes_a_job_off_the_path(condition, runs):
    job = {} if condition is None else {"if": condition}
    assert ci_wiring.on_pull_requests(job) is runs
    workflow = _gate_workflow(extra=job)
    expected = ["test.yml/extra runs on pull requests but is not a need of Gate — Required."] if runs else []
    assert ci_wiring.check({"test.yml": workflow}, {}) == expected


def test_an_exemption_without_a_reason_is_red():
    workflows = {"test.yml": _gate_workflow(record={}), "smoke.yml": {"on": "pull_request", "jobs": {"smoke": {}}}}
    config = {"not_gates": {"test.yml/record": ""}, "outside_gate": {"smoke.yml/smoke": ""}}
    assert ci_wiring.check(workflows, config) == [
        "test.yml/record is declared without a reason.",
        "smoke.yml/smoke is declared without a reason.",
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
@pytest.mark.parametrize("key", ["paths", "paths-ignore", "types"])
def test_a_core_gate_trigger_filter_is_red(event, key):
    workflow = _gate_workflow()
    workflow[True][event][key] = ["docs/**"]
    assert ci_wiring.check({"test.yml": workflow}, {}) == [
        f"test.yml filters its {event} trigger by {key}, so a core gate skips some changes."
    ]


def test_a_merge_group_filter_is_not_a_trigger_github_reads():
    workflow = _gate_workflow()
    workflow[True]["merge_group"] = {"paths": ["docs/**"], "types": ["checks_requested"]}
    assert ci_wiring.check({"test.yml": workflow}, {}) == []


@pytest.mark.parametrize("on", ["pull_request", ["pull_request"], {"pull_request_target": None}, {"merge_group": {}}])
def test_another_pull_request_job_is_red_unless_declared(on):
    jobs = {"smoke": {}, "late": {"needs": "smoke"}, "nightly": {"if": "github.event_name == 'schedule'"}}
    workflows = {"test.yml": _gate_workflow(), "smoke.yml": {"on": on, "jobs": jobs}}
    assert ci_wiring.check(workflows, {}) == [
        f"smoke.yml/{job} runs on pull requests outside test.yml, so it cannot be a need of Gate — Required."
        for job in ("smoke", "late")
    ]
    assert ci_wiring.check(workflows, {"outside_gate": {"smoke.yml/smoke": "scoped"}}) == [
        "smoke.yml/late runs on pull requests outside test.yml, so it cannot be a need of Gate — Required."
    ]
    assert ci_wiring.check(workflows, {"outside_gate": {"smoke.yml/smoke": "a", "smoke.yml/late": "b"}}) == []


def test_workflows_off_the_pull_request_path_need_no_declaration():
    workflows = {
        "test.yml": _gate_workflow(),
        "called.yml": {"on": {"workflow_call": None}, "jobs": {"smoke": {}}},
        "nightly.yml": {"on": {"schedule": [{"cron": "0 0 * * *"}]}, "jobs": {"sweep": {}}},
        "empty.yml": {},
    }
    assert ci_wiring.check(workflows, {}) == []


def test_a_stale_outside_declaration_is_red():
    workflows = {
        "test.yml": _gate_workflow(),
        "called.yml": {"on": "workflow_call", "jobs": {"smoke": {}}},
        "smoke.yml": {"on": "pull_request", "jobs": {"push": {"if": "github.event_name == 'push'"}}},
    }
    outside = {"called.yml/smoke": "a", "gone.yml/job": "b", "smoke.yml/push": "c", "test.yml/unit": "d"}
    assert ci_wiring.check(workflows, {"outside_gate": outside}) == [
        f"{key} is declared outside the gate but names no pull request job outside test.yml." for key in outside
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


def test_the_tests_workflow_runs_wiring_and_brain_smoke_as_parallel_gate_needs():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    needs = set(workflow["jobs"]["gate-required"]["needs"])
    assert {"wiring", "brain-smoke"} <= needs
    assert workflow["jobs"]["wiring"]["steps"][-1]["run"] == "python -m scripts.ci_wiring"
    assert "needs" not in workflow["jobs"]["wiring"]
