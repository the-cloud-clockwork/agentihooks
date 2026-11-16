"""The ledger task update takes the review round counts and reader launches that swarm review and the sub-agent gate
write, so a successor on any hive reads the review its predecessor left."""

from pathlib import Path

import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_tasks, new_ledger
from tests.swarm_ledger import legacy_page

SLUG = "reviewfields-2026-01-01"
ENGINEER = "engineer@abcdef-0002"
ROUNDS = {"rounds-before-pr": 3, "rounds-0a1b": 0}
READERS = ["spec-reader", "standards-reader"]
REFUSED = "review_rounds must map round counter names to nonnegative integers"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = legacy_page.paths(SLUG, core)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG, ops=[{"op": "task_add", "id": "seed", "by": "swarm", "task": "t1", "title": "a", "lane": "eng"}])


def op(fields: dict, op_id: str = "rv-1", **guards: object) -> dict:
    return {"op": "task_update", "id": op_id, "by": ENGINEER, "item": "tasks/t1", "fields": fields, **guards}


def task(state: dict) -> dict:
    return next(t for t in state["tasks"] if t["id"] == "t1")


@pytest.mark.feature("review-state-on-ledger")
def test_a_task_update_records_the_review_round_counts_and_readers() -> None:
    fields = {"review_rounds": ROUNDS, "review_readers": READERS}
    core.check_op(op(fields))
    state, _ = core.sync(SLUG, ops=[op(fields)])
    assert (task(state)["review_rounds"], task(state)["review_readers"]) == (ROUNDS, READERS)


def test_an_empty_review_rounds_object_is_taken() -> None:
    ledger_tasks.check(op({"review_rounds": {}}))


@pytest.mark.feature("review-state-on-ledger")
@pytest.mark.parametrize(
    "rounds",
    [[], "3", None, {"rounds": -1}, {"rounds": True}, {"rounds": 1.5}, {"rounds": "1"}, {"": 1}, {"rounds": None}],
)
def test_review_rounds_that_are_not_counter_names_to_whole_numbers_are_refused(rounds: object) -> None:
    with pytest.raises(ValueError) as refused:
        ledger_tasks.check(op({"review_rounds": rounds}))
    assert str(refused.value) == REFUSED


@pytest.mark.feature("review-state-on-ledger")
@pytest.mark.parametrize("readers", ["spec-reader", [""], [" "], [3]])
def test_review_readers_that_are_not_a_list_of_names_are_refused(readers: object) -> None:
    with pytest.raises(ValueError, match="review_readers must be a list of nonempty strings"):
        ledger_tasks.check(op({"review_readers": readers}))


@pytest.mark.feature("review-state-on-ledger")
def test_a_reader_write_lands_only_while_the_readers_it_read_still_hold() -> None:
    first = op({"review_readers": ["spec-reader"]}, "rv-1", if_match={"review_readers": []})
    stale = op({"review_readers": ["standards-reader"]}, "rv-2", if_match={"review_readers": []})
    fresh = op({"review_readers": READERS}, "rv-3", if_match={"review_readers": ["spec-reader"]})
    for write in (first, stale, fresh):
        core.check_op(write)
    landed = [task(core.sync(SLUG, ops=[write])[0])["review_readers"] for write in (first, stale, fresh)]
    assert landed == [["spec-reader"], ["spec-reader"], READERS]
