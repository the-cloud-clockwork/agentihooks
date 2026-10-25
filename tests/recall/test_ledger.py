import copy
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from scripts.recall.ledger import extract_ledger
from scripts.recall.models import RecallRecord


@pytest.fixture
def ledger():
    return json.loads(Path(__file__).with_name("fixtures").joinpath("ledger.json").read_text())


def records(ledger):
    return {r.ref: r for r in extract_ledger("fixture", ledger, swarm_slug="swarm")}


def test_fixture_covers_every_kind_and_comment(ledger):
    found = records(ledger)
    expected = {
        "ledger": "ledger",
        "phases/p1": "phase",
        "tasks/t1": "task",
        "questions/q1": "question",
        "questions/q1/answers/a1": "answer",
        "followups/f1": "followup",
        "notes/n1": "note",
        "chat/m1": "chat",
        "artifacts/r1": "artifact",
        **{
            f"{parent}/comments/{entry}": "comment"
            for parent, entry in [
                ("phases/p1", "pc"),
                ("tasks/t1", "tc"),
                ("questions/q1", "qc"),
                ("questions/q1/answers/a1", "ac"),
                ("followups/f1", "fc"),
                ("notes/n1", "nc"),
                ("chat/m1", "mc"),
                ("artifacts/r1", "rc"),
            ]
        },
    }
    assert {ref: r.kind for ref, r in found.items()} == expected
    assert all(isinstance(r, RecallRecord) for r in found.values())
    assert found["ledger"].text == "Recall fixture\n\nEarlier swarm work"
    assert found["ledger"].time == 1
    assert found["phases/p1"].text == "Memory\n\nFind earlier work"
    assert found["artifacts/r1"].title == found["artifacts/r1"].text == "Recall design"
    assert "design.md" not in found["artifacts/r1"].text


def test_task_retains_all_searchable_fields(ledger):
    task = records(ledger)["tasks/t1"]
    assert task.title == "Build recall"
    for text in [
        "Build recall",
        "Extract ledger records",
        "Records are searchable",
        "Fixture test",
        "Reader",
        "pytest recall",
        "passed",
        "https://example.com/fix",
        "https://example.com/pull/42",
        "https://example.com/issues/41",
    ]:
        assert text in task.text


def test_structured_fields_keep_unicode_and_stable_order(ledger):
    ledger["tasks"][0]["contract"] = {"must": "Recuerda café", "check": "Verify", "judge": "Reader"}
    first = records(ledger)["tasks/t1"].text
    assert '## Contract\n\n{"check": "Verify", "judge": "Reader", "must": "Recuerda café"}' in first
    assert '## Proof\n\n{"command": "pytest recall", "fix": "https://example.com/fix", "output": "passed"}' in first
    ledger["tasks"][0]["contract"] = {"judge": "Reader", "check": "Verify", "must": "Recuerda café"}
    assert records(ledger)["tasks/t1"].text == first


def test_text_items_have_titles_and_standalone_artifacts_have_ledger_parent(ledger):
    found = records(ledger)
    assert found["questions/q1"].title == "How does recall work?"
    assert found["tasks/t1/comments/tc"].title == found["tasks/t1/comments/tc"].text == "Task comment"
    assert found["followups/f1"].title == "Add semantic search"
    del ledger["artifacts"][0]["task"]
    assert records(ledger)["artifacts/r1"].parent_ref == "ledger"


def test_missing_comment_text_and_root_time_are_unknown():
    ledger = {
        "title": "No history",
        "notes": [{"id": "n", "text": "Body", "comments": [{"id": "c", "attachments": [{"id": "image.png"}]}]}],
    }
    found = records(ledger)
    assert (found["ledger"].time, found["ledger"].author) == (0, "")
    assert (found["notes/n/comments/c"].title, found["notes/n/comments/c"].text) == ("", "")


def test_first_event_provenance_overrides_item_and_claimant(ledger):
    found = records(ledger)
    for ref, author, time in [
        ("tasks/t1", "creator", 2),
        ("questions/q1", "asker", 4),
        ("followups/f1", "reporter", 6),
    ]:
        assert (found[ref].author, found[ref].time) == (author, time)
    assert (found["questions/q1/answers/a1"].author, found["questions/q1/answers/a1"].time) == ("operator", 13)
    assert (found["notes/n1"].author, found["notes/n1"].time) == ("operator", 17)
    assert (found["chat/m1/comments/mc"].author, found["chat/m1/comments/mc"].time) == ("master", 20)


def test_keys_parent_refs_and_swarm_are_explicit(ledger):
    found = records(ledger)
    assert found["tasks/t1"].key == "fixture/tasks/t1#0"
    assert found["tasks/t1"].parent_ref == "phases/p1"
    assert found["artifacts/r1"].parent_ref == "tasks/t1"
    assert found["questions/q1/answers/a1"].parent_ref == "questions/q1"
    assert found["tasks/t1/comments/tc"].parent_ref == "tasks/t1"
    assert found["phases/p1"].parent_ref == "ledger"
    assert found["ledger"].parent_ref == ""
    assert all(r.ledger_slug == "fixture" and r.swarm_slug == "swarm" and r.chunk_index == 0 for r in found.values())
    assert all(r.swarm_slug == "" for r in extract_ledger("fixture", ledger))
    assert extract_ledger("other", ledger)[0].key == "other/ledger#0"


@pytest.mark.parametrize("collection", ["phases", "tasks", "questions", "followups", "notes", "chat", "artifacts"])
def test_deleted_items_and_their_children_are_omitted(ledger, collection):
    ledger[collection][0]["deleted"] = True
    assert not any(r.ref.startswith(collection + "/") for r in records(ledger).values())


def test_deleted_answer_hides_its_comment(ledger):
    ledger["questions"][0]["answers"][0]["deleted"] = True
    assert not any("/answers/" in r.ref for r in records(ledger).values())


def test_attachment_only_comment_is_still_a_record(ledger):
    ledger["tasks"][0]["comments"].append(
        {"id": "image", "by": "operator", "at": 23, "text": "", "attachments": [{"id": "image.png"}]}
    )
    comment = records(ledger)["tasks/t1/comments/image"]
    assert (comment.kind, comment.author, comment.time, comment.title, comment.text) == (
        "comment",
        "operator",
        23,
        "",
        "",
    )


def test_no_event_never_attributes_to_claimant_or_snapshot(ledger):
    ledger["_meta"]["events"] = []
    found = records(ledger)
    assert (found["tasks/t1"].author, found["tasks/t1"].time) == ("", 0)
    assert not any("seed" in r.ref or "Snapshot" in r.text for r in found.values())


def test_empty_document_and_missing_optional_fields():
    assert extract_ledger("empty", {}) == []
    found = extract_ledger("plain", {"tasks": [{"id": "t", "title": "Standalone"}]})
    assert len(found) == 1
    assert (found[0].text, found[0].parent_ref, found[0].author, found[0].time) == ("Standalone", "ledger", "", 0)


def test_pure_and_repeatable_with_stable_chunk_keys(ledger):
    ledger["tasks"][0]["description"] = "## First\n" + "a" * 1200 + "\n\n## Second\n" + "b" * 1200
    before = copy.deepcopy(ledger)
    first = extract_ledger("fixture", ledger)
    assert first == extract_ledger("fixture", ledger)
    assert ledger == before
    chunks = [r for r in first if r.ref == "tasks/t1"]
    assert len(chunks) > 2
    assert [r.chunk_index for r in chunks] == list(range(len(chunks)))
    assert [r.key for r in chunks] == [f"fixture/tasks/t1#{i}" for i in range(len(chunks))]
    assert all((r.title, r.author, r.time, r.parent_ref) == ("Build recall", "creator", 2, "phases/p1") for r in chunks)
    with pytest.raises(FrozenInstanceError):
        chunks[0].text = "changed"
