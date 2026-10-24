"""SV2-SES-02: byte-safe transcript tailing with durable cursors."""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from hooks.memory.collector import ANCHOR_BYTES, Collector, read_batch
from hooks.memory.cursors import Cursor, CursorConflict, CursorStore
from tests import sv2_ses02_cases

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parent.parent / "evidence" / "SV2-SES-02"


def entry(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def line(text):
    return sv2_ses02_cases.line(entry(text))


def texts(batch):
    return [record["text"] for record in batch.records]


@pytest.fixture
def source(tmp_path):
    return tmp_path / "session.jsonl"


@pytest.fixture
def store(tmp_path):
    return CursorStore(tmp_path / "cursors")


def test_a_fresh_cursor_reads_every_complete_line_from_the_start(source):
    source.write_bytes(line("one") + line("two"))
    batch = read_batch(source, Cursor(str(source)))
    assert texts(batch) == ["one", "two"]
    size = source.stat().st_size
    assert (batch.start, batch.cursor.offset, batch.end, batch.pending) == (0, size, size, 0)
    assert batch.cursor.fmt == "claude"
    assert batch.replayed is False
    stat = os.stat(source)
    assert (batch.cursor.device, batch.cursor.inode) == (stat.st_dev, stat.st_ino)


def test_an_incomplete_trailing_record_stays_for_the_next_poll(source):
    source.write_bytes(line("one") + b'{"type": "user", "mess')
    batch = read_batch(source, Cursor(str(source)))
    assert texts(batch) == ["one"]
    assert batch.cursor.offset == len(line("one"))
    assert batch.end == source.stat().st_size
    assert batch.pending == len(b'{"type": "user", "mess')


def test_a_utf8_character_split_at_the_end_is_not_decoded(source):
    data = line("café")
    cut = data.index("é".encode()) + 1
    source.write_bytes(data[:cut])
    first = read_batch(source, Cursor(str(source)))
    assert first.records == [] and first.cursor.offset == 0 and first.pending == cut
    source.write_bytes(data)
    assert texts(read_batch(source, first.cursor)) == ["café"]


def test_a_complete_malformed_line_is_skipped_and_consumed(source):
    source.write_bytes(b"not json\n" + b"\xff\xfe\n" + line("ok"))
    batch = read_batch(source, Cursor(str(source)))
    assert texts(batch) == ["ok"]
    assert batch.cursor.offset == source.stat().st_size


def test_crlf_lines_decode(source):
    source.write_bytes(line("one")[:-1] + b"\r\n")
    assert texts(read_batch(source, Cursor(str(source)))) == ["one"]


def test_the_format_is_detected_once_and_kept(source):
    codex = {"type": "session_meta", "payload": {}}
    source.write_bytes(sv2_ses02_cases.line(codex))
    first = read_batch(source, Cursor(str(source)))
    assert first.cursor.fmt == "codex"
    kept = replace(first.cursor, fmt="copilot")
    assert read_batch(source, kept).cursor.fmt == "copilot"


def test_an_empty_file_proposes_offset_zero_with_no_format(source):
    source.write_bytes(b"")
    batch = read_batch(source, Cursor(str(source)))
    assert (batch.records, batch.cursor.offset, batch.cursor.fmt, batch.pending) == ([], 0, "", 0)


def test_turn_state_carries_across_batches(tmp_path):
    source = tmp_path / "rollout.jsonl"
    message = {
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
    }
    done = {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "hi"}}
    source.write_bytes(sv2_ses02_cases.line({"type": "session_meta", "payload": {}}) + sv2_ses02_cases.line(message))
    first = read_batch(source, Cursor(str(source)))
    assert first.cursor.turn == {"assistant_seen": True}
    with open(source, "ab") as handle:
        handle.write(sv2_ses02_cases.line(done))
    second = read_batch(source, first.cursor)
    assert (second.records, second.cursor.turn) == ([], {"assistant_seen": False})


def test_replacement_rotation_and_truncation_restart_from_zero(source):
    source.write_bytes(line("one") + line("two"))
    cursor = replace(read_batch(source, Cursor(str(source))).cursor, revision=1)
    source.rename(source.with_suffix(".1"))
    source.write_bytes(line("rotated"))
    rotated = read_batch(source, cursor)
    assert (rotated.start, texts(rotated), rotated.replayed, rotated.cursor.replays) == (0, ["rotated"], True, 1)
    cursor = replace(rotated.cursor, revision=2)
    source.write_bytes(b"")
    shrunk = read_batch(source, cursor)
    assert (shrunk.start, shrunk.replayed, shrunk.cursor.offset) == (0, True, 0)
    source.write_bytes(line("rotatee"))
    rewritten = read_batch(source, cursor)
    assert (rewritten.start, texts(rewritten), rewritten.replayed) == (0, ["rotatee"], True)


def test_the_anchor_covers_only_the_bytes_before_the_offset(source):
    long = line("x" * (ANCHOR_BYTES * 2))
    source.write_bytes(long)
    cursor = replace(read_batch(source, Cursor(str(source))).cursor, revision=1)
    source.write_bytes(b"y" + long[1:] + line("next"))
    assert read_batch(source, cursor).replayed is False
    source.write_bytes(long[:-2] + b"z\n" + line("next"))
    assert read_batch(source, cursor).replayed is True


def test_a_never_committed_cursor_does_not_count_a_replay(source):
    source.write_bytes(line("one"))
    batch = read_batch(source, Cursor(str(source), device=1, inode=1, offset=5))
    assert (batch.replayed, batch.cursor.replays, batch.start) == (False, 0, 0)


def test_batch_identity_names_generation_and_byte_range(source):
    source.write_bytes(line("one"))
    batch = read_batch(source, Cursor(str(source)))
    stat = os.stat(source)
    assert batch.identity == f"{stat.st_dev}:{stat.st_ino}:0:{batch.cursor.offset}"


def test_store_loads_a_blank_cursor_for_an_unknown_source(store):
    assert store.load("missing") == Cursor("missing")
    assert store.replays_total() == 0


def test_store_commit_round_trips_and_bumps_the_revision(store, tmp_path):
    cursor = Cursor(
        "s", device=1, inode=2, offset=3, anchor="a", fmt="claude", turn={"assistant_seen": True}, replays=4
    )
    committed = store.commit(cursor)
    assert committed == replace(cursor, revision=1)
    assert CursorStore(tmp_path / "cursors").load("s") == committed
    assert store.replays_total() == 4
    files = sorted(path.name for path in store.root.glob("*.json"))
    assert len(files) == 1
    assert not list(store.root.glob("*.tmp"))
    assert json.loads((store.root / files[0]).read_text())["revision"] == 1


def test_store_refuses_a_stale_revision_without_writing(store):
    first = store.commit(Cursor("s", offset=10))
    before = store.load("s")
    with pytest.raises(CursorConflict) as error:
        store.commit(replace(first, revision=0, offset=1))
    assert str(error.value) == "stale_cursor"
    assert store.load("s") == before
    with pytest.raises(CursorConflict):
        store.commit(replace(first, revision=2))


def test_store_sums_replays_across_sources(store):
    store.commit(Cursor("a", replays=2))
    store.commit(Cursor("b", replays=3))
    assert store.replays_total() == 5


def test_poll_commits_only_after_acceptance(source, store):
    source.write_bytes(line("one"))
    seen = []

    def refuse(batch):
        seen.append(texts(batch))
        raise OSError("down")

    with pytest.raises(OSError):
        Collector(store).poll(source, refuse)
    assert store.load(str(source)) == Cursor(str(source))
    batch = Collector(store).poll(source, lambda batch: seen.append(texts(batch)))
    assert seen == [["one"], ["one"]]
    assert batch.cursor == store.load(str(source))
    assert batch.cursor.revision == 1


def test_poll_without_new_data_neither_accepts_nor_commits(source, store):
    source.write_bytes(line("one"))
    collector = Collector(store)
    committed = collector.poll(source, lambda batch: None).cursor
    calls = []
    again = collector.poll(source, calls.append)
    assert (calls, again.records, again.cursor) == ([], [], committed)


def test_poll_commits_consumed_lines_that_produce_no_records(source, store):
    source.write_bytes(b'{"type": "summary"}\n')
    calls = []
    batch = Collector(store).poll(source, calls.append)
    assert calls == []
    assert store.load(str(source)).offset == batch.cursor.offset == source.stat().st_size


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_ses02_cases.run_case(case), sv2_ses02_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)


def test_the_package_result_names_the_case_records_and_states():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, digest in sv2_ses02_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == digest
    assert manifest["cases"] == ["T-SV2-SES-02-A", "T-SV2-SES-02-B", "T-SV2-SES-02-C"]
    assert result["package"] == "SV2-SES-02"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
