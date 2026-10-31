from unittest.mock import Mock

import pytest

from scripts.swarm_ledger.repository import rows, writer_copy
from scripts.swarm_ledger.repository.entry import thawed
from scripts.swarm_ledger.repository.fragments import document, encoded
from scripts.swarm_ledger.repository.rows import changes, diff, flatten

LIMIT = 64


def ledger(size=40):
    return {
        "tasks": [{"id": f"t{index}", "title": f"task {index}", "comments": []} for index in range(size)],
        "questions": [{"id": index, "text": f"q{index}"} for index in range(5)],
        "notes": ["one", "two", "three"],
        "_meta": {
            "rev": 1,
            "stamps": {f"tasks/t{index}": {"at": index, "rev": index, "by": "a"} for index in range(size)},
        },
    }


def edited(old, collection, position, item):
    new = {**old, collection: list(old[collection])}
    new[collection][position] = item
    return new


def retitle(old):
    return edited(old, "tasks", 7, {**old["tasks"][7], "title": "changed"})


def comment(old):
    return edited(old, "tasks", 3, {**old["tasks"][3], "comments": [{"id": "c1", "text": "x"}]})


def rename(old):
    return edited(old, "tasks", 5, {**old["tasks"][5], "id": "renamed"})


def numeric_id(old):
    return edited(old, "questions", 2, {**old["questions"][2], "text": "changed"})


def numeric_rename(old):
    return edited(old, "questions", 2, {**old["questions"][2], "id": 99})


def later_duplicate(old):
    new = {**old, "tasks": [*old["tasks"], {**old["tasks"][4]}]}
    edit = {**new, "tasks": list(new["tasks"])}
    edit["tasks"][-1] = {**new["tasks"][-1], "title": "duplicate changed"}
    return new, edit


def scalar_item(old):
    return edited(old, "notes", 1, "changed")


def many(old):
    new = {**old, "tasks": list(old["tasks"])}
    for position in range(LIMIT + 1 if len(old["tasks"]) > LIMIT else len(old["tasks"])):
        new["tasks"][position] = {**old["tasks"][position], "title": "bulk"}
    return new


EDITS = [retitle, comment, rename, numeric_id, numeric_rename, scalar_item, many]


def applied(old, new):
    before, after = diff(old, new)
    stored = flatten(old)
    for path, row in changes(before, after).items():
        if row is None:
            del stored[path]
        else:
            stored[path] = row
    return stored, before, after


@pytest.mark.parametrize("size", [40, LIMIT + 10])
@pytest.mark.parametrize("edit", EDITS, ids=lambda edit: edit.__name__)
def test_diff_of_lists_sharing_unchanged_items_stores_the_new_document(edit, size):
    old = ledger(size)
    new = edit(old)
    stored, before, after = applied(old, new)
    assert stored == flatten(new)
    wanted, kept = flatten(new), flatten(old)
    assert after == {path: wanted[path] for path in after}
    assert before == {path: kept[path] for path in before}


def test_diff_of_a_changed_later_duplicate_writes_its_own_occurrence():
    old, new = later_duplicate(ledger())
    stored, _, after = applied(old, new)
    assert stored == flatten(new)
    assert list(after) == ['["tasks",["id","t4",1],"title"]']


def test_diff_of_one_changed_item_visits_no_unchanged_sibling(monkeypatch):
    old = ledger(200)
    new = retitle(old)
    children = Mock(wraps=rows.children)
    monkeypatch.setattr(rows, "children", children)
    _, before, after = applied(old, new)
    assert list(after) == ['["tasks",["id","t7",0],"title"]'] == list(before)
    visited = [call.args[0] for call in children.call_args_list]
    assert not any(item is old["tasks"] or item is new["tasks"] for item in visited)


def write(state):
    writer = thawed(state)
    writer["_meta"]["stamps"] = dict(state["_meta"]["stamps"])
    writer["_meta"]["stamps"]["tasks/t1"] = {"at": 99, "rev": 2, "by": "b"}
    writer["_meta"]["rev"] = 2
    writer["tasks"][1]["title"] = "changed"
    return writer


def test_written_keeps_replaced_stamp_records_shared_with_the_writer_copy():
    state = ledger()
    previous = document(state)
    writer = write(state)
    record = writer["_meta"]["stamps"]["tasks/t1"]
    fragment, copy = writer_copy.written(writer, previous)
    stored = fragment.parts["_meta"].parts["stamps"]
    assert stored.value["tasks/t1"] is record
    assert copy["_meta"]["stamps"]["tasks/t1"] is record
    assert copy["_meta"]["stamps"] is not stored.value
    copy["_meta"]["stamps"]["tasks/t2"] = {"at": 100, "rev": 3, "by": "c"}
    assert fragment.value["_meta"]["stamps"]["tasks/t2"]["at"] == 2


def test_written_detaches_no_stamp_record(monkeypatch):
    state = ledger()
    previous = document(state)
    writer = write(state)
    detached = Mock(wraps=writer_copy.detached)
    monkeypatch.setattr(writer_copy, "detached", detached)
    stamps = writer["_meta"]["stamps"]
    writer_copy.written(writer, previous)
    assert not any(
        isinstance(call.args[2], dict) and call.args[2] in stamps.values() for call in detached.call_args_list
    )


def test_written_stores_the_text_and_copy_of_the_written_state():
    state = ledger()
    previous = document(state)
    writer = write(state)
    wanted = thawed(writer)
    fragment, copy = writer_copy.written(writer, previous)
    assert encoded(fragment) == encoded(document(wanted))
    assert copy == wanted
    copy["tasks"][1]["title"] = "after"
    assert fragment.value["tasks"][1]["title"] == "changed"


def test_diff_walks_at_most_the_limit_of_changed_positions_before_the_full_walk():
    assert rows.CHANGED_LIMIT == LIMIT
