import copy
import json
import subprocess
from unittest.mock import patch

import pytest

from scripts.swarm_ledger import ledger, ledger_phase_cli, ledger_phases, new_ledger
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.ledger_page import page_source

SLUG = "phase-fields-proof"


@pytest.fixture(autouse=True)
def phase_ledger_dir(ledger_dir, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", ledger_dir)


def make_ledger(phases=None):
    content = {"title": "Demo", "phases": phases or [{"title": "First"}]}
    html, state = core.paths(SLUG)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765))
    state.unlink(missing_ok=True)
    return core.sync(SLUG)[0]


def operation(kind, **fields):
    return {"op": kind, "id": "phase-operation", "by": "engineer", **fields}


def apply(kind, **fields):
    op = operation(kind, **fields)
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


def test_phase_add_and_update_store_fields():
    make_ledger()
    state, rejected = apply("phase_add", phase="p2", title="Second", depends_on=["p1"], planning="auto", release=True)
    assert rejected == []
    assert state["phases"][1] == {
        "id": "p2",
        "title": "Second",
        "description": "",
        "done": False,
        "comments": [],
        "depends_on": ["p1"],
        "planning": "auto",
        "release": True,
    }
    state, rejected = apply(
        "phase_update",
        item="phases/p2",
        fields={"title": "Changed", "description": "Intent", "planning": "manual", "release": False, "depends_on": []},
    )
    assert rejected == []
    assert state["phases"][1]["title"] == "Changed"
    assert state["phases"][1]["description"] == "Intent"
    assert state["phases"][1]["planning"] == "manual"
    assert state["phases"][1]["release"] is False
    assert state["phases"][1]["depends_on"] == []


def test_a_phase_added_without_a_planning_value_is_planned_automatically():
    make_ledger()
    state, rejected = apply("phase_add", phase="p2", title="Second")
    assert rejected == []
    assert state["phases"][1]["planning"] == "auto"
    state, rejected = apply("phase_add", phase="p3", title="Third", planning="manual")
    assert rejected == []
    assert state["phases"][2]["planning"] == "manual"


@pytest.mark.parametrize(
    "field,value",
    [
        ("depends_on", "p1"),
        ("depends_on", [1]),
        ("depends_on", [""]),
        ("planning", "automatic"),
        ("planning", 1),
        ("release", 1),
        ("release", "true"),
        ("title", 1),
        ("description", False),
        ("review", {"state": "approved"}),
    ],
)
def test_phase_fields_are_validated(field, value):
    with pytest.raises(ValueError):
        core.check_op(operation("phase_update", item="phases/p1", fields={field: value}))
    with pytest.raises(ValueError):
        core.check_op(
            operation("phase_add", phase="p2", title="Second", **{field: value})
            if field != "title"
            else operation("phase_add", phase="p2", title=value)
        )


def test_unknown_dependency_and_cycle_are_refused_without_changes():
    before = make_ledger()
    state, rejected = apply("phase_add", phase="p2", title="Second", depends_on=["missing"])
    assert rejected == ["phase-operation"]
    assert state["phases"] == before["phases"]
    assert "missing" in " ".join(state["_meta"]["warnings"])
    apply("phase_add", phase="p2", title="Second", depends_on=["p1"])
    state, rejected = apply("phase_update", item="phases/p1", fields={"depends_on": ["p2"]})
    assert rejected == ["phase-operation"]
    assert "p1 -> p2 -> p1" in " ".join(state["_meta"]["warnings"])
    assert "depends_on" not in state["phases"][0]
    state, rejected = apply("phase_update", item="phases/absent", fields={"planning": "auto"})
    assert rejected == ["phase-operation"]


def test_phase_review_op_changes_only_the_review_field():
    before = make_ledger()["phases"][0]
    state, rejected = apply("phase_review", item="phases/p1", state="approved", rounds=2, note="Reviewed")
    assert rejected == []
    phase = state["phases"][0]
    review = phase["review"]
    assert {k: v for k, v in phase.items() if k != "review"} == before
    assert review == {
        "state": "approved",
        "by": "engineer",
        "at": state["_meta"]["updated_at"],
        "rounds": 2,
        "note": "Reviewed",
    }


def test_content_builds_phase_fields_and_rejects_bad_graph():
    content = {
        "title": "Demo",
        "phases": [{"title": "First"}, {"title": "Second", "depends_on": ["p1"], "planning": "auto", "release": True}],
    }
    assert new_ledger.check(content) == []
    phase = new_ledger.build_doc(content)["phases"][1]
    assert (phase["depends_on"], phase["planning"], phase["release"]) == (["p1"], "auto", True)
    bad = copy.deepcopy(content)
    bad["phases"][0]["depends_on"] = ["p2"]
    assert "p1 -> p2 -> p1" in " ".join(new_ledger.check(bad))
    bad["phases"][0]["depends_on"] = ["missing"]
    assert "missing" in " ".join(new_ledger.check(bad))


def test_phase_cli_preserves_old_command_and_sends_new_ops(capsys):
    sent = []
    with patch.object(
        ledger,
        "send",
        lambda args, kind, **fields: (
            sent.append((kind, fields))
            if args.slug == SLUG and args.name == "engineer"
            else pytest.fail("lost caller identity")
        ),
    ):
        for command in (
            ["phase", "p1", "done"],
            ["phase", "add", "p2", "Second", "--depends-on", "p1", "--planning", "auto", "--release"],
            ["phase", "set", "p2", "depends_on=p1", "planning=manual", "release=false"],
        ):
            args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "engineer", *command])
            ledger.cmd_phase(args)
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert outputs == [
        {"phase": "p1", "state": "done"},
        {
            "phase": "p2",
            "title": "Second",
            "description": "",
            "depends_on": ["p1"],
            "planning": "auto",
            "release": True,
        },
        {"item": "phases/p2", "fields": {"depends_on": ["p1"], "planning": "manual", "release": False}},
    ]
    assert sent == [
        ("set", {"path": "phases/p1/done", "value": True}),
        (
            "phase_add",
            {
                "phase": "p2",
                "title": "Second",
                "description": "",
                "depends_on": ["p1"],
                "planning": "auto",
                "release": True,
            },
        ),
        (
            "phase_update",
            {"item": "phases/p2", "fields": {"depends_on": ["p1"], "planning": "manual", "release": False}},
        ),
    ]


def test_page_round_trip_preserves_phase_fields_and_old_phases():
    phases = [
        {"id": "p1", "title": "First", "description": "", "done": False, "out_of_scope": False, "comments": []},
        {
            "id": "p2",
            "title": "Second",
            "description": "",
            "done": False,
            "out_of_scope": False,
            "depends_on": ["p1"],
            "planning": "auto",
            "release": True,
            "review": {"state": "approved"},
            "comments": [],
        },
    ]
    source = page_source()
    functions = "\n".join(
        "function " + name + "(" + source.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"
        for name in ("entries", "withDefaults")
    )
    script = (
        functions
        + "\nprocess.stdout.write(JSON.stringify(withDefaults("
        + json.dumps({"phases": phases})
        + ").phases));"
    )
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == phases


@pytest.mark.parametrize(
    "fields",
    [
        {"by": ""},
        {"by": 1},
        {"by": "bad/name"},
        {"phase": 1},
        {"phase": "bad/name"},
        {"title": ""},
        {"title": " "},
    ],
)
def test_phase_add_rejects_invalid_identity(fields):
    with pytest.raises(ValueError):
        core.check_op({**operation("phase_add", phase="p2", title="Second"), **fields})


@pytest.mark.parametrize(
    "fields",
    [
        {"item": "tasks/p1"},
        {"item": "phases/"},
        {"fields": {}},
        {"fields": []},
        {"fields": {"done": True}},
    ],
)
def test_phase_update_rejects_invalid_target_or_fields(fields):
    with pytest.raises(ValueError):
        core.check_op({**operation("phase_update", item="phases/p1", fields={"planning": "auto"}), **fields})


@pytest.mark.parametrize(
    "fields",
    [
        {"state": "done"},
        {"rounds": -1},
        {"rounds": True},
        {"rounds": 1.5},
        {"note": 1},
        {"escalated": 1},
        {"title": "Changed"},
    ],
)
def test_review_validation(fields):
    with pytest.raises(ValueError):
        core.check_op({**operation("phase_review", item="phases/p1", state="pending"), **fields})


def test_review_defaults_operator_and_escalation():
    make_ledger()
    op = {**operation("phase_review", item="phases/p1", state="pending", escalated=True), "by": "operator"}
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    assert rejected == []
    assert state["phases"][0]["review"] == {
        "state": "pending",
        "by": "operator",
        "at": state["_meta"]["updated_at"],
        "rounds": 0,
        "note": "",
        "escalated": True,
    }
    state, rejected = apply("phase_review", item="phases/missing", state="approved")
    assert rejected == ["phase-operation"]


def test_phase_add_retry_and_no_change_update_are_idempotent():
    make_ledger()
    state, _ = apply("phase_add", phase="p2", title="Second")
    assert state["phases"][1] == {
        "id": "p2",
        "title": "Second",
        "description": "",
        "done": False,
        "comments": [],
        "planning": "auto",
    }
    events = state["_meta"]["events"]
    state, rejected = apply("phase_add", phase="p2", title="Another")
    assert rejected == []
    assert state["phases"][1]["title"] == "Second"
    assert state["_meta"]["events"] == events
    state, rejected = apply("phase_update", item="phases/p2", fields={"title": "Second"})
    assert rejected == []
    assert state["_meta"]["events"] == events


def test_dependency_diamond_is_valid_and_self_cycle_is_refused():
    phases = [{"id": "p1"}, {"id": "p2", "depends_on": ["p1"]}, {"id": "p3", "depends_on": ["p1", "p2"]}]
    ledger_phases.validate(phases)
    with pytest.raises(ValueError, match="p1 -> p1"):
        ledger_phases.validate([{"id": "p1", "depends_on": ["p1"]}])


@pytest.mark.parametrize("value", ["yes", "1", "True"])
def test_cli_release_requires_boolean(value):
    args = ledger.build_parser().parse_args(["phase", "set", "p1", f"release={value}"])
    with pytest.raises(SystemExit) as error:
        ledger_phase_cli.operation(args)
    assert str(error.value) == "release must be true or false"


def test_cli_set_requires_pairs_and_splits_trimmed_dependencies():
    args = ledger.build_parser().parse_args(["phase", "set", "p1", "bad"])
    with pytest.raises(SystemExit) as error:
        ledger_phase_cli.operation(args)
    assert str(error.value) == "phase set takes FIELD=VALUE pairs"
    args = ledger.build_parser().parse_args(["phase", "set", "p1", "depends_on= p2,, p3 ", "release=true"])
    assert ledger_phase_cli.operation(args) == (
        "phase_update",
        {"item": "phases/p1", "fields": {"depends_on": ["p2", "p3"], "release": True}},
    )


@pytest.mark.parametrize("command", [["phase", "p1", "bad"], ["phase", "p1", "done", "extra"]])
def test_cli_old_command_rejects_invalid_state(command):
    args = ledger.build_parser().parse_args(command)
    with pytest.raises(SystemExit) as error:
        ledger.cmd_phase(args)
    assert str(error.value) == "phase takes ID done|open, add ID TITLE, or set ID FIELD=VALUE"


@pytest.mark.parametrize("fields", [{"depends_on": "p1"}, {"depends_on": [1]}, {"planning": "bad"}, {"release": 1}])
def test_content_and_seed_reject_invalid_fields(fields):
    content = {"title": "Demo", "phases": [{"title": "First", **fields}]}
    assert new_ledger.check(content)
    with pytest.raises(ValueError):
        core.validate({"phases": [{"id": "p1", **fields}]})


def test_phase_cli_refusal_names_the_dependency_chain():
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "engineer", "phase", "set", "p1", "depends_on=p2"])
    reply = {"rejected": ["phase-operation"], "_meta": {"warnings": ["phase dependency cycle: p1 -> p2 -> p1"]}}
    with patch.object(ledger, "call", return_value=reply):
        with pytest.raises(SystemExit, match="p1 -> p2 -> p1"):
            ledger.cmd_phase(args)


def test_cli_add_defaults_auto_planning_and_no_release():
    args = ledger.build_parser().parse_args(["phase", "add", "p2", "Second"])
    assert ledger_phase_cli.operation(args) == (
        "phase_add",
        {"phase": "p2", "title": "Second", "description": "", "depends_on": [], "planning": "auto", "release": False},
    )
    args = ledger.build_parser().parse_args(
        ["phase", "add", "p2", "Second", "--planning", "manual", "--description", "Intent"]
    )
    assert ledger_phase_cli.operation(args)[1]["description"] == "Intent"
    assert args.planning == "manual"
    with pytest.raises(SystemExit) as error:
        ledger.build_parser().parse_args(["phase", "add", "p2", "Second", "--planning", "bad"])
    assert error.value.code == 2


def test_cli_reopens_old_phase():
    args = ledger.build_parser().parse_args(["phase", "p1", "open"])
    with patch.object(ledger, "send") as sent:
        ledger.cmd_phase(args)
    sent.assert_called_once_with(args, "set", path="phases/p1/done", value=False)


def test_cli_multiline_description_and_multiword_title():
    args = ledger.build_parser().parse_args(["phase", "add", "p2", "Second", "phase"])
    assert ledger_phase_cli.operation(args)[1]["title"] == "Second phase"
    args = ledger.build_parser().parse_args(["phase", "set", "p2", "description=first=second"])
    assert ledger_phase_cli.operation(args)[1]["fields"] == {"description": "first=second"}


def test_document_without_phase_list_is_valid():
    core.validate({})


def test_content_preserves_titles_descriptions_and_optional_fields():
    content = {
        "title": "Demo",
        "phases": [
            {"title": "First", "description": "First intent", "planning": "manual"},
            {"title": "Second", "description": "Next intent", "release": False},
        ],
    }
    assert new_ledger.check(content) == []
    assert new_ledger.build_doc(content)["phases"] == [
        {
            "id": "p1",
            "title": "First",
            "description": "First intent",
            "done": False,
            "comments": [],
            "planning": "manual",
        },
        {"id": "p2", "title": "Second", "description": "Next intent", "done": False, "comments": [], "release": False},
    ]


@pytest.mark.parametrize(
    "kind,fields,message",
    [
        ("phase_update", {"fields": {"title": 1}}, "title must be a string"),
        ("phase_update", {"fields": {"description": 1}}, "description must be a string"),
        ("phase_update", {"fields": {"depends_on": "p1"}}, "depends_on must be a list of nonempty phase ids"),
        ("phase_update", {"fields": {"planning": "bad"}}, "planning must be manual or auto"),
        ("phase_update", {"fields": {"release": 1}}, "release must be a boolean"),
        ("phase_add", {"by": ""}, "phase ops need by, an agent name or operator"),
        ("phase_add", {"phase": ""}, "phase_add needs a phase id"),
        ("phase_add", {"title": ""}, "phase_add needs a title"),
        ("phase_update", {"item": ""}, "phase ops need item phases/<id>"),
        (
            "phase_update",
            {"fields": {"review": {}}},
            "phase fields may set only ('title', 'description', 'depends_on', 'planning', 'release', 'plan_url')",
        ),
        ("phase_review", {"title": "bad"}, "phase_review writes only the review record"),
        ("phase_review", {"state": "bad"}, "review state must be one of ('pending', 'approved', 'sent_back')"),
        ("phase_review", {"rounds": -1}, "review rounds must be a nonnegative integer"),
        ("phase_review", {"note": 1}, "review note must be a string"),
        ("phase_review", {"escalated": 1}, "review escalated must be a boolean"),
    ],
)
def test_phase_diagnostics_explain_the_refusal(kind, fields, message):
    values = {"phase": "p2", "title": "Second"} if kind == "phase_add" else {"item": "phases/p1"}
    if kind == "phase_review":
        values["state"] = "pending"
    with pytest.raises(ValueError) as error:
        core.check_op(operation(kind, **{**values, **fields}))
    assert str(error.value) == message


def test_unknown_dependencies_are_named_together():
    make_ledger()
    state, rejected = apply("phase_add", phase="p2", title="Second", depends_on=["absent", "missing"])
    assert rejected == ["phase-operation"]
    assert "phase p2 depends on unknown phases: absent, missing" in state["_meta"]["warnings"]


def test_phase_operations_record_actor_events_and_stamps():
    make_ledger()
    state, _ = apply("phase_add", phase="p2", title="Second")
    assert state["_meta"]["events"][-1] == {
        "rev": state["_meta"]["rev"],
        "at": state["_meta"]["updated_at"],
        "by": "engineer",
        "kind": "added",
        "target": "phases/p2",
        "text": "Second",
    }
    state, _ = apply("phase_update", item="phases/p2", fields={"planning": "manual"})
    assert state["_meta"]["events"][-1] == {
        "rev": state["_meta"]["rev"],
        "at": state["_meta"]["updated_at"],
        "by": "engineer",
        "kind": "planning changed",
        "target": "phases/p2",
    }
    assert state["_meta"]["stamps"]["phases/p2/planning"] == {
        "at": state["_meta"]["updated_at"],
        "rev": state["_meta"]["rev"],
        "by": "engineer",
    }


def test_large_ordered_phase_graph_validates_without_revisiting():
    phases = [{"id": f"p{n}", "depends_on": [f"p{n - 1}"] if n else []} for n in range(1100)]
    ledger_phases.validate(phases)


@pytest.mark.parametrize(
    "reply",
    [
        {"rejected": ["phase-operation"]},
        {"rejected": ["phase-operation"], "_meta": {}},
        {"rejected": ["phase-operation"], "_meta": {"warnings": []}},
    ],
)
def test_phase_cli_reports_refusal_without_warning_details(reply):
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "engineer", "phase", "set", "p1", "planning=auto"])
    with patch.object(ledger, "call", return_value=reply):
        with pytest.raises(SystemExit) as error:
            ledger.cmd_phase(args)
    assert str(error.value) == "rejected: ['phase-operation']"


def test_phase_cli_reports_all_refusal_details():
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "engineer", "phase", "set", "p1", "planning=auto"])
    reply = {"rejected": ["phase-operation"], "_meta": {"warnings": ["First refusal", "Second refusal"]}}
    with patch.object(ledger, "call", return_value=reply):
        with pytest.raises(SystemExit) as error:
            ledger.cmd_phase(args)
    assert str(error.value) == "First refusal; Second refusal"


def test_content_without_phases_reports_the_missing_phase():
    assert new_ledger.check({"title": "Demo"}) == ["no phases"]


def test_phase_content_missing_description_defaults_to_empty():
    assert new_ledger.build_doc({"title": "Demo", "phases": [{"title": "First"}]})["phases"] == [
        {"id": "p1", "title": "First", "description": "", "done": False, "comments": []}
    ]


@pytest.mark.parametrize("phase_id", ["add", "set"])
@pytest.mark.parametrize("state", ["done", "open"])
def test_legacy_phase_ids_matching_new_commands_keep_state_syntax(phase_id, state):
    args = ledger.build_parser().parse_args(["phase", phase_id, state])
    with patch.object(ledger, "send") as sent:
        ledger.cmd_phase(args)
    sent.assert_called_once_with(args, "set", path=f"phases/{phase_id}/done", value=state == "done")


def test_new_phase_add_can_use_a_state_word_as_its_id():
    args = ledger.build_parser().parse_args(["phase", "add", "done", "Final"])
    with patch.object(ledger, "send") as sent:
        ledger.cmd_phase(args)
    sent.assert_called_once_with(
        args, "phase_add", phase="done", title="Final", description="", depends_on=[], planning="auto", release=False
    )
