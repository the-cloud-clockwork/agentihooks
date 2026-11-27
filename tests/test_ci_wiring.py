import inspect
import json
from pathlib import Path
from typing import Any

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


def _entry(reason="why", owner="ci-1@swarm", expires="2026-10-08"):
    return {"reason": reason, "owner": owner, "expires": expires}


def _declared(*keys):
    return {key: _entry() for key in keys}


def _check(workflows, config):
    return ci_wiring.check(workflows, config)


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


def test_main_ignores_declaration_dates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = {
        "not_gates": {"test.yml/old": _entry(expires="2000-01-01"), "test.yml/new": _entry(expires="invalid-date")}
    }
    _write(tmp_path, {"test.yml": _gate_workflow(old={}, new={})}, config)
    assert ci_wiring.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "1 workflows, 5 jobs, 0 wiring problems\n"


def test_the_check_takes_no_date() -> None:
    assert "today" not in inspect.signature(ci_wiring.check).parameters


def test_help_names_what_the_check_enforces(capsys):
    with pytest.raises(SystemExit):
        ci_wiring.main(["--help"])
    assert "fail unless every pull request job is a need of Gate — Required" in capsys.readouterr().out


def test_a_wired_gate_has_no_problems():
    assert _check({"test.yml": _gate_workflow()}, {}) == []


def test_a_planted_job_outside_the_needs_is_red(tmp_path, capsys):
    _write(tmp_path, {"test.yml": _gate_workflow(planted={"runs-on": "ubuntu-latest"})}, {})
    assert ci_wiring.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "1 workflows, 4 jobs, 1 wiring problems" in out
    assert "::error::test.yml/planted runs on pull requests but is not a need of Gate — Required." in out


def test_a_declared_non_gate_passes_and_one_naming_a_need_is_red():
    workflow = _gate_workflow(record={"needs": ["unit"]})
    assert _check({"test.yml": workflow}, {"not_gates": _declared("test.yml/record")}) == []
    assert _check({"test.yml": workflow}, {"not_gates": _declared("test.yml/unit", "test.yml/record")}) == [
        "test.yml/unit is declared a non gate but names no pull request job of test.yml outside the needs of Gate — Required."
    ]


def test_a_stale_non_gate_is_red():
    workflow = _gate_workflow(refresh={"if": "github.event_name == 'push'"})
    keys = ("test.yml/gone", "other.yml/unit", "test.yml/refresh")
    assert _check({"test.yml": workflow}, {"not_gates": _declared(*keys)}) == [
        f"{key} is declared a non gate but names no pull request job of test.yml outside the needs of Gate — Required."
        for key in keys
    ]


@pytest.mark.parametrize(
    ("condition", "runs"),
    [
        ("github.event_name == 'push'", False),
        ("${{ github.event_name == 'schedule' }}", False),
        ("${{github.event_name=='workflow_dispatch'}}", False),
        (" github.event_name == 'push' ", False),
        ("github.event_name != 'pull_request'", False),
        ("${{ failure() && github.event_name != 'pull_request' }}", False),
        ("always() && github.event_name == 'push'", False),
        ("github.event_name == 'pull_request'", True),
        ("github.event_name == 'pull_request_target'", True),
        ("${{ github.event_name == 'merge_group' }}", True),
        ("github.event_name != 'merge_group'", True),
        ("github.event_name != 'push'", True),
        ("github.event_name == 'push' || github.event_name == 'pull_request'", True),
        ("always() && (github.event_name == 'push' || github.event_name == 'merge_group')", True),
        ("github.event_name == 'push' && always() || github.event_name == 'pull_request'", True),
        ("${{\n  github.event_name == 'push'\n}}", False),
        ("always()", True),
        ("${{ always() }}", True),
        ('github.event_name == "push"', False),
        (None, True),
    ],
)
def test_a_conjunct_that_skips_the_pull_request_event_takes_a_job_off_the_path(condition, runs):
    job = {} if condition is None else {"if": condition}
    assert ci_wiring.on_pull_requests(job) is runs
    expected = ["test.yml/extra runs on pull requests but is not a need of Gate — Required."] if runs else []
    assert _check({"test.yml": _gate_workflow(extra=job)}, {}) == expected


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("reversed_operands", [False, True])
@pytest.mark.parametrize(
    ("operator", "event", "runs"),
    [
        ("==", "push", False),
        ("==", "schedule", False),
        ("==", "workflow_dispatch", False),
        ("==", "pull_request", True),
        ("==", "pull_request_target", True),
        ("==", "merge_group", True),
        ("!=", "push", True),
        ("!=", "pull_request", False),
        ("!=", "pull_request_target", True),
        ("!=", "merge_group", True),
    ],
)
def test_event_comparisons_use_either_quote_style_and_operand_order(quote, reversed_operands, operator, event, runs):
    literal = f"{quote}{event}{quote}"
    left, right = (literal, "github.event_name") if reversed_operands else ("github.event_name", literal)
    job = {"if": f"${{{{ always() && {left} {operator} {right} }}}}"}
    assert ci_wiring.on_pull_requests(job) is runs
    expected = ["test.yml/extra runs on pull requests but is not a need of Gate — Required."] if runs else []
    assert _check({"test.yml": _gate_workflow(extra=job)}, {}) == expected


@pytest.mark.parametrize(
    "condition",
    ["github.event_name == 'push\"", "\"push' == github.event_name"],
)
def test_mismatched_event_quotes_keep_the_job_on_the_pull_request_path(condition):
    assert ci_wiring.on_pull_requests({"if": condition}) is True


@pytest.mark.parametrize("field", ["reason", "owner"])
@pytest.mark.parametrize("empty", ["", None])
def test_an_exemption_missing_a_field_is_red(field: str, empty: str | None) -> None:
    workflows = {"test.yml": _gate_workflow(record={}), "smoke.yml": {"on": "pull_request", "jobs": {"smoke": {}}}}
    entry = {**_entry(), field: empty}
    config = {"not_gates": {"test.yml/record": entry}, "outside_gate": {"smoke.yml/smoke": entry}}
    assert _check(workflows, config) == [
        f"test.yml/record is declared without its {field}.",
        f"smoke.yml/smoke is declared without its {field}.",
    ]


def test_declarations_remain_valid_after_their_former_expiry() -> None:
    workflows = {"test.yml": _gate_workflow(record={}, late={})}
    config = {
        "not_gates": {"test.yml/record": _entry(expires="2026-10-08"), "test.yml/late": _entry(expires="2026-10-07")}
    }
    assert _check(workflows, config) == []


@pytest.mark.parametrize("count", [0, 2])
def test_exactly_one_gate_must_exist(count):
    workflows = {f"w{index}.yml": _gate_workflow() for index in range(count)}
    assert _check(workflows, {"not_gates": {"x": _entry(owner="")}}) == [
        "x is declared without its owner.",
        f"{count} jobs are named Gate — Required; exactly one must be.",
    ]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_the_gate_must_run_on_pull_requests_and_the_merge_queue(event):
    workflow = _gate_workflow()
    del workflow[True][event]
    assert _check({"test.yml": workflow}, {}) == [f"test.yml holds Gate — Required but does not run on {event}."]


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("key", ["paths", "paths-ignore", "types"])
def test_a_core_gate_trigger_filter_is_red(event, key):
    workflow = _gate_workflow()
    workflow[True][event][key] = ["docs/**"]
    assert _check({"test.yml": workflow}, {}) == [
        f"test.yml filters its {event} trigger by {key}, so a core gate skips some changes."
    ]


def test_a_merge_group_filter_is_not_a_trigger_github_reads():
    workflow = _gate_workflow()
    workflow[True]["merge_group"] = {"paths": ["docs/**"], "types": ["checks_requested"]}
    assert _check({"test.yml": workflow}, {}) == []


@pytest.mark.parametrize("on", ["pull_request", ["pull_request"], {"pull_request_target": None}, {"merge_group": {}}])
def test_another_pull_request_job_is_red_unless_declared(on):
    jobs = {"smoke": {}, "late": {"needs": "smoke"}, "nightly": {"if": "github.event_name == 'schedule'"}}
    workflows = {"test.yml": _gate_workflow(), "smoke.yml": {"on": on, "jobs": jobs}}
    assert _check(workflows, {}) == [
        f"smoke.yml/{job} runs on pull requests outside test.yml, so it cannot be a need of Gate — Required."
        for job in ("smoke", "late")
    ]
    assert _check(workflows, {"outside_gate": _declared("smoke.yml/smoke")}) == [
        "smoke.yml/late runs on pull requests outside test.yml, so it cannot be a need of Gate — Required."
    ]
    assert _check(workflows, {"outside_gate": _declared("smoke.yml/smoke", "smoke.yml/late")}) == []


def test_workflows_off_the_pull_request_path_need_no_declaration():
    workflows = {
        "test.yml": _gate_workflow(),
        "called.yml": {"on": {"workflow_call": None}, "jobs": {"smoke": {}}},
        "nightly.yml": {"on": {"schedule": [{"cron": "0 0 * * *"}]}, "jobs": {"sweep": {}}},
        "empty.yml": {},
    }
    assert _check(workflows, {}) == []


@pytest.mark.feature("ci-wiring-dequeued-operations")
@pytest.mark.parametrize(
    "on",
    [
        {"pull_request_target": {"types": ["dequeued"]}},
        {"pull_request_target": {"types": ["dequeued"]}, "workflow_run": {"types": ["completed"]}},
    ],
    ids=["dequeued", "dequeued-and-completed"],
)
def test_dequeued_only_recovery_is_outside_pull_request_gates(on: dict[str, Any]) -> None:
    workflows = {
        "test.yml": _gate_workflow(),
        "recovery.yml": {"on": on, "jobs": {"resume": {}}},
    }
    assert ci_wiring.check(workflows, {}) == []


@pytest.mark.feature("ci-wiring-dequeued-operations")
@pytest.mark.parametrize(
    "on",
    [
        "pull_request_target",
        ["pull_request_target"],
        {"pull_request_target": None},
        {"pull_request_target": {}},
        {"pull_request_target": {"types": None}},
        {"pull_request_target": {"types": []}},
        {"pull_request_target": {"types": "dequeued"}},
        {"pull_request_target": {"types": ["opened"]}},
        {"pull_request_target": {"types": ["synchronize"]}},
        {"pull_request_target": {"types": ["reopened"]}},
        {"pull_request_target": {"types": ["dequeued", "opened"]}},
        {"pull_request_target": {"types": ["DEQUEUED"]}},
        {"pull_request_target": {"types": ["unknown"]}},
        {"pull_request_target": {"types": ["dequeued"]}, "pull_request": None},
        {"pull_request_target": {"types": ["dequeued"]}, "merge_group": None},
        {"pull_request": {"types": ["dequeued"]}},
    ],
    ids=[
        "string",
        "list",
        "null",
        "default",
        "null-types",
        "empty",
        "scalar",
        "opened",
        "synchronize",
        "reopened",
        "mixed",
        "case",
        "unknown",
        "ordinary-pull",
        "merge-group",
        "different-event",
    ],
)
def test_other_pull_request_triggers_still_require_a_declaration(on: Any) -> None:
    workflows = {
        "test.yml": _gate_workflow(),
        "recovery.yml": {"on": on, "jobs": {"resume": {}}},
    }
    assert ci_wiring.check(workflows, {}) == [
        "recovery.yml/resume runs on pull requests outside test.yml, so it cannot be a need of Gate — Required."
    ]
    assert ci_wiring.check(workflows, {"outside_gate": _declared("recovery.yml/resume")}) == []


@pytest.mark.feature("ci-wiring-dequeued-operations")
def test_a_dequeued_only_operation_is_not_a_gate_declaration() -> None:
    workflows = {
        "test.yml": _gate_workflow(),
        "recovery.yml": {"on": {"pull_request_target": {"types": ["dequeued"]}}, "jobs": {"resume": {}}},
    }
    assert ci_wiring.check(workflows, {"outside_gate": _declared("recovery.yml/resume")}) == [
        "recovery.yml/resume is declared outside the gate but names no pull request job outside test.yml."
    ]


def test_a_stale_outside_declaration_is_red():
    workflows = {
        "test.yml": _gate_workflow(),
        "called.yml": {"on": "workflow_call", "jobs": {"smoke": {}}},
        "smoke.yml": {"on": "pull_request", "jobs": {"push": {"if": "github.event_name == 'push'"}}},
    }
    keys = ("called.yml/smoke", "gone.yml/job", "smoke.yml/push", "test.yml/unit")
    assert _check(workflows, {"outside_gate": _declared(*keys)}) == [
        f"{key} is declared outside the gate but names no pull request job outside test.yml." for key in keys
    ]


def test_needs_may_be_a_single_job_name():
    workflow = _gate_workflow()
    workflow["jobs"]["gate"]["needs"] = "unit"
    assert _check({"test.yml": workflow}, {"not_gates": _declared("test.yml/lint")}) == []


def test_a_feature_suite_must_be_a_need_of_both_promotion_jobs() -> None:
    push_only = "github.event_name == 'push'"
    workflow = _gate_workflow(
        **{
            "feature-suite": {"if": push_only},
            "promotion-ready": {"if": push_only, "needs": "feature-suite"},
        }
    )
    config = {"not_gates": {"test.yml/lint": _entry()}}
    problems = _check({"test.yml": workflow}, config)
    assert (
        "test.yml/lint is declared a non gate but names no pull request job of test.yml outside the needs of Gate — Required."
        in problems
    )
    assert "test.yml/feature-suite is not a need of promote." in problems
    assert "test.yml/feature-suite is not a need of promotion-ready." not in problems


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


def test_the_tests_workflow_runs_wiring_in_lint_and_brain_smoke_as_gate_needs() -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    needs = set(workflow["jobs"]["gate-required"]["needs"])
    assert {"lint", "brain-smoke"} <= needs
    assert "wiring" not in workflow["jobs"]
    lint = workflow["jobs"]["lint"]
    check = next(
        step for step in lint["steps"] if step.get("name") == "Check every pull request job is a need of Gate Required"
    )
    assert check["run"].splitlines() == [
        'cd "$RUNNER_TEMP/grader"',
        'python -m scripts.ci_wiring --root "$GITHUB_WORKSPACE"',
    ]
    assert check["if"] == "${{ !cancelled() }}"
    assert lint["needs"] in (["reuse"], ["reuse", "admission"], ["reuse", "admission", "lab-verdict"])


def test_a_pull_request_workflow_without_jobs_needs_no_declaration() -> None:
    workflows = {"test.yml": _gate_workflow(), "bare.yml": {"on": "pull_request"}}

    assert _check(workflows, {}) == []


def _lab_gate(replaces: str = "test.yml/lint", reason: str = "why", owner: str = "eng-1@swarm") -> dict[str, str]:
    return {"reason": reason, "owner": owner, "replaces": replaces}


def _main_with_lab(root: Path, workflow: dict[Any, Any], config: dict[str, Any], lab: dict[str, Any]) -> int:
    _write(root, {"test.yml": workflow}, config)
    (root / "delivery.yaml").write_text(yaml.safe_dump({"lab": lab}))
    return ci_wiring.main(["--root", str(root)])


def test_a_planted_undeclared_flag_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = {"lab_gates": {"lab/unit": _lab_gate("test.yml/unit")}}
    assert _main_with_lab(tmp_path, _gate_workflow(), config, {"authoritative": ["lint", "unit"]}) == 1
    assert capsys.readouterr().out == (
        "1 workflows, 3 jobs, 1 wiring problems\n"
        "::error::lab.authoritative names lint, but .github/gate-wiring.json declares no lab/lint lab gate.\n"
    )


def test_every_declared_authoritative_flag_is_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = {"lab_gates": {"lab/lint": _lab_gate(), "lab/unit": _lab_gate("test.yml/unit")}}
    assert _main_with_lab(tmp_path, _gate_workflow(), config, {"authoritative": ["lint", "unit"]}) == 0
    assert capsys.readouterr().out == "1 workflows, 3 jobs, 0 wiring problems\n"


def test_a_planted_undeclared_queue_flag_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = {"lab_gates": {"lab/unit": _lab_gate("test.yml/unit")}}
    lab = {"authoritative": ["unit"], "queue": {"authoritative": ["unit", "lint"]}}
    assert _main_with_lab(tmp_path, _gate_workflow(), config, lab) == 1
    assert capsys.readouterr().out == (
        "1 workflows, 3 jobs, 1 wiring problems\n"
        "::error::lab.queue.authoritative names lint, but .github/gate-wiring.json declares no lab/lint lab gate.\n"
    )


def test_every_declared_queue_flag_is_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = {"lab_gates": {"lab/lint": _lab_gate(), "lab/unit": _lab_gate("test.yml/unit")}}
    lab = {"authoritative": [], "queue": {"authoritative": ["unit", "lint"], "full_suite_entries": 3}}
    assert _main_with_lab(tmp_path, _gate_workflow(), config, lab) == 0
    assert capsys.readouterr().out == "1 workflows, 3 jobs, 0 wiring problems\n"


@pytest.mark.parametrize("field", ["reason", "owner", "replaces"])
def test_a_lab_gate_missing_a_field_is_red(field: str) -> None:
    config = {"lab_gates": {"lab/lint": _lab_gate(**{field: ""})}}
    assert _check({"test.yml": _gate_workflow()}, config) == [f"lab/lint is declared without its {field}."]


@pytest.mark.parametrize("key", ["lint", "lab/"])
def test_a_lab_gate_named_outside_the_lab_prefix_is_red(key: str) -> None:
    config = {"lab_gates": {key: _lab_gate()}}
    assert _check({"test.yml": _gate_workflow()}, config) == [
        f"{key} is declared a lab gate but is not named lab/<gate>."
    ]


def test_a_lab_gate_replacing_no_workflow_job_is_red() -> None:
    config = {"lab_gates": {"lab/lint": _lab_gate("test.yml/gone"), "lab/unit": _lab_gate("other.yml/unit")}}
    assert _check({"test.yml": _gate_workflow()}, config) == [
        "lab/lint replaces test.yml/gone, which is no workflow job.",
        "lab/unit replaces other.yml/unit, which is no workflow job.",
    ]


@pytest.mark.parametrize(
    ("gate_required", "dropped", "code", "out"),
    [
        (True, ("pull_request",), 0, "1 workflows, 3 jobs, 0 wiring problems\n"),
        (
            False,
            ("pull_request",),
            1,
            "1 workflows, 3 jobs, 1 wiring problems\n"
            "::error::test.yml holds Gate — Required but does not run on pull_request.\n",
        ),
        (
            True,
            ("pull_request", "merge_group"),
            1,
            "1 workflows, 3 jobs, 1 wiring problems\n"
            "::error::test.yml holds Gate — Required but does not run on merge_group.\n",
        ),
    ],
    ids=["lab-gate-lifts-pull-request", "actions-gate-needs-pull-request", "lab-gate-keeps-merge-group"],
)
def test_the_lab_gate_required_flag_lifts_only_the_pull_request_trigger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    gate_required: bool,
    dropped: tuple[str, ...],
    code: int,
    out: str,
) -> None:
    workflow = _gate_workflow()
    for event in dropped:
        del workflow[True][event]
    lab = {"authoritative": [], "gate_required": gate_required}
    assert _main_with_lab(tmp_path, workflow, {}, lab) == code
    assert capsys.readouterr().out == out
