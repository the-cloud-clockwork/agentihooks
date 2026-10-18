import itertools

import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import new_ledger
from scripts.swarm_ledger.api import schemas
from scripts.swarm_ledger.api.errors import APIError
from tests.swarm_ledger import legacy_page

SLUG = "plan-scope"
IDS = itertools.count()


@pytest.fixture(autouse=True)
def plan_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    content = {"title": "Scope", "overview": "Intent", "sources": [], "phases": [{"title": "Build"}]}
    html, json_path = core.paths(SLUG)
    json_path.unlink(missing_ok=True)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765))
    core.sync(SLUG)
    op = {"op": "plan_add", "id": f"plan-{next(IDS)}", "by": "planner", "plan": "a", "title": "Plan a"}
    core.check_op(op)
    assert core.sync(SLUG, ops=[op])[1] == []


def change(path, value, base):
    return core.sync(SLUG, changes=[{"path": path, "value": value, "base": base}])


def test_a_plan_out_of_scope_change_sets_the_flag_and_records_the_event():
    state, rejected = change("plans/a/out_of_scope", True, False)
    assert rejected == []
    assert state["plans"][0]["out_of_scope"] is True
    event = state["_meta"]["events"][-1]
    assert (event["by"], event["kind"], event["target"]) == ("operator", "out of scope", "plans/a")


def test_a_plan_comes_back_in_scope():
    change("plans/a/out_of_scope", True, False)
    state, rejected = change("plans/a/out_of_scope", False, True)
    assert rejected == []
    assert state["plans"][0]["out_of_scope"] is False
    assert state["_meta"]["events"][-1]["kind"] == "back in scope"


def test_a_plan_takes_no_done_change_and_an_unknown_plan_is_rejected():
    state, rejected = change("plans/a/done", True, False)
    assert rejected == ["plans/a/done"]
    assert "done" not in state["plans"][0]
    state, rejected = change("plans/zz/out_of_scope", True, False)
    assert rejected == ["plans/zz/out_of_scope"]


def test_a_stale_plan_base_loses():
    change("plans/a/out_of_scope", True, False)
    state, rejected = change("plans/a/out_of_scope", False, False)
    assert rejected == ["plans/a/out_of_scope"]
    assert state["plans"][0]["out_of_scope"] is True


def test_a_non_boolean_plan_out_of_scope_is_refused_by_validation():
    doc = {"title": "t", "overview": "o", "sources": [], "plans": [{"id": "a", "title": "A", "out_of_scope": "yes"}]}
    with pytest.raises(ValueError) as raised:
        core.validate(doc)
    assert str(raised.value) == "plans/a/out_of_scope must be bool"


def mutation(path):
    return {"operation_id": "o", "ops": [], "guards": {}, "changes": [{"path": path, "value": True}]}


def test_the_mutation_schema_takes_a_plan_out_of_scope_change_only():
    schemas.validate(schemas.MUTATION, mutation("plans/a/out_of_scope"))
    with pytest.raises(APIError):
        schemas.validate(schemas.MUTATION, mutation("plans/a/done"))
