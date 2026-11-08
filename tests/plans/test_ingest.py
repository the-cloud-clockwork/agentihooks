import json

import pytest

from scripts.plans import cli, ingest
from scripts.plans.normalize import normalize
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger, ledger_artifacts, ledger_link, ledger_publish
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import test_plan_kind

plan_ledger = test_plan_kind.plan_ledger

MARKED = (
    "# Ingest proof\n\n"
    "## Alpha\n<!-- phase: id=alpha planning=auto -->\nAlpha prose.\n\n"
    "<!-- slice: a1 -->\n### First slice\nDo it.\n\n"
    "## Beta\n<!-- phase: id=beta depends=alpha planning=manual -->\nBeta prose.\n"
)
SEAT = {"AGENTIHOOKS_SWARM": "plan-proof", "AGENTIHOOKS_AGENT_NAME": "planner"}


class NoSwarm:
    def config(self, slug):
        raise SwarmError(f"no swarm {slug}")


@pytest.fixture
def served(plan_ledger, monkeypatch):
    sent = []

    def call(slug, ops=None, service=False):
        state, rejected = core.sync(slug, ops=ops)
        sent.append([op["op"] for op in ops or []])
        return {**state, "applied": [op["id"] for op in ops or [] if op["id"] not in rejected], "rejected": rejected}

    monkeypatch.setattr(ledger, "call", call)
    monkeypatch.setattr(ledger, "resource", lambda slug, key, collection=True: core.sync(slug)[0].get(key, []))
    monkeypatch.setattr(
        ledger,
        "upload_artifact",
        lambda slug, name, path, request: ledger_artifacts.store(slug, "plan.md", open(path, "rb").read()),
    )
    monkeypatch.setattr(ledger_publish, "has_issues", lambda repo, run=None: False)
    monkeypatch.setattr(ledger_link, "remote", lambda environ=None, slug="": False)
    monkeypatch.setattr("scripts.swarm.store.connect", lambda environ=None: NoSwarm())
    notes = []
    monkeypatch.setattr("scripts.inbox.cli.main", lambda argv: notes.append(argv) or 0)
    return {"slug": plan_ledger, "sent": sent, "notes": notes}


def run(tmp_path, capsys, *extra, text=MARKED, env=SEAT):
    path = tmp_path / "plan.md"
    path.write_text(text, encoding="utf-8")
    assert cli.main(["ingest", str(path), "--ledger", "plan-proof", *extra], env) == 0
    return json.loads(capsys.readouterr().out)


def test_author_is_the_bound_seat_or_the_operator_on_the_host():
    assert ingest.author("planner", "plan-proof", "plan-proof", remote=True) == ingest.Author("planner", True)
    assert ingest.author("", "", "plan-proof", remote=False) == ingest.Author("operator", False)
    assert ingest.author("planner", "", "plan-proof", remote=False) == ingest.Author("operator", False)


def test_author_refuses_another_swarms_seat_and_an_unbound_remote_session():
    with pytest.raises(ingest.IngestError) as raised:
        ingest.author("planner", "other", "plan-proof", remote=False)
    assert str(raised.value) == "a seat of swarm other cannot ingest a plan into ledger plan-proof"
    with pytest.raises(ingest.IngestError) as raised:
        ingest.author("", "", "plan-proof", remote=True)
    assert str(raised.value) == (
        "an unbound session cannot ingest into a remote ledger; the operator credential stays on its host"
    )


def test_batch_links_every_phase_and_adds_a_slice_per_anchor():
    plan = normalize(MARKED, "plan.md")
    phases = ingest.entries(plan, ["p1"])
    links = ingest.Links("plan-abc", {"id": "f.md"}, "https://l/a", "https://l/u", "t1", {"harness": "claude"})
    ops = ingest.batch(plan, phases, links, "operator")
    assert [op["op"] for op in ops] == ["artifact_add", "plan_add", "phase_append", "phase_append", "slice_add"]
    assert [op["id"] for op in ops] == [f"{op['op']}-plan-abc-{n}" for n, op in enumerate(ops)]
    assert {op["by"] for op in ops} == {"operator"}
    assert ops[0] == {
        "op": "artifact_add",
        "task": "t1",
        "title": "Ingest proof",
        "file": {"id": "f.md"},
        "plan": True,
        "id": "artifact_add-plan-abc-0",
        "by": "operator",
    }
    assert ops[1]["source"] == {"harness": "claude"}
    assert (ops[1]["plan"], ops[1]["artifact"], ops[1]["url"]) == ("plan-abc", "https://l/a", "https://l/u")
    [alpha] = ops[2]["phases"]
    assert alpha["phase"] == "alpha"
    assert (alpha["plan"], alpha["plan_url"]) == ("plans/plan-abc", "https://l/u")
    assert alpha["plan_ref"] == {"artifact": "https://l/a", "lines": "3-10"}
    assert ops[3]["phases"][0]["depends_on"] == ["alpha"]
    assert (ops[4]["phase"], ops[4]["anchor"]) == ("phases/alpha", "a1")


def test_batch_adds_a_not_duplicate_phase_with_phase_add_and_omits_an_empty_source():
    text = MARKED.replace("planning=manual", 'planning=manual not_duplicate="a different build"')
    plan = normalize(text, "plan.md")
    ops = ingest.batch(plan, ingest.entries(plan, []), ingest.Links("plan-abc", {}, "https://l/a", "https://l/u"), "x")
    assert "source" not in ops[1]
    assert ops[3]["op"] == "phase_add"
    assert (ops[3]["phase"], ops[3]["not_duplicate"]) == ("beta", "a different build")


def test_batch_refuses_more_than_the_operation_cap():
    text = "# Big\n\n## Only\n<!-- phase: id=only -->\n\n" + "".join(
        f"<!-- slice: s{n} -->\n### Slice {n}\nx\n\n" for n in range(99)
    )
    plan = normalize(text, "plan.md")
    with pytest.raises(ingest.IngestError) as raised:
        ingest.batch(plan, ingest.entries(plan, []), ingest.Links("plan-abc", {}, "https://l/a", "https://l/u"), "x")
    assert str(raised.value) == "the plan needs 102 operations; one ingest holds at most 100"


def test_planning_override_applies_to_every_phase():
    plan = normalize(MARKED, "plan.md")
    assert [entry["planning"] for entry in ingest.entries(plan, [], "manual")] == ["manual", "manual"]
    assert [entry["planning"] for entry in ingest.entries(plan, [])] == ["auto", "manual"]


def test_dry_run_writes_nothing(served, tmp_path, capsys):
    before = core.sync(served["slug"])[0]
    result = run(tmp_path, capsys, "--dry-run")
    assert result == {
        "dry_run": True,
        "plan": ingest.plan_of(MARKED),
        "by": "planner",
        "phases": ["alpha", "beta"],
        "ops": 5,
    }
    after = core.sync(served["slug"])[0]
    assert (after["phases"], after.get("plans", []), after.get("artifacts", [])) == (
        before["phases"],
        before.get("plans", []),
        before.get("artifacts", []),
    )
    assert served["sent"] == [[]] * len(served["sent"])


def test_apply_links_phases_and_slices_then_a_rerun_is_already_ingested(served, tmp_path, capsys):
    plan = ingest.plan_of(MARKED)
    result = run(tmp_path, capsys)
    url = result.pop("plan_url")
    assert result == {
        "plan": plan,
        "phases": ["alpha", "beta"],
        "lifecycle": {"alpha": "to_plan", "beta": "waiting"},
        "swarm": "absent",
        "ingested": True,
        "published_to": "artifact",
    }
    state = core.sync(served["slug"])[0]
    assert [row["id"] for row in state["plans"]] == [plan]
    assert url == state["plans"][0]["url"]
    assert [row["anchor"] for row in state["slices"]] == ["a1"]
    assert {p["id"]: p.get("plan") for p in state["phases"]} == {
        "p1": None,
        "alpha": f"plans/{plan}",
        "beta": f"plans/{plan}",
    }
    assert served["notes"] == [
        ["send", "master@plan-proof", "--fyi", "planner ingested the plan Ingest proof as 2 phases."]
    ]
    again = run(tmp_path, capsys)
    assert again == {
        "plan": plan,
        "phases": ["alpha", "beta"],
        "lifecycle": {"alpha": "to_plan", "beta": "waiting"},
        "swarm": "absent",
        "ingested": False,
        "note": "already ingested",
    }
    assert len(core.sync(served["slug"])[0]["phases"]) == 3


def test_a_master_author_sends_no_note(served, tmp_path, capsys):
    run(tmp_path, capsys, env={**SEAT, "AGENTIHOOKS_AGENT_NAME": "master@plan-proof"})
    assert served["notes"] == []


def test_a_duplicate_phase_rolls_back_every_row(served, tmp_path, capsys):
    text = MARKED.replace("id=beta", "id=p1")
    with pytest.raises(SystemExit) as raised:
        run(tmp_path, capsys, text=text)
    assert "phase id p1 is already taken" in str(raised.value)
    state = core.sync(served["slug"])[0]
    assert [p["id"] for p in state["phases"]] == ["p1"]
    assert (state.get("plans", []), state.get("slices", [])) == ([], [])


def test_the_command_refuses_another_swarms_seat_before_reading(served, tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        run(tmp_path, capsys, env={**SEAT, "AGENTIHOOKS_SWARM": "other"})
    assert str(raised.value) == "a seat of swarm other cannot ingest a plan into ledger plan-proof"


def test_the_command_refuses_a_plan_the_normalizer_refuses(served, tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        run(tmp_path, capsys, text="   \n")
    assert str(raised.value) == "the plan has no content"


def test_plan_read_still_routes_through_the_plan_command(monkeypatch):
    seen = []
    monkeypatch.setattr("scripts.swarm_ledger.plan_read.main", lambda argv, environ: seen.append(argv) or 0)
    assert cli.main(["read", "--task", "t1"], {}) == 0
    assert seen == [["read", "--task", "t1"]]
