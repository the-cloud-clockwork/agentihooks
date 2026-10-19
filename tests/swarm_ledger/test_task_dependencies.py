import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_kinds, ledger_tasks, task_dependencies  # noqa: E402
from scripts.swarm_ledger.ledger_core import Context  # noqa: E402


def claim(doc, state="claimed", by="swarm"):
    op = {
        "op": "task_update",
        "id": "claim",
        "by": by,
        "item": "tasks/work",
        "fields": {"state": state, "claimed_by": "engineer-next"},
        "if_state": ["open"],
    }
    ledger_tasks.check(op)
    ctx = Context({"rev": 0, "stamps": {}}, 1)
    assert ledger_tasks.apply(doc, op, ctx)
    assert ctx.refused == []
    return doc["tasks"][0], ctx


@pytest.mark.parametrize("field", ["depends_on", "parked_on"])
@pytest.mark.parametrize("target", ["claimed", "pr"])
@pytest.mark.parametrize(
    ("state", "branch", "eligible"),
    [
        ("open", "", False),
        ("open", "feature", False),
        ("blocked", "feature", False),
        ("claimed", "", False),
        ("pr", "", False),
        ("claimed", "feature", True),
        ("pr", "feature", True),
        ("done", "", True),
    ],
)
def test_claim_checks_dependencies_added_after_selection(field, target, state, branch, eligible):
    work = {"id": "work", "title": "repair", "lane": "eng", "state": "open", "claimed_by": ""}
    doc = {"tasks": [work, {"id": "dep", "state": state, "branch": branch}]}
    selected = dict(work)
    work[field] = ["dep"]

    live, ctx = claim(doc, target)

    accepted = eligible and (field == "depends_on" or state == "done")
    assert "depends_on" not in selected and "parked_on" not in selected
    assert (live["state"], live["claimed_by"]) == ((target, "engineer-next") if accepted else ("open", ""))
    assert ctx.dirty == accepted


def test_claim_without_dependencies_still_applies():
    work = {"id": "work", "title": "repair", "lane": "eng", "state": "open", "claimed_by": ""}
    live, ctx = claim({"tasks": [work]})
    assert (live["state"], live["claimed_by"], ctx.dirty) == ("claimed", "engineer-next", True)


@pytest.mark.parametrize("kind", ["code", "ci", None])
@pytest.mark.parametrize(("state", "eligible"), [("claimed", False), ("pr", False), ("done", True)])
def test_a_tune_task_waits_for_its_code_dependency_to_be_done(kind, state, eligible):
    work = {"id": "work", "title": "measure", "lane": "ci", "kind": "tune", "state": "open", "claimed_by": ""}
    dep = {"id": "dep", "state": state, "branch": "feature", **({"kind": kind} if kind else {})}
    work["depends_on"] = ["dep"]

    live, ctx = claim({"tasks": [work, dep]})

    assert (live["state"], live["claimed_by"]) == (("claimed", "engineer-next") if eligible else ("open", ""))
    assert ctx.dirty == eligible


@pytest.mark.parametrize(
    ("work_kind", "dep_kind"), [("tune", "tune"), ("tune", "research"), ("tune", "ops"), ("code", "code")]
)
def test_other_kind_pairs_still_stack_on_a_claimed_dependency_branch(work_kind, dep_kind):
    work = {"id": "work", "title": "x", "lane": "eng", "kind": work_kind, "state": "open", "claimed_by": ""}
    dep = {"id": "dep", "state": "claimed", "branch": "feature", "kind": dep_kind}
    work["depends_on"] = ["dep"]

    live, ctx = claim({"tasks": [work, dep]})

    assert (live["state"], live["claimed_by"], ctx.dirty) == ("claimed", "engineer-next", True)


@pytest.mark.parametrize("state", ["claimed", "pr"])
def test_retained_pull_claim_waits_for_all_dependencies_to_finish(state):
    work = {
        "id": "work",
        "title": "repair",
        "lane": "eng",
        "state": "open",
        "claimed_by": "",
        "pr_url": "https://github.com/o/r/pull/3",
        "depends_on": ["dep"],
    }
    dep = {"id": "dep", "state": state, "branch": "feature"}
    doc = {"tasks": [work, dep]}

    live, ctx = claim(doc, "pr")

    assert (live["state"], live["claimed_by"], ctx.dirty) == ("open", "", False)
    assert live["pr_url"] == "https://github.com/o/r/pull/3"
    dep["state"] = "done"
    live, ctx = claim(doc, "pr")
    assert (live["state"], live["claimed_by"], ctx.dirty) == ("pr", "engineer-next", True)


@pytest.mark.parametrize("field", ["depends_on", "parked_on"])
def test_claim_waits_when_a_current_dependency_is_missing(field):
    work = {
        "id": "work",
        "title": "repair",
        "lane": "eng",
        "state": "open",
        "claimed_by": "",
        field: ["done", "missing"],
    }
    live, ctx = claim({"tasks": [work, {"id": "done", "state": "done"}]})
    assert (live["state"], live["claimed_by"], ctx.dirty) == ("open", "", False)


def test_claim_treats_an_unspecified_state_as_open():
    work = {"id": "work", "title": "repair", "lane": "eng", "claimed_by": "", "depends_on": ["dep"]}
    live, ctx = claim({"tasks": [work, {"id": "dep", "state": "open"}]})
    assert "state" not in live
    assert (live["claimed_by"], ctx.dirty) == ("", False)


def test_a_tune_task_is_not_ready_while_its_dependency_is_missing_from_the_rows():
    task = {"id": "m", "kind": "tune", "depends_on": ["gone"]}
    assert task_dependencies.ready(task, {"m": task}) is False


@pytest.mark.parametrize(("task", "read_only"), [({}, False), ({"kind": "ci"}, False), ({"kind": "tune"}, True)])
def test_read_only_names_every_kind_that_keeps_no_branch(task, read_only):
    assert ledger_kinds.read_only(task) is read_only


@pytest.mark.parametrize(
    ("state", "by", "fields"),
    [
        ("open", "engineer", {"state": "claimed", "claimed_by": "engineer-next"}),
        ("claimed", "swarm", {"state": "pr", "claimed_by": "engineer-next"}),
        ("open", "swarm", {"state": "open", "claimed_by": "engineer-next"}),
        ("open", "swarm", {"state": "claimed", "claimed_by": ""}),
        ("open", "swarm", {"branch": "retained-branch"}),
        ("pr", "swarm", {"claimed_by": "engineer-next"}),
    ],
)
def test_dependency_guard_preserves_nonclaim_updates(state, by, fields):
    work = {
        "id": "work",
        "title": "repair",
        "lane": "eng",
        "state": state,
        "claimed_by": "previous",
        "depends_on": ["dep"],
    }
    doc = {"tasks": [work, {"id": "dep", "state": "open"}]}
    op = {"op": "task_update", "id": "update", "by": by, "item": "tasks/work", "fields": fields}
    ledger_tasks.check(op)
    ctx = Context({"rev": 0, "stamps": {}}, 1)

    assert ledger_tasks.apply(doc, op, ctx)

    assert ctx.refused == []
    assert ctx.dirty
    assert all(work[key] == value for key, value in fields.items())
