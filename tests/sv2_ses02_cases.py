import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from hooks.memory.collector import Collector
from hooks.memory.cursors import CursorConflict, CursorStore
from hooks.memory.transcript_reader import iter_transcript_records

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/transcript-tailing.json"
EVIDENCE_CLASS = (
    "mocked: local files in a temporary folder stand in for a provider transcript; "
    "no provider process, spool or network runs"
)


def line(entry):
    return (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")


def split_inside_character(data):
    index = next(i for i, byte in enumerate(data) if byte >= 0xC0)
    return data[: index + 1], data[index + 1 :]


class Sink:
    def __init__(self):
        self.accepted = {}
        self.calls = 0
        self.fail = False

    def __call__(self, batch):
        self.calls += 1
        if self.fail:
            raise OSError("destination unavailable")
        assert batch.identity not in self.accepted
        self.accepted[batch.identity] = [record["text"] for record in batch.records]

    def texts(self):
        return [text for texts in self.accepted.values() for text in texts]


def append(path, data):
    with open(path, "ab") as handle:
        handle.write(data)


def cursor_bytes(root):
    return {path.name: path.read_bytes() for path in sorted(Path(root).glob("*.json"))}


def positive(inputs, root, source):
    store = CursorStore(root / "cursors")
    collector, sink = Collector(store), Sink()
    head, tail = split_inside_character(line(inputs["first"]))
    source.write_bytes(head)
    steps = [collector.poll(source, sink).cursor.offset]
    append(source, tail)
    steps.append(collector.poll(source, sink).cursor.offset)
    boundary = steps[-1]
    append(source, line(inputs["appended"]))
    appended = collector.poll(source, sink)
    steps.append(appended.cursor.offset)
    again = collector.poll(source, sink)
    assert again.records == [] and again.cursor.offset == appended.cursor.offset
    source.rename(source.with_suffix(".1"))
    source.write_bytes(line(inputs["rotated"]))
    rotated = collector.poll(source, sink)
    source.write_bytes(line(inputs["truncated"]))
    truncated = collector.poll(source, sink)
    source.write_bytes(line(inputs["regrown"]))
    regrown = collector.poll(source, sink)
    assert [record["text"] for record in appended.records] == [inputs["appended"]["message"]["content"][0]["text"]]
    assert appended.start == boundary
    assert (rotated.replayed, truncated.replayed, regrown.replayed) == (True, True, True)
    assert truncated.cursor.offset < rotated.cursor.offset < regrown.cursor.offset
    assert sink.texts().count(inputs["appended"]["message"]["content"][0]["text"]) == 1
    return {
        "accepted_texts": sink.texts(),
        "committed_offsets": steps,
        "appended_line_ingested": len([t for t in sink.texts() if t == "日本 answer"]),
        "transcript_cursor_replays_total": store.replays_total(),
        "final_offset": regrown.cursor.offset,
        "final_revision": regrown.cursor.revision,
    }


def rejection(inputs, root, source):
    store = CursorStore(root / "cursors")
    collector, sink = Collector(store), Sink()
    source.write_bytes(line(inputs["first"]))
    committed = collector.poll(source, sink).cursor
    before = cursor_bytes(store.root)
    head, tail = split_inside_character(line(inputs["partial"]))
    append(source, head)
    unicode_split = collector.poll(source, sink)
    assert cursor_bytes(store.root) == before
    unterminated, rest = tail[:-3], tail[-3:]
    append(source, unterminated)
    json_split = collector.poll(source, sink)
    assert cursor_bytes(store.root) == before
    append(source, rest)
    sink.fail = True
    with pytest.raises(OSError):
        collector.poll(source, sink)
    assert cursor_bytes(store.root) == before
    with pytest.raises(CursorConflict) as stale:
        store.commit(replace(committed, offset=0, revision=0))
    assert cursor_bytes(store.root) == before
    sink.fail = False
    completed = collector.poll(source, sink)
    return {
        "unicode_split_pending_bytes": unicode_split.pending,
        "unicode_split_committed_offset": unicode_split.cursor.offset,
        "json_split_pending_bytes": json_split.pending,
        "json_split_committed_offset": json_split.cursor.offset,
        "cursor_unchanged_on_rejection": True,
        "refused_acceptance_calls": sink.calls,
        "stale_commit": str(stale.value),
        "accepted_texts": sink.texts(),
        "completed_offset": completed.cursor.offset,
        "transcript_cursor_replays_total": store.replays_total(),
    }


def recovery(inputs, root, source):
    store = CursorStore(root / "cursors")
    sink = Sink()
    source.write_bytes(line(inputs["first"]))
    durable = Collector(store).poll(source, sink).cursor
    head, tail = split_inside_character(line(inputs["partial"]))
    append(source, head)
    interrupted = Collector(store).poll(source, sink)
    del store
    restarted = CursorStore(root / "cursors")
    resumed = restarted.load(str(source))
    assert resumed == durable
    append(source, tail)
    collector = Collector(restarted)
    completed = collector.poll(source, sink)
    duplicate = collector.poll(source, sink)
    with pytest.raises(CursorConflict) as stale:
        restarted.commit(interrupted.cursor)
    whole = [record["text"] for record in iter_transcript_records(source)]
    assert whole == sink.texts()
    rebuilt = Sink()
    Collector(CursorStore(root / "replay")).poll(source, rebuilt)
    assert rebuilt.texts() == sink.texts()
    assert restarted.load(str(source)) == completed.cursor
    return {
        "controlled_replay_texts": rebuilt.texts(),
        "resumed_offset": resumed.offset,
        "completed_start": completed.start,
        "completed_texts": [record["text"] for record in completed.records],
        "duplicate_records": len(duplicate.records),
        "accepted_texts": sink.texts(),
        "stale_commit": str(stale.value),
        "committed_revision": restarted.load(str(source)).revision,
        "transcript_cursor_replays_total": restarted.replays_total(),
        "rollback_whole_file_reader": whole,
        "cursor_files_retained": len(cursor_bytes(restarted.root)),
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / inputs["source"]
        observed = CASES[case](inputs, root, source)
        assert os.path.exists(root / "cursors")
    return {
        "case": f"T-SV2-SES-02-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        "observed": observed,
    }
