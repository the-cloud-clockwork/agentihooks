import json

import pytest

from scripts.swarm_ledger import ledger, ledger_phase_cli, ledger_phases, new_ledger
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "plan-phases-proof"


@pytest.fixture(autouse=True)
def phase_ledger_dir(ledger_dir, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", ledger_dir)


def make_ledger():
    content = {"title": "Demo", "phases": [{"title": "First"}]}
    html, state = core.paths(SLUG)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765))
    state.unlink(missing_ok=True)
    return core.sync(SLUG)[0]


def append(phases):
    op = {"op": "phase_append", "id": "append-plan", "by": "master", "phases": phases}
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


def served(slug, ops=None):
    state, rejected = core.sync(slug, ops=ops)
    return {**state, "rejected": rejected}


def cli(monkeypatch, *argv):
    monkeypatch.setattr(ledger, "call", served)
    monkeypatch.setattr(ledger, "resource", lambda slug, path, collection=False: served(slug)[path])
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "master", *argv])
    getattr(ledger, f"cmd_{args.command.replace('-', '_')}")(args)


def plan_file(tmp_path, phases):
    path = tmp_path / "phases.json"
    path.write_text(json.dumps({"phases": phases}), encoding="utf-8")
    return str(path)


def test_a_plan_phase_is_appended_auto_unless_it_names_manual_and_only_manual_waits_in_review():
    make_ledger()
    state, rejected = append(
        [
            {"phase": "p2", "title": "Second", "description": "Intent two", "planning": "manual"},
            {"phase": "p3", "title": "Third", "depends_on": ["p2"], "release": True},
        ]
    )
    assert rejected == []
    first, second, third = state["phases"]
    assert first["id"] == "p1" and "review" not in first
    assert (second["id"], second["title"], second["description"], second["planning"]) == (
        "p2",
        "Second",
        "Intent two",
        "manual",
    )
    assert (third["id"], third["depends_on"], third["planning"], third["release"]) == ("p3", ["p2"], "auto", True)
    assert third["description"] == ""
    at = second["review"]["at"]
    assert type(at) is int and at > 0
    assert second["review"] == {"state": "pending", "by": "master", "at": at, "rounds": 0, "note": ""}
    assert "review" not in third
    for phase in (second, third):
        assert phase["done"] is False and phase["comments"] == []
    assert [(e["by"], e["kind"], e["target"], e["text"]) for e in state["_meta"]["events"][-2:]] == [
        ("master", "added", "phases/p2", "Second"),
        ("master", "added", "phases/p3", "Third"),
    ]


def test_a_taken_phase_id_refuses_the_whole_plan_and_changes_nothing():
    before = make_ledger()
    state, rejected = append([{"phase": "p9", "title": "Fresh"}, {"phase": "p1", "title": "Taken"}])
    assert rejected == ["append-plan"]
    assert state["phases"] == before["phases"]
    assert state["_meta"]["events"] == before["_meta"]["events"]
    assert "phase id p1 is already taken" in state["_meta"]["warnings"]


def test_an_invalid_graph_refuses_the_whole_plan():
    before = make_ledger()
    state, rejected = append([{"phase": "p2", "title": "Second", "depends_on": ["p7"]}])
    assert rejected == ["append-plan"]
    assert state["phases"] == before["phases"]
    assert any("depends on unknown phases: p7" in w for w in state["_meta"]["warnings"])


@pytest.mark.parametrize(
    "phases,message",
    [
        ([], "phase_append needs a list of phases"),
        ("p2", "phase_append needs a list of phases"),
        ([{"phase": "p2", "title": "A"}, {"phase": "p2", "title": "B"}], "phase p2 appears twice in the plan"),
        ([{"phase": "p2", "title": " "}], "phase_add needs a title"),
        ([{"phase": "2", "title": "A"}], "phase_add needs a phase id"),
        ([{"phase": "p2", "title": "A", "planning": "automatic"}], "planning must be manual or auto"),
        ([{"phase": "p2", "title": "A", "extra": 1}], "phase fields may set only"),
        (["p2"], "each appended phase is an object"),
    ],
)
def test_phase_append_check_runs_the_phase_add_checks(phases, message):
    with pytest.raises(ValueError) as refused:
        ledger_phases.check({"op": "phase_append", "id": "x", "by": "master", "phases": phases})
    assert str(refused.value).startswith(message)


def test_plan_entries_take_the_next_free_ids_and_resolve_positions():
    plan = {
        "phases": [
            {"title": "Second", "description": "Two", "depends_on": [3]},
            {"id": "deploy", "title": "Deploy", "depends_on": [1, "p1"], "release": True},
            {"title": "Third", "depends_on": [2], "planning": "manual"},
        ]
    }
    assert ledger_phase_cli.append_phases(plan, ["p1", "p4", "deploy-old", "pilot"]) == [
        {"phase": "p5", "title": "Second", "description": "Two", "depends_on": ["p6"], "planning": "auto"},
        {
            "phase": "deploy",
            "title": "Deploy",
            "description": "",
            "depends_on": ["p5", "p1"],
            "planning": "auto",
            "release": True,
        },
        {"phase": "p6", "title": "Third", "description": "", "depends_on": ["deploy"], "planning": "manual"},
    ]


@pytest.mark.parametrize(
    "taken,phases,ids",
    [
        (["intro"], [{"title": "A"}], ["p1"]),
        (["p1"], [{"id": "p2", "title": "A"}, {"title": "B"}], ["p2", "p3"]),
        (["p1"], [{"id": "p2", "title": "A"}, {"id": "p3", "title": "B"}, {"title": "C"}], ["p2", "p3", "p4"]),
    ],
)
def test_minted_ids_skip_ids_the_plan_names(taken, phases, ids):
    assert [phase["phase"] for phase in ledger_phase_cli.append_phases({"phases": phases}, taken)] == ids


def test_a_plan_phase_without_a_title_keeps_an_empty_title_for_the_check():
    [phase] = ledger_phase_cli.append_phases({"phases": [{}]}, [])
    assert phase["title"] == ""


@pytest.mark.parametrize(
    "plan,message",
    [
        ({"phases": []}, "the plan file needs a nonempty phases list"),
        ({"title": "x"}, "the plan file needs a nonempty phases list"),
        ([], "the plan file needs a nonempty phases list"),
        ({"phases": [{"title": "A", "depends_on": [2]}]}, "phase 1 depends on position 2, outside the plan"),
        ({"phases": [{"title": "A", "depends_on": [0]}]}, "phase 1 depends on position 0, outside the plan"),
        ({"phases": [{"title": "A", "depends_on": "p1"}]}, "phase 1 depends_on must be a list"),
        ({"phases": ["A"]}, "each plan phase is an object"),
    ],
)
def test_plan_file_shape_is_refused_with_its_reason(plan, message):
    with pytest.raises(SystemExit) as refused:
        ledger_phase_cli.append_phases(plan, ["p1"])
    assert refused.value.code == message


def test_plan_phases_command_appends_a_two_phase_plan_in_review(tmp_path, monkeypatch, capsys):
    make_ledger()
    path = plan_file(tmp_path, [{"title": "Second", "planning": "manual"}, {"title": "Third", "depends_on": [1]}])
    cli(monkeypatch, "plan", "phases", path)
    assert json.loads(capsys.readouterr().out) == {"appended": ["p2", "p3"], "planning": {"p2": "manual", "p3": "auto"}}
    state = core.sync(SLUG)[0]
    assert [(p["id"], p.get("planning"), (p.get("review") or {}).get("state")) for p in state["phases"]] == [
        ("p1", None, None),
        ("p2", "manual", "pending"),
        ("p3", "auto", None),
    ]
    assert state["phases"][2]["depends_on"] == ["p2"]


def test_plan_phases_command_refuses_a_taken_id_and_changes_nothing(tmp_path, monkeypatch):
    before = make_ledger()
    path = plan_file(tmp_path, [{"title": "Second"}, {"id": "p1", "title": "Again"}])
    with pytest.raises(SystemExit, match="phase id p1 is already taken"):
        cli(monkeypatch, "plan", "phases", path)
    assert core.sync(SLUG)[0]["phases"] == before["phases"]


def test_plan_takes_only_the_phases_action():
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["--slug", SLUG, "--as", "master", "plan", "tasks", "plan.json"])


def test_plan_phases_command_reads_the_plan_as_utf8(tmp_path, monkeypatch, capsys):
    make_ledger()
    path = tmp_path / "phases.json"
    path.write_bytes(json.dumps({"phases": [{"title": "Café"}]}, ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr("locale.getpreferredencoding", lambda *_: "ascii")
    cli(monkeypatch, "plan", "phases", str(path))
    assert core.sync(SLUG)[0]["phases"][1]["title"] == "Café"
