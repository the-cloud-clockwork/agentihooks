import copy

import pytest

from scripts.swarm_ledger.repository.rows import changes, diff, flatten


def base():
    return {
        "title": "t",
        "tasks": [
            {"id": "t1", "title": "a", "comments": [{"id": "c1", "text": "x"}]},
            {"id": "t2", "title": "b", "comments": []},
            {"id": "t2", "title": "dup", "comments": []},
        ],
        "chat": [{"id": "m1", "text": "hi"}],
        "_meta": {"rev": 1, "stamps": {"a": {"at": 1}}, "members": {}},
    }


def field(s):
    s["tasks"][0]["title"] = "changed"


def append_comment(s):
    s["tasks"][1]["comments"].append({"id": "c2", "text": "y"})


def append_task(s):
    s["tasks"].append({"id": "t3", "title": "c", "comments": []})


def remove_task(s):
    del s["tasks"][0]


def reorder(s):
    s["tasks"].reverse()


def stamp(s):
    s["_meta"]["stamps"]["b"] = {"at": 2}
    s["_meta"]["rev"] = 2


def drop_key(s):
    del s["_meta"]["stamps"]["a"]


def retype(s):
    s["chat"] = {"not": "a list"}


def duplicate_edit(s):
    s["tasks"][2]["title"] = "dup changed"


def add_top(s):
    s["extension"] = [{"id": "x", "comments": [{"id": "c", "text": "z"}]}]


def rename_key(s):
    s["_meta"]["stamps"] = {"z": {"at": 1}}


def nothing(s):
    pass


def insert_first(s: dict) -> None:
    s["tasks"].insert(0, {"id": "t0", "title": "z", "comments": [{"id": "c0", "text": "w"}]})


def drop_middle(s: dict) -> None:
    del s["tasks"][1]


EDITS = [
    field,
    append_comment,
    append_task,
    remove_task,
    reorder,
    stamp,
    drop_key,
    retype,
    duplicate_edit,
    add_top,
    rename_key,
    nothing,
    insert_first,
    drop_middle,
]


@pytest.mark.parametrize("edit", EDITS, ids=lambda edit: edit.__name__)
def test_partial_rows_turn_stored_rows_into_the_new_document(edit):
    old = base()
    new = copy.deepcopy(old)
    edit(new)
    before, after = diff(old, new)
    stored = flatten(old)
    for path, row in changes(before, after).items():
        if row is None:
            del stored[path]
        else:
            stored[path] = row
    assert stored == flatten(new)


def test_one_field_edit_touches_one_row():
    old = base()
    new = copy.deepcopy(old)
    new["tasks"][0]["title"] = "changed"
    before, after = diff(old, new)
    assert list(changes(before, after)) == ['["tasks",["id","t1",0],"title"]']


def test_unchanged_document_touches_no_rows():
    assert diff(base(), base()) == ({}, {})


@pytest.mark.parametrize("edit", EDITS, ids=lambda edit: edit.__name__)
def test_diff_rows_are_the_rows_flatten_stores(edit):
    old = base()
    new = copy.deepcopy(old)
    edit(new)
    before, after = diff(old, new)
    stored, wanted = flatten(old), flatten(new)
    assert before == {path: stored[path] for path in before}
    assert after == {path: wanted[path] for path in after}


def test_a_field_edit_reads_and_writes_only_its_own_row():
    old = base()
    new = copy.deepcopy(old)
    new["tasks"][0]["title"] = "changed"
    before, after = diff(old, new)
    path = '["tasks",["id","t1",0],"title"]'
    assert (list(before), list(after)) == ([path], [path])


@pytest.mark.parametrize(("old", "new"), [({"a": 1}, ["x"]), ([], {})])
def test_a_container_of_another_type_replaces_every_row(old, new):
    before, after = diff(old, new)
    assert (before, after) == (flatten(old), flatten(new))


def test_an_empty_list_turned_empty_object_replaces_its_own_row():
    old, new = {"x": []}, {"x": {}}
    assert diff(old, new) == ({'["x"]': flatten(old)['["x"]']}, {'["x"]': flatten(new)['["x"]']})


def freeze(row_id: str, verb: str) -> dict:
    return {"id": row_id, "verb": verb, "target": f"tasks/{row_id}", "by": "m", "at": 1, "reason": ""}


def frozen(count: int, focuses: list[str]) -> dict:
    return {
        "title": "t",
        "freezes": [freeze(f"f{n}", "freeze") for n in range(count)] + [freeze(f, "focus") for f in focuses],
    }


def row_path(row_id: str) -> str:
    return f'["freezes",["id","{row_id}",0]]'


@pytest.mark.feature("ledger-row-diff-keyed-lists")
def test_a_top_focus_insert_into_a_large_freeze_list_writes_only_the_new_row_and_the_moved_focus() -> None:
    old = frozen(1000, ["c0"])
    new = copy.deepcopy(old)
    new["freezes"].insert(1000, freeze("c1", "focus"))
    before, after = diff(old, new)
    added = set(flatten(new)) - set(flatten(old))
    assert len(added) == 7
    assert set(changes(before, after)) == added | {row_path("c0")}


@pytest.mark.feature("ledger-row-diff-keyed-lists")
def test_an_only_focus_filter_deletes_only_the_dropped_focus_rows() -> None:
    old = frozen(1000, ["c0", "c1", "c2"])
    new = copy.deepcopy(old)
    del new["freezes"][1002]
    del new["freezes"][1000]
    before, after = diff(old, new)
    gone = set(flatten(old)) - set(flatten(new))
    assert len(gone) == 14
    changed = changes(before, after)
    assert set(changed) == gone | {row_path("c1")}
    assert all(changed[path] is None for path in gone)
    assert changed[row_path("c1")] == flatten(new)[row_path("c1")]


@pytest.mark.feature("ledger-row-diff-keyed-lists")
def test_a_freeze_clear_in_the_middle_rewrites_no_field_row_of_a_kept_freeze() -> None:
    old = frozen(50, [])
    new = copy.deepcopy(old)
    del new["freezes"][10]
    before, after = diff(old, new)
    changed = changes(before, after)
    gone = set(flatten(old)) - set(flatten(new))
    moved = {row_path(f"f{n}") for n in range(11, 50)}
    assert set(changed) == gone | moved
    assert {path: changed[path] for path in moved} == {path: flatten(new)[path] for path in moved}
