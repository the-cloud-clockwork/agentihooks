from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger.repository import rows, writer_copy
from scripts.swarm_ledger.repository.entry import thawed
from scripts.swarm_ledger.repository.fragments import Fragment, document, encoded
from scripts.swarm_ledger.repository.rows import changes, diff, flatten

Doc = dict[str, Any]
LIMIT = 64


def ledger(size: int = 40) -> Doc:
    return {
        "tasks": [{"id": f"t{index}", "title": f"task {index}", "comments": []} for index in range(size)],
        "questions": [{"id": index, "text": f"q{index}"} for index in range(5)],
        "notes": ["one", "two", "three"],
        "_meta": {
            "rev": 1,
            "stamps": {f"tasks/t{index}": {"at": index, "rev": index, "by": "a"} for index in range(size)},
        },
    }


def edited(old: Doc, collection: str, position: int, item: object) -> Doc:
    new = {**old, collection: list(old[collection])}
    new[collection][position] = item
    return new


def retitle(old: Doc) -> Doc:
    return edited(old, "tasks", 7, {**old["tasks"][7], "title": "changed"})


def comment(old: Doc) -> Doc:
    return edited(old, "tasks", 3, {**old["tasks"][3], "comments": [{"id": "c1", "text": "x"}]})


def rename(old: Doc) -> Doc:
    return edited(old, "tasks", 5, {**old["tasks"][5], "id": "renamed"})


def numeric_id(old: Doc) -> Doc:
    return edited(old, "questions", 2, {**old["questions"][2], "text": "changed"})


def numeric_rename(old: Doc) -> Doc:
    return edited(old, "questions", 2, {**old["questions"][2], "id": 99})


def later_duplicate(old: Doc) -> tuple[Doc, Doc]:
    new = {**old, "tasks": [*old["tasks"], {**old["tasks"][4]}]}
    edit = {**new, "tasks": list(new["tasks"])}
    edit["tasks"][-1] = {**new["tasks"][-1], "title": "duplicate changed"}
    return new, edit


def scalar_item(old: Doc) -> Doc:
    return edited(old, "notes", 1, "changed")


def many(old: Doc) -> Doc:
    new = {**old, "tasks": list(old["tasks"])}
    for position in range(min(LIMIT + 1, len(old["tasks"]))):
        new["tasks"][position] = {**old["tasks"][position], "title": "bulk"}
    return new


EDITS = [retitle, comment, rename, numeric_id, numeric_rename, scalar_item, many]


def applied(old: Doc, new: Doc) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    before, after = diff(old, new)
    stored: dict[str, Any] = flatten(old)
    for path, row in changes(before, after).items():
        if row is None:
            del stored[path]
        else:
            stored[path] = row
    return stored, before, after


@pytest.mark.parametrize("size", [40, LIMIT + 10])
@pytest.mark.parametrize("edit", EDITS, ids=lambda edit: edit.__name__)
def test_diff_of_lists_sharing_unchanged_items_stores_the_new_document(edit: Any, size: int) -> None:
    old = ledger(size)
    new = edit(old)
    stored, before, after = applied(old, new)
    assert stored == flatten(new)
    wanted, kept = flatten(new), flatten(old)
    assert after == {path: wanted[path] for path in after}
    assert before == {path: kept[path] for path in before}


def test_diff_of_a_changed_later_duplicate_writes_its_own_occurrence() -> None:
    duplicated, edit = later_duplicate(ledger())
    stored, _, after = applied(duplicated, edit)
    assert stored == flatten(edit)
    assert list(after) == ['["tasks",["id","t4",1],"title"]']


def test_diff_of_one_changed_item_visits_no_unchanged_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    old = ledger(200)
    new = retitle(old)
    children = Mock(wraps=rows.children)
    monkeypatch.setattr(rows, "children", children)
    _, before, after = applied(old, new)
    assert list(after) == ['["tasks",["id","t7",0],"title"]'] == list(before)
    visited = [call.args[0] for call in children.call_args_list]
    assert not any(item is old["tasks"] or item is new["tasks"] for item in visited)


def part(fragment: Fragment, key: str) -> Fragment:
    return cast(dict[str, Fragment], fragment.parts)[key]


def write(state: Doc) -> Doc:
    writer = cast(Doc, thawed(state))
    writer["_meta"]["stamps"] = dict(state["_meta"]["stamps"])
    writer["_meta"]["stamps"]["tasks/t1"] = {"at": 99, "rev": 2, "by": "b"}
    writer["_meta"]["rev"] = 2
    writer["tasks"][1]["title"] = "changed"
    return writer


def test_written_keeps_replaced_stamp_records_shared_with_the_writer_copy() -> None:
    state = ledger()
    previous = document(state)
    writer = write(state)
    record = writer["_meta"]["stamps"]["tasks/t1"]
    fragment, copy = writer_copy.written(writer, previous)
    stored = cast(Doc, part(part(fragment, "_meta"), "stamps").value)
    assert stored["tasks/t1"] is record
    assert copy["_meta"]["stamps"]["tasks/t1"] is record
    assert copy["_meta"]["stamps"] is not stored
    copy["_meta"]["stamps"]["tasks/t2"] = {"at": 100, "rev": 3, "by": "c"}
    assert stored["tasks/t2"]["at"] == 2


def test_written_detaches_no_stamp_record(monkeypatch: pytest.MonkeyPatch) -> None:
    state = ledger()
    previous = document(state)
    writer = write(state)
    detached = Mock(wraps=writer_copy.detached)
    monkeypatch.setattr(writer_copy, "detached", detached)
    records = list(writer["_meta"]["stamps"].values())
    writer_copy.written(writer, previous)
    assert not any(any(call.args[2] is record for record in records) for call in detached.call_args_list)


def test_written_stores_the_text_and_copy_of_the_written_state() -> None:
    state = ledger()
    previous = document(state)
    writer = write(state)
    wanted = cast(Doc, thawed(writer))
    fragment, copy = writer_copy.written(writer, previous)
    assert encoded(fragment) == encoded(document(wanted))
    assert copy == wanted
    copy["tasks"][1]["title"] = "after"
    assert cast(Doc, fragment.value)["tasks"][1]["title"] == "changed"


def test_diff_walks_at_most_the_limit_of_changed_positions_before_the_full_walk() -> None:
    old = ledger(LIMIT + 10)
    within: list[object] = [
        {**item, "title": "bulk"} if position < LIMIT else item for position, item in enumerate(old["tasks"])
    ]
    beyond: list[object] = [
        {**item, "title": "bulk"} if position <= LIMIT else item for position, item in enumerate(old["tasks"])
    ]
    walked = rows._changed_children(old["tasks"], within, ["tasks"])
    assert walked is not None and [change[3] for change in walked] == list(range(LIMIT))
    assert rows._changed_children(old["tasks"], beyond, ["tasks"]) is None
