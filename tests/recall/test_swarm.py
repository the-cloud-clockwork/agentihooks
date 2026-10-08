import json
import os
from hashlib import sha256

import fakeredis
import pytest

from scripts.inbox.seats import SeatMemory, SeatRegistry, SwarmCulture
from scripts.inbox.store import InboxStore
from scripts.recall.swarm import extract_swarm
from scripts.swarm import store as swarm_store
from scripts.swarm.store import RedisStore


@pytest.fixture
def source():
    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def put_transfer(source, **changes):
    row = {
        "id": "transfer-one",
        "seat": "eng-1@sample",
        "task": "task-one",
        "predecessor": "old-engineer",
        "successor": "new-engineer",
        "at": 1234,
        "handoff": "## Intent\nBuild recall\n## Done\nTests passed\n## Stopped at\nReview\n"
        "## Decisions and promises\nKeep records\n## Next\nMerge\n## Read first\nIssue\n"
        "<!-- handoff complete -->",
        **changes,
    }
    source.redis.hset(source.key("sample", "transfers"), row["id"], json.dumps(row))
    return row


def test_transfers_split_short_handoffs_and_keep_metadata(source):
    put_transfer(source)
    records = extract_swarm("sample", source)
    assert len(records) == 6
    assert {record.title for record in records} == {
        "Intent",
        "Done",
        "Stopped at",
        "Decisions and promises",
        "Next",
        "Read first",
    }
    for record in records:
        assert record.kind == "handoff"
        assert record.ledger_slug == record.swarm_slug == "sample"
        assert record.author == "old-engineer"
        assert record.time == 1234
        assert record.parent_ref == "transfers/transfer-one"
        assert record.key.startswith("swarm/sample/")
        for text in ("eng-1@sample", "task-one", "old-engineer", "new-engineer"):
            assert text in record.text
        assert "handoff complete" not in record.text


def test_current_handoff_and_legacy_transfer(source):
    source.put_handoff(
        "sample",
        "task-one",
        "## Next\nRun tests",
        "eng-1@sample",
        {"agent": "old-engineer", "time": "1970-01-01T00:00:09+00:00"},
    )
    put_transfer(source, id="legacy", handoff="Legacy body")
    records = extract_swarm("sample", source)
    current = next(r for r in records if r.ref.startswith("tasks/task-one/handoff/"))
    assert current.author == "old-engineer"
    assert current.time == 9000
    assert "Run tests" in current.text
    assert "eng-1@sample" in current.text
    assert current.parent_ref == "tasks/task-one/handoff"
    assert any("Legacy body" in r.text for r in records)


def test_seat_recaps_and_notes_keep_identity_across_retirement_and_reordering(source):
    seats = SeatRegistry(source.redis)
    seats.occupy("eng-2@sample", "author", 5)
    memory = SeatMemory(source.redis)
    memory.add_recap("eng-2@sample", "author", "task-two", "Recap evidence", 10)
    memory.learn("eng-2@sample", "author", "Keep evidence because it survives", 11)
    before = extract_swarm("sample", source)
    note = next(r for r in before if r.kind == "learned")
    digest = sha256("Keep evidence because it survives".encode()).hexdigest()
    assert digest in note.key
    assert "author" in note.ref and "11" in note.ref
    assert note.author == "author" and note.time == 11
    assert '"maturity": "note"' in note.text
    memory.promote("eng-2@sample", 1, "insight", "reviewer", "Confirmed twice", 12)
    memory.retire("eng-2@sample", 1, "reviewer", "Replaced", 13)
    source.redis.lpush(
        memory.key("eng-2@sample", "learned"), json.dumps({"occupant": "other", "at": 1, "text": "Earlier note"})
    )
    after = extract_swarm("sample", source)
    updated = next(r for r in after if r.key == note.key)
    assert '"maturity": "insight"' in updated.text
    assert '"retired":' in updated.text
    recap = next(r for r in after if r.kind == "recap")
    assert recap.author == "author" and recap.time == 10
    assert "task-two" in recap.text and "Recap evidence" in recap.text
    assert recap.parent_ref == "seats/eng-2@sample"


def test_culture_inbox_sender_recipient_and_historical_agents(source):
    seats = SeatRegistry(source.redis)
    seats.occupy("eng-1@sample", "old-engineer", 1)
    seats.occupy("eng-1@sample", "new-engineer", 2)
    seats.occupy("eng-1@sample-other", "outsider", 3)
    SwarmCulture(source.redis).set("sample", "Preserve evidence")
    SwarmCulture(source.redis).set("sample-other", "Wrong culture")
    inbox = InboxStore(source.redis)
    sent = inbox.send("old-engineer", "external", "Outgoing historical mail")
    received = inbox.send("operator", "eng-1@sample", "Incoming seat mail", task="task-one")
    personal = inbox.send("operator", "new-engineer", "Personal mail")
    inbox.close(sent.id, "external", "done", "Completed")
    unrelated = inbox.send("outsider", "external", "Unrelated mail")
    records = extract_swarm("sample", source)
    mail = {r.ref: r for r in records if r.kind == "inbox"}
    assert set(mail) == {f"inbox/{item.id}" for item in (sent, received, personal)}
    assert f"inbox/{unrelated.id}" not in mail
    assert mail[f"inbox/{sent.id}"].author == "old-engineer"
    assert mail[f"inbox/{sent.id}"].time == sent.created_at
    assert '"state": "done"' in mail[f"inbox/{sent.id}"].text
    assert mail[f"inbox/{received.id}"].parent_ref == "tasks/task-one"
    culture = next(r for r in records if r.kind == "culture")
    assert culture.text == "Preserve evidence"


def test_task_files_use_workspace_and_modification_time(source, tmp_path):
    workspace = tmp_path / "external-workspace"
    workspace.mkdir()
    for name in ("steering", "progress", "proof"):
        path = workspace / f"{name}.md"
        path.write_text(f"{name} evidence")
        os.utime(path, ns=(1234567890000000, 1234567890000000))
    document = {"tasks": [{"id": "task-one", "workspace": str(workspace)}]}
    records = extract_swarm("sample", source, document, task_root=tmp_path / "default")
    assert len(records) == 3
    assert {r.kind for r in records} == {"steering", "progress", "proof"}
    for record in records:
        assert record.time == 1234567890
        assert record.ref == f"tasks/task-one/{record.kind}"
        assert record.parent_ref == "tasks/task-one"
        assert record.text == f"{record.kind} evidence"


def test_default_task_folders_and_missing_files(source, tmp_path):
    root = tmp_path / "tasks"
    (root / "task-one").mkdir(parents=True)
    (root / "task-one" / "proof.md").write_text("Proof")
    (root / "task-one" / "other.md").write_text("Ignored")
    records = extract_swarm("sample", source, task_root=root)
    assert [r.ref for r in records] == ["tasks/task-one/proof"]
    assert extract_swarm("sample", source, task_root=tmp_path / "absent") == []


def test_custom_swarm_key_prefix_is_used(source, monkeypatch):
    put_transfer(source, handoff="Wrong default prefix")
    monkeypatch.setattr(swarm_store, "PREFIX", "custom:swarm")
    put_transfer(source, handoff="Custom prefix body")
    source.put_handoff("sample", "task-two", "Current custom handoff")
    records = extract_swarm("sample", source)
    assert len(records) == 2
    assert any("Custom prefix body" in r.text for r in records)
    assert any("Current custom handoff" in r.text for r in records)
    assert all("Wrong default prefix" not in r.text for r in records)


def test_fenced_headings_and_nested_headings_stay_in_their_handoff_section(source):
    put_transfer(
        source,
        handoff="## Done\nEvidence\n~~~md\n## Next\nFenced\n~~~\n"
        "### Command output\nMore evidence\n## Next\nActual next",
    )
    records = extract_swarm("sample", source)
    assert [r.title for r in records] == ["Done", "Next"]
    assert "Fenced" in records[0].text and "More evidence" in records[0].text
    assert "Actual next" in records[1].text


def test_empty_transfer_preserves_relationships_and_long_chunks_share_ref(source):
    put_transfer(source, handoff="")
    empty = extract_swarm("sample", source)
    assert len(empty) == 1 and "new-engineer" in empty[0].text
    put_transfer(source, handoff="## Done\n" + "Evidence. " * 400)
    records = extract_swarm("sample", source)
    assert len(records) > 1
    assert len({r.ref for r in records}) == 1
    assert len({r.key for r in records}) == len(records)
    assert [r.chunk_index for r in records] == list(range(len(records)))
    assert all("old-engineer" in r.text for r in records)
