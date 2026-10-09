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


def edits():
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

    def nothing(s):
        pass

    return [
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
        nothing,
    ]


@pytest.mark.parametrize("edit", edits(), ids=lambda edit: edit.__name__)
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
