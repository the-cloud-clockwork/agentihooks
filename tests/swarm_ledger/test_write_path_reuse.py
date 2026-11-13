from typing import Any
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger.repository import rows
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


def swap(old: Doc) -> Doc:
    new = edited(old, "tasks", 2, old["tasks"][9])
    new["tasks"][9] = old["tasks"][2]
    return new


def to_scalar(old: Doc) -> Doc:
    return edited(old, "tasks", 6, "plain")


def anonymous(old: Doc) -> Doc:
    rows_without_id = {**old, "notes": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}
    return edited(rows_without_id, "notes", 1, {"text": "changed"})


EDITS = [retitle, comment, rename, numeric_id, numeric_rename, scalar_item, many, swap, to_scalar]


def test_diff_of_a_changed_earlier_duplicate_writes_its_own_occurrence() -> None:
    old = ledger()
    duplicated = {**old, "tasks": [*old["tasks"], {**old["tasks"][4]}]}
    edit = edited(duplicated, "tasks", 4, {**old["tasks"][4], "title": "first changed"})
    stored, _, after = applied(duplicated, edit)
    assert stored == flatten(edit)
    assert list(after) == ['["tasks",["id","t4",0],"title"]']


def test_diff_of_dicts_without_ids_stores_the_new_document() -> None:
    old = {**ledger(), "notes": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}
    new = anonymous(old)
    stored, _, after = applied(old, new)
    assert stored == flatten(new)
    assert list(after) == ['["notes",["index",1,0],"text"]']


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
    before, after = diff(old, new)
    monkeypatch.undo()
    assert list(after) == ['["tasks",["id","t7",0],"title"]'] == list(before)
    visited = [call.args[0] for call in children.call_args_list]
    assert not any(item is old["tasks"] or item is new["tasks"] for item in visited)


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
