import hashlib
import json
import tempfile
import threading
from pathlib import Path

import pytest

from hooks.memory.collector import Collector
from hooks.memory.cursors import CursorStore
from hooks.memory.spool import Spool, SpoolConfig, SpoolFull, run_flusher

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/spool-outage.json"
EVIDENCE_CLASS = (
    "mocked: a local transcript file and an in-process destination stand in for the provider and central "
    "ingestion; no network, Pod or hive collector runs"
)


class WorkerLost(BaseException):
    pass


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Central:
    def __init__(self):
        self.accepted = {}
        self.attempts = []
        self.identities = []
        self.down = True
        self.lose_worker_after = None

    def __call__(self, entry):
        self.attempts.append(entry.sequence)
        self.identities.append(entry.identity)
        if self.down:
            raise ConnectionError("destination unavailable")
        self.accepted.setdefault(entry.identity, [record["text"] for record in entry.records])
        if entry.sequence == self.lose_worker_after:
            raise WorkerLost

    def texts(self):
        return [text for texts in self.accepted.values() for text in texts]


def line(text):
    entry = {"type": "user", "message": {"role": "user", "content": text}}
    return (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")


def snapshot(folder):
    return {path.name: path.read_bytes() for path in sorted(Path(folder).glob("*")) if path.is_file()}


class Rig:
    def __init__(self, inputs, root):
        self.inputs = inputs
        self.source = root / inputs["source"]
        self.source.write_bytes(b"")
        self.clock = Clock(inputs["start_clock"])
        self.cursors = CursorStore(root / "cursors")
        self.collector = Collector(self.cursors)
        self.config = SpoolConfig(root / "spool", inputs["max_bytes"], inputs["warn_bytes"])
        self.spool = Spool(self.config, self.clock)
        self.central = Central()

    def accept(self, batch):
        self.spool.append(f"{self.source.name}:{batch.start}:{batch.cursor.offset}", list(batch.records))

    def write(self, text):
        with open(self.source, "ab") as handle:
            handle.write(line(text))

    def step(self, text):
        self.write(text)
        self.collector.poll(self.source, self.accept)
        flushed = self.spool.flush(self.central)
        self.clock.now += self.inputs["tick_seconds"]
        return flushed

    def buffer_until(self, level):
        steps = []
        for text in self.inputs["prompts"]:
            flushed = self.step(text)
            status = self.spool.status()
            steps.append(
                {
                    "flush_error": flushed.error,
                    "level": status.level,
                    "transcript_spool_bytes": status.transcript_spool_bytes,
                    "archive_lag_seconds": status.archive_lag_seconds,
                }
            )
            if status.level == level:
                return steps
        raise AssertionError(f"spool never reached {level}")


def positive(inputs, root):
    rig = Rig(inputs, root)
    steps = rig.buffer_until("warning")
    warned = rig.spool.status()
    assert rig.central.accepted == {}
    assert warned.acknowledged == 0 and warned.pending == list(range(1, warned.buffered + 1))
    assert rig.cursors.load(str(rig.source)).offset == rig.source.stat().st_size
    idle = rig.collector.poll(rig.source, rig.accept)
    assert idle.records == [] and rig.spool.status().pending == warned.pending
    rig.central.down = False
    stop = threading.Event()
    stop.set()
    final = run_flusher(rig.spool, rig.central, stop, 30.0)
    recovered = rig.spool.status()
    expected = inputs["prompts"][: warned.buffered]
    assert rig.central.texts() == expected
    assert len(rig.central.accepted) == warned.buffered
    assert (recovered.acknowledged, recovered.pending, recovered.transcript_spool_bytes) == (warned.buffered, [], 0)
    assert recovered.archive_lag_seconds == 0.0
    return {
        "buffered_steps": steps,
        "warning_sequences": warned.pending,
        "upload_attempts": rig.central.attempts,
        "recovery_uploaded": final.uploaded,
        "central_texts": rig.central.texts(),
        "central_copies": len(rig.central.accepted),
        "acknowledged": recovered.acknowledged,
        "transcript_spool_bytes": recovered.transcript_spool_bytes,
        "archive_lag_seconds": recovered.archive_lag_seconds,
    }


def rejection(inputs, root):
    rig = Rig(inputs, root)
    rig.buffer_until("warning")
    refused = None
    for text in inputs["prompts"][rig.spool.status().buffered :]:
        protected = (snapshot(rig.spool.batches), snapshot(rig.cursors.root), rig.spool.status())
        rig.write(text)
        try:
            rig.collector.poll(rig.source, rig.accept)
        except SpoolFull as error:
            refused = (text, str(error))
            break
        rig.spool.flush(rig.central)
    assert refused is not None
    batches, cursors, before = protected
    after = rig.spool.status()
    assert snapshot(rig.spool.batches) == batches and snapshot(rig.cursors.root) == cursors
    assert after.pending == before.pending and after.pending[0] == 1
    assert refused[0] not in refused[1]
    rig.central.down = False
    flushed = rig.spool.flush(rig.central)
    resumed = rig.collector.poll(rig.source, rig.accept)
    rig.spool.flush(rig.central)
    assert [record["text"] for record in resumed.records] == [refused[0]]
    assert rig.central.texts() == inputs["prompts"][: after.buffered + 1]
    return {
        "refused_error": refused[1],
        "refused_level": after.level,
        "refused_transcript_spool_bytes": after.transcript_spool_bytes,
        "refused_archive_lag_seconds": after.archive_lag_seconds,
        "pending_after_refusal": after.pending,
        "protected_state_unchanged": True,
        "recovery_uploaded": flushed.uploaded,
        "resumed_texts": [record["text"] for record in resumed.records],
        "central_texts": rig.central.texts(),
        "final_acknowledged": rig.spool.status().acknowledged,
    }


def recovery(inputs, root):
    rig = Rig(inputs, root)
    for text in inputs["prompts"][:3]:
        rig.step(text)
    earlier = rig.spool
    rig.central.down = False
    rig.central.lose_worker_after = 2
    with pytest.raises(WorkerLost):
        rig.spool.flush(rig.central)
    (rig.spool.batches / ".00000000000000000004-torn.json.tmp").write_bytes(b'{"sequence": 4, "rec')
    third = next(path for path in rig.spool.batches.glob("*.json") if path.name.startswith("00000000000000000003"))
    third.write_bytes(third.read_bytes().replace("three".encode(), "thr3e".encode()))
    restarted = Spool(rig.config, rig.clock)
    reported = restarted.status()
    assert (reported.acknowledged, reported.pending, reported.corrupt, reported.buffered) == (1, [2], [3], 3)
    rig.central.lose_worker_after = None
    flushed = restarted.flush(rig.central)
    final = restarted.status()
    replays = [identity for sequence, identity in zip(rig.central.attempts, rig.central.identities) if sequence == 2]
    assert len(replays) == 2 and len(set(replays)) == 1
    assert rig.central.texts() == inputs["prompts"][:2]
    assert (flushed.uploaded, flushed.lost) == ([2], [3])
    assert (final.settled, final.acknowledged, final.lost, final.pending) == (3, 2, [3], [])
    stale = earlier.flush(rig.central)
    earlier._acknowledge(rig.spool.batches / "00000000000000000002-stale.json", lost=False)
    assert (stale.uploaded, restarted.status().settled) == ([], 3)
    return {
        "reported_after_loss": {
            "remote_durable_through": reported.acknowledged,
            "local_only": reported.pending,
            "corrupt": reported.corrupt,
            "transcript_spool_bytes": reported.transcript_spool_bytes,
            "archive_lag_seconds": reported.archive_lag_seconds,
        },
        "upload_attempts": rig.central.attempts,
        "replayed_identity_attempts": len(replays),
        "central_texts": rig.central.texts(),
        "replayed_uploaded": flushed.uploaded,
        "lost": final.lost,
        "quarantined": sorted(int(path.name[:20]) for path in restarted.quarantine.glob("*.json")),
        "torn_append_retained_uncounted": (restarted.batches / ".00000000000000000004-torn.json.tmp").exists(),
        "final_settled": final.settled,
        "final_acknowledged": final.acknowledged,
        "stale_flush_uploaded": stale.uploaded,
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as folder:
        observed = CASES[case](inputs, Path(folder))
    return {
        "case": f"T-SV2-SES-03-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        "observed": observed,
    }
