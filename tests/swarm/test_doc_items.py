import json
from unittest.mock import Mock

import pytest


def test_command_help_and_required_inputs(capsys):
    from scripts.swarm.doc_items import main

    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    assert "Map watchdog document items to their ledger tasks" in capsys.readouterr().out.splitlines()
    for args, missing in [(["--slug", "sw"], "--document"), (["--document", "doc.md"], "--slug")]:
        with pytest.raises(SystemExit) as error_exit:
            main(args)
        assert error_exit.value.code == 2
        assert f"the following arguments are required: {missing}" in capsys.readouterr().err


def test_mapping_keeps_every_open_item_and_expands_task_ranges():
    from scripts.swarm.doc_items import parse

    document = """# Open work
1. [open] First item, with punctuation. [Ledger: t1, t-three]
2. [closed] Finished item. [Ledger: t1]
3. [open] Range item. [Ledger: t4 to t6, t1]
4. [open] No assignment.
5. [open] Another item. [Ledger: t1]
6. [open] One task range. [Ledger: t7 to t7]
"""
    assert parse(document) == {
        "t1": ["1 First item, with punctuation.", "3 Range item.", "5 Another item."],
        "t-three": ["1 First item, with punctuation."],
        "t4": ["3 Range item."],
        "t5": ["3 Range item."],
        "t6": ["3 Range item."],
        "t7": ["6 One task range."],
    }


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("  1. [open] Item.  [Ledger: t1]  ", {"t1": ["1 Item."]}),
        ("1. [open] Item. [Ledger: t1", {}),
        ("1. [open] " + "word " * 2000, {}),
        ("not numbered [open] Item. [Ledger: t1]", {}),
        ("1. [open] [Ledger: t1]", {}),
        ("1. [open]   [Ledger: t1]", {}),
    ],
)
def test_mapping_handles_delimiters_and_untagged_text(line, expected):
    from scripts.swarm.doc_items import parse

    assert parse(line) == expected


def test_mapping_updates_done_tasks_preserves_links_and_is_idempotent():
    from scripts.swarm.doc_items import sync

    rows = [{"id": "t1", "state": "done", "doc_items": ["Earlier item"]}, {"id": "t2"}]
    ledger = Mock()
    ledger.tasks.return_value = rows

    def update(slug, tid, fields):
        assert slug == "sw"
        next(row for row in rows if row["id"] == tid).update(fields)

    ledger.update_task.side_effect = update
    mapping = {"t1": ["1 First item"], "t2": ["2 Second item"]}
    assert sync(ledger, "sw", mapping) == ["t1", "t2"]
    assert rows[0]["doc_items"] == ["Earlier item", "1 First item"]
    assert rows[1]["doc_items"] == ["2 Second item"]
    assert sync(ledger, "sw", mapping) == []
    assert ledger.update_task.call_count == 2
    ledger.tasks.assert_called_with("sw")


def test_unknown_tag_refuses_before_any_task_changes():
    from scripts.swarm.doc_items import sync

    ledger = Mock()
    ledger.tasks.return_value = [{"id": "t1"}]
    with pytest.raises(ValueError, match="^Unknown ledger tasks: t-absent, t-missing$"):
        sync(ledger, "sw", {"t1": ["1 Known"], "t-missing": ["2 Missing"], "t-absent": ["3 Absent"]})
    ledger.update_task.assert_not_called()


@pytest.mark.parametrize("tag", ["t5 to t3", "t1 to t-two", "t1 garbage", "", "t1] [Ledger: t2"])
def test_invalid_task_tags_are_reported(tag):
    from scripts.swarm.doc_items import parse

    with pytest.raises(ValueError, match="Ledger tag"):
        parse(f"1. [open] Item [Ledger: {tag}]")


def test_command_reads_the_document_and_prints_every_mapping(tmp_path, monkeypatch, capsys):
    from scripts.swarm import doc_items

    document = tmp_path / "watchdog.md"
    document.write_text("1. [open] First item [Ledger: t1]\n2. [open] Second item [Ledger: t1]")
    ledger = Mock()
    ledger.tasks.return_value = [{"id": "t1"}]
    monkeypatch.setattr(doc_items, "LedgerClient", lambda: ledger)
    assert doc_items.main(["--slug", "sw", "--document", str(document)]) == 0
    ledger.tasks.assert_called_once_with("sw")
    ledger.update_task.assert_called_once_with("sw", "t1", {"doc_items": ["1 First item", "2 Second item"]})
    assert json.loads(capsys.readouterr().out) == {
        "slug": "sw",
        "tasks": 1,
        "items": 2,
        "updated": ["t1"],
        "mapping": {"t1": ["1 First item", "2 Second item"]},
    }
