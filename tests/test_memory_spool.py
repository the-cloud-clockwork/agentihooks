"""SV2-SES-03: execution-local durable spool."""

import json
import os
from pathlib import Path

import pytest

from hooks.memory import spool as spool_module
from hooks.memory.spool import Entry, FlushResult, Spool, SpoolConfig, SpoolFull, run_flusher
from tests import sv2_ses03_cases

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parent.parent / "evidence" / "SV2-SES-03"


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def records(text):
    return [{"kind": "user_text", "text": text}]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def config(tmp_path):
    return SpoolConfig(tmp_path / "spool", max_bytes=10_000, warn_bytes=5_000)


@pytest.fixture
def spool(config, clock):
    return Spool(config, clock)


def batch_files(spool):
    return sorted(path.name for path in spool.batches.glob("*.json"))


def test_append_writes_one_checksummed_batch_file(spool):
    entry = spool.append("d:i:0:10", records("one"))
    assert (entry.sequence, entry.identity, entry.created, entry.records) == (1, "d:i:0:10", 100.0, records("one"))
    [name] = batch_files(spool)
    assert name.startswith("00000000000000000001-") and len(name) == len("00000000000000000001-") + 16 + 5
    data = json.loads((spool.batches / name).read_bytes())
    assert data == {
        "sequence": 1,
        "identity": "d:i:0:10",
        "created": 100.0,
        "records": records("one"),
        "sha256": spool_module._checksum(1, "d:i:0:10", 100.0, records("one")),
    }
    assert entry.size == (spool.batches / name).stat().st_size
    assert not list(spool.batches.glob(".*.tmp"))


def test_checksum_covers_every_field():
    base = spool_module._checksum(1, "a", 1.0, records("x"))
    assert len(base) == 64
    assert base != spool_module._checksum(2, "a", 1.0, records("x"))
    assert base != spool_module._checksum(1, "b", 1.0, records("x"))
    assert base != spool_module._checksum(1, "a", 2.0, records("x"))
    assert base != spool_module._checksum(1, "a", 1.0, records("y"))
    unsorted = [{"text": "x", "kind": "user_text"}]
    assert spool_module._checksum(1, "a", 1.0, unsorted) == base


def test_a_corrupt_pending_identity_is_appended_again(spool):
    spool.append("same", records("one"))
    path = spool.batches / batch_files(spool)[0]
    path.write_bytes(path.read_bytes().replace(b"one", b"0ne"))
    assert spool.append("same", records("one")).sequence == 2


def test_lag_follows_the_oldest_pending_batch(spool, clock):
    spool.append("a", records("x"))
    clock.now += 10
    spool.append("b", records("x"))
    clock.now += 5
    assert spool.status().archive_lag_seconds == 15.0


def test_sequences_grow_and_keep_utf8(spool):
    first = spool.append("a", records("café"))
    second = spool.append("b", records("日本"))
    assert (first.sequence, second.sequence) == (1, 2)
    raw = (spool.batches / batch_files(spool)[1]).read_bytes()
    assert "日本".encode() in raw


def test_a_pending_identity_is_appended_once(spool):
    first = spool.append("same", records("one"))
    again = spool.append("same", records("ignored"))
    assert again == first
    assert len(batch_files(spool)) == 1


def test_accept_appends_the_batch_identity_and_records(spool):
    one = records("one")

    class Batch:
        identity = "d:i:0:5"
        records = one

    spool.accept(Batch())
    [entry] = [spool._read(path) for path in spool._files()]
    assert (entry.identity, entry.records) == ("d:i:0:5", records("one"))


def test_status_reports_levels_bytes_and_lag(tmp_path, clock):
    probe = Spool(SpoolConfig(tmp_path / "probe", 10_000, 10_000), clock)
    size = probe.append("a", records("x")).size
    spool = Spool(SpoolConfig(tmp_path / "s", max_bytes=size * 3, warn_bytes=size * 2), clock)
    empty = spool.status()
    assert empty == spool_module.Status("ok", 0, 0, 0, [], [], [], 0, 0.0)
    spool.append("a", records("x"))
    clock.now += 7
    one = spool.status()
    assert (one.level, one.buffered, one.pending, one.transcript_spool_bytes) == ("ok", 1, [1], size)
    assert one.archive_lag_seconds == 7.0
    spool.append("b", records("x"))
    assert spool.status().level == "warning"
    spool.append("c", records("x"))
    full = spool.status()
    assert (full.level, full.transcript_spool_bytes) == ("full", size * 3)


def test_lag_never_goes_negative(spool, clock):
    spool.append("a", records("x"))
    clock.now -= 50
    assert spool.status().archive_lag_seconds == 0.0


def test_a_full_spool_refuses_and_keeps_the_oldest_batch(tmp_path, clock):
    probe = Spool(SpoolConfig(tmp_path / "probe", 10_000, 10_000), clock)
    size = probe.append("a", records("x")).size
    spool = Spool(SpoolConfig(tmp_path / "s", max_bytes=size * 2, warn_bytes=size), clock)
    spool.append("a", records("x"))
    spool.append("b", records("x"))
    before = {name: (spool.batches / name).read_bytes() for name in batch_files(spool)}
    with pytest.raises(SpoolFull) as error:
        spool.append("c", records("x"))
    assert str(error.value) == "spool_full"
    assert {name: (spool.batches / name).read_bytes() for name in batch_files(spool)} == before
    assert spool.status().pending == [1, 2]


def test_a_batch_that_exactly_fills_the_spool_is_accepted(tmp_path, clock):
    probe = Spool(SpoolConfig(tmp_path / "probe", 10_000, 10_000), clock)
    size = probe.append("a", records("x")).size
    spool = Spool(SpoolConfig(tmp_path / "s", max_bytes=size, warn_bytes=size), clock)
    assert spool.append("a", records("x")).sequence == 1


def test_flush_uploads_in_order_and_acknowledges_after_each_upload(spool):
    for name in "abc":
        spool.append(name, records(name))
    seen = []

    def upload(entry):
        seen.append((entry.sequence, spool.status().acknowledged))

    result = spool.flush(upload)
    assert result == FlushResult([1, 2, 3], [], "")
    assert seen == [(1, 0), (2, 1), (3, 2)]
    assert batch_files(spool) == []
    keys = {spool_module._key(name): sequence for sequence, name in enumerate("abc", 1)}
    assert json.loads(spool.ack_path.read_bytes()) == {"settled": 3, "lost": [], "recent": keys}
    assert spool.append("d", records("d")).sequence == 4


def test_flush_stops_at_the_first_outage_and_keeps_the_rest(spool):
    for name in "abc":
        spool.append(name, records(name))

    def upload(entry):
        if entry.sequence == 2:
            raise ConnectionError("down")

    result = spool.flush(upload)
    assert result == FlushResult([1], [], "ConnectionError")
    status = spool.status()
    assert (status.acknowledged, status.pending) == (1, [2, 3])


def test_flush_of_an_empty_spool_uploads_nothing(spool):
    assert spool.flush(lambda entry: pytest.fail("no upload")) == FlushResult()
    assert not spool.ack_path.exists()


def test_a_corrupt_batch_is_quarantined_and_reported_lost(spool):
    spool.append("a", records("one"))
    spool.append("b", records("two"))
    path = spool.batches / batch_files(spool)[0]
    path.write_bytes(path.read_bytes().replace(b"one", b"0ne"))
    uploaded = []
    result = spool.flush(lambda entry: uploaded.append(entry.identity))
    assert result == FlushResult([2], [1], "")
    assert uploaded == ["b"]
    assert (spool.quarantine / path.name).exists()
    status = spool.status()
    assert (status.settled, status.acknowledged, status.lost) == (2, 0, [1])
    assert json.loads(spool.ack_path.read_bytes())["recent"] == {spool_module._key("b"): 2}


def test_status_lists_a_corrupt_batch_apart_from_pending(spool):
    spool.append("a", records("one"))
    spool.append("b", records("two"))
    path = spool.batches / batch_files(spool)[0]
    path.write_bytes(path.read_bytes().replace(b"one", b"0ne"))
    status = spool.status()
    assert (status.pending, status.corrupt, status.buffered) == ([2], [1], 2)


def test_an_acknowledged_identity_is_not_appended_again(spool):
    spool.append("a", records("one"))
    spool.flush(lambda entry: None)
    assert spool.append("a", records("one")) is None
    assert batch_files(spool) == []
    assert spool.append("b", records("two")).sequence == 2


def test_recent_identities_keep_only_the_newest(spool, monkeypatch):
    monkeypatch.setattr(spool_module, "RECENT_IDENTITIES", 2)
    for name in "abc":
        spool.append(name, records(name))
    spool.flush(lambda entry: None)
    recent = json.loads(spool.ack_path.read_bytes())["recent"]
    assert recent == {spool_module._key("b"): 2, spool_module._key("c"): 3}
    assert spool.append("a", records("a")).sequence == 4


def test_the_batch_file_stays_until_its_acknowledgement_is_written(spool, monkeypatch):
    spool.append("a", records("one"))

    def crash(path, lost):
        raise OSError("ack write failed")

    monkeypatch.setattr(spool, "_acknowledge", crash)
    with pytest.raises(OSError):
        spool.flush(lambda entry: None)
    assert len(batch_files(spool)) == 1


def test_every_lock_is_exclusive(spool, monkeypatch):
    calls = []
    real = spool_module.fcntl.flock
    monkeypatch.setattr(spool_module.fcntl, "flock", lambda handle, mode: calls.append(mode) or real(handle, mode))
    spool.append("a", records("one"))
    spool.status()
    spool.flush(lambda entry: None)
    assert calls == [spool_module.fcntl.LOCK_EX] * 4


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"sequence": 1}',
        b'{"sequence": 2, "identity": "a", "created": 1.0, "records": []}',
    ],
)
def test_unreadable_or_mismatched_batches_read_as_none(spool, body):
    spool.batches.mkdir(parents=True)
    path = spool.batches / "00000000000000000001-0000000000000000.json"
    path.write_bytes(body)
    assert spool._read(path) is None


def test_a_batch_renamed_to_another_sequence_reads_as_none(spool):
    spool.append("a", records("one"))
    original = spool.batches / batch_files(spool)[0]
    moved = original.with_name("00000000000000000009" + original.name[20:])
    original.rename(moved)
    assert spool._read(moved) is None


def test_acknowledge_never_moves_the_watermark_back(spool):
    spool.batches.mkdir(parents=True)
    spool._acknowledge(spool.batches / "00000000000000000003-k3.json", lost=False)
    spool._acknowledge(spool.batches / "00000000000000000002-k2.json", lost=True)
    spool._acknowledge(spool.batches / "00000000000000000003-k4.json", lost=True)
    assert json.loads(spool.ack_path.read_bytes()) == {"settled": 3, "lost": [], "recent": {"k3": 3}}


def test_files_at_or_below_the_watermark_are_neither_pending_nor_reuploaded(spool):
    spool.append("a", records("one"))
    spool.append("b", records("two"))
    spool._acknowledge(spool.batches / batch_files(spool)[0], lost=False)
    assert spool.status().pending == [2]
    uploaded = []
    spool.flush(lambda entry: uploaded.append(entry.sequence))
    assert uploaded == [2]


def test_a_torn_temporary_file_is_not_buffered(spool):
    spool.append("a", records("one"))
    (spool.batches / ".00000000000000000002-x.json.tmp").write_bytes(b"{")
    status = spool.status()
    assert (status.buffered, status.pending) == (1, [1])
    assert spool.append("b", records("two")).sequence == 2


def test_a_restarted_spool_reads_the_same_state(spool, config, clock):
    spool.append("a", records("one"))
    spool.append("b", records("two"))

    def upload(entry):
        if entry.sequence == 2:
            raise OSError("down")

    spool.flush(upload)
    again = Spool(config, clock).status()
    assert (again.acknowledged, again.pending, again.buffered) == (1, [2], 2)


def test_write_durably_syncs_the_file_then_the_folder(tmp_path, monkeypatch):
    events, opened = [], []
    real_open, real_fsync, real_close, real_replace = os.open, os.fsync, os.close, Path.replace

    def record_open(path, flags):
        fd = real_open(path, flags)
        opened.append(fd)
        events.append(("open", Path(path) == tmp_path, flags))
        return fd

    def record_fsync(fd):
        events.append(("fsync", fd in opened))
        if fd not in opened:
            assert os.fstat(fd).st_size == len(b"data")
        real_fsync(fd)

    def record_close(fd):
        events.append(("close", fd in opened))
        real_close(fd)

    def record_replace(self, target):
        events.append(("replace", self.name, Path(target).name))
        return real_replace(self, target)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "close", record_close)
    monkeypatch.setattr(Path, "replace", record_replace)
    spool_module._write_durably(tmp_path / "x.json", b"data")
    assert [event for event in events if event[0] != "close" or event[1]] == [
        ("fsync", False),
        ("replace", ".x.json.tmp", "x.json"),
        ("open", True, os.O_RDONLY),
        ("fsync", True),
        ("close", True),
    ]
    assert (tmp_path / "x.json").read_bytes() == b"data"


def test_run_flusher_flushes_each_interval_then_once_on_stop(spool):
    class Stop:
        def __init__(self, answers):
            self.answers = answers
            self.intervals = []

        def wait(self, interval):
            self.intervals.append(interval)
            return self.answers.pop(0)

    calls = []
    spool.flush = lambda upload: calls.append(upload) or FlushResult([len(calls)])
    stop = Stop([False, False, True])
    upload = object()
    result = run_flusher(spool, upload, stop, 12.5)
    assert stop.intervals == [12.5, 12.5, 12.5]
    assert calls == [upload, upload, upload]
    assert result == FlushResult([3])


def test_entry_fields():
    assert Entry(1, "a", 2.0, [], 3).size == 3


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_ses03_cases.run_case(case), sv2_ses03_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)


def test_the_package_result_names_the_case_records_and_states():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, digest in sv2_ses03_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == digest
    assert manifest["cases"] == ["T-SV2-SES-03-A", "T-SV2-SES-03-B", "T-SV2-SES-03-C"]
    assert result["package"] == "SV2-SES-03"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
