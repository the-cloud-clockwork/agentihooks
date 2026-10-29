"""Replay synthetic Claude transcripts through capture, spool, archive, chunking, embedding and search on a fake clock.

Writers append interleaved in one process so every run of one seed takes the same path; search wall time is the only
measured clock and stays out of the deterministic report.
"""

import argparse
import hashlib
import json
import random
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from hooks.memory import archive, chunking
from hooks.memory.api import search as search_api
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, ingest
from hooks.memory.catalog import ArchiveEntry, Catalog, Conflict, SessionSource
from hooks.memory.collector import Collector, read_batch
from hooks.memory.cursors import CursorConflict, CursorStore
from hooks.memory.embeddings import EmbeddingModel
from hooks.memory.index_worker import IndexWorker, Limits, Target
from hooks.memory.search import SessionSearch, tokens
from hooks.memory.spool import Spool, SpoolConfig, SpoolFull

WORKLOAD = Path(__file__).parent / "workload.json"
GENERATION = "g1"
NEEDLE = "spoolwatermark"
EXPECTED_FLUSH_ERRORS = ("", "CentralUnavailable", "LostAcknowledgement")
PEAKS = ("spool_pending_batches", "archive_lag_seconds", "embedding_pending_chunks", "embedding_backlog_age_seconds")
WORDS = (
    "archive backlog batch build catalog chunk cursor drain embed evidence flush grant index ingest lease ledger "
    "marker offset pending project query record replay search session source spool tick transcript upload vector"
).split()


class CentralUnavailable(Exception):
    pass


class LostAcknowledgement(Exception):
    pass


def line(entry: dict) -> bytes:
    return json.dumps(entry, ensure_ascii=False).encode() + b"\n"


def byte_range(entry) -> tuple[int, int]:
    start, end = entry.identity.rsplit(":", 2)[1:]
    return int(start), int(end)


def percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "max": None}

    def rank(share: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))], 6)

    return {"count": len(ordered), "p50": rank(0.5), "p95": rank(0.95), "max": round(ordered[-1], 6)}


def found(answer: dict) -> bool:
    return any(item["explain"]["lexical"] for item in answer.get("results", []))


class HashEmbedder:
    def __init__(self, model: EmbeddingModel):
        self.model, self.texts = model, Counter()

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.update(texts)
        return [self.vector(text) for text in texts]

    def vector(self, text: str) -> list[float]:
        values = [0.0] * self.model.dimension
        for word in tokens(text):
            values[int(hashlib.sha256(word.encode()).hexdigest()[:8], 16) % self.model.dimension] += 1.0
        return values


@dataclass
class Writer:
    name: str
    uid: str
    project: str
    path: Path
    rng: random.Random
    carry: bytes = b""
    size: int = 0
    completed: dict = field(default_factory=dict)
    markers: list = field(default_factory=list)

    def entries(self, tick: int) -> list[dict]:
        marker = f"m{self.uid[-8:]}t{tick}x"
        self.markers.append((tick, marker))
        words = " ".join(self.rng.choice(WORDS) for _ in range(12))
        reply = " ".join(self.rng.choice(WORDS) for _ in range(18))
        return [
            {"type": "user", "message": {"role": "user", "content": f"{marker} {NEEDLE} {words}"}},
            {"type": "assistant", "message": {"role": "assistant", "content": f"{reply} {self.name}"}},
        ]

    def write(self, data: bytes, now: float) -> None:
        with open(self.path, "ab") as handle:
            handle.write(data)
        start, self.size = self.size, self.size + len(data)
        for index, byte in enumerate(data):
            if byte == ord("\n"):
                self.completed[start + index + 1] = now

    def append(self, tick: int, now: float, split: bool) -> None:
        data = self.carry + b"".join(line(entry) for entry in self.entries(tick))
        last = data.rfind(b"\n", 0, len(data) - 1) + 1
        cut = last + (len(data) - last) // 2 if split else len(data)
        self.write(data[:cut], now)
        self.carry = data[cut:]

    def finish(self, now: float) -> None:
        if self.carry:
            self.write(self.carry, now)
            self.carry = b""


class Execution:
    def __init__(self, writer: Writer, root: Path, grant: Grant, config: SpoolConfig, clock):
        self.writer, self.root, self.grant, self.config, self.clock = writer, root, grant, config, clock
        self.acked: list[tuple[int, int]] = []
        self.delivered: list = []
        self.open()

    def open(self, max_bytes: int | None = None) -> None:
        self.cursors = CursorStore(self.root / "cursors")
        self.spool = Spool(replace(self.config, max_bytes=max_bytes) if max_bytes else self.config, self.clock)
        self.collector = Collector(self.cursors)

    def cursor(self):
        return self.cursors.load(str(self.writer.path))


class Central:
    def __init__(self, root: Path, spec: dict):
        self.root, self.spec, self.domain = root, spec, spec["corpus"]
        self.indexer = Grant(spec["principals"]["indexer"], self.domain, frozenset(spec["projects"].values()))
        self.policy = chunking.Policy(**spec["policy"])
        self.down = False
        self.open()

    def open(self) -> None:
        self.catalog = Catalog(self.root / "catalog.sqlite3")
        self.store = archive.ObjectStore(self.root / "objects")
        self.chunks = chunking.ChunkIndex(self.catalog)
        self.worker = IndexWorker(self.catalog, Limits(**self.spec["limits"]))
        self.searcher = SessionSearch(self.catalog)

    def restart(self) -> None:
        self.catalog.close()
        self.open()

    def register(self, writer: Writer) -> None:
        spec = self.spec
        source = SessionSource(self.domain, writer.uid, GENERATION, spec["provider"], writer.name, spec["retention"])
        self.catalog.register(source)

    def upload(self, execution: Execution, entry) -> dict:
        if self.down:
            raise CentralUnavailable("central session service unavailable")
        writer = execution.writer
        start, end = byte_range(entry)
        lines = [line(record["raw"]) for record in entry.records]
        if sum(map(len, lines)) != end - start:
            raise ValueError("spooled records do not cover their byte range")
        digest = self.store.put(b"".join(lines))
        archive.acknowledge(
            self.catalog, self.store, ArchiveEntry(self.domain, writer.uid, GENERATION, start, end, digest)
        )
        base = writer.path.read_bytes()[:start].count(b"\n")
        events, cursor = [], start
        for index, (record, encoded) in enumerate(zip(entry.records, lines, strict=True)):
            normalized = {key: value for key, value in record.items() if key != "raw"}
            item = {"sequence": base + index + 1, "byte_start": cursor, "byte_end": cursor + len(encoded)}
            events.append({**item, "project_id": writer.project, "record": normalized})
            cursor += len(encoded)
        body = {
            "schema_version": SCHEMA_VERSION,
            "batch_id": f"capture:{writer.uid}:{start}:{end}",
            "access_domain": self.domain,
            "session_uid": writer.uid,
            "source_generation": GENERATION,
            "events": events,
        }
        result = ingest(self.catalog, execution.grant, body)
        if result["state"] not in ("committed", "replayed"):
            raise Conflict(result["error"]["class"])
        return result

    def index(self, writer: Writer, now: float) -> None:
        self.chunks.build(self.indexer, chunking.ChunkScope(self.domain, writer.uid), self.policy)
        target = Target(self.domain, writer.uid, self.policy.version, self.spec["index_generation"])
        self.worker.enqueue(self.indexer, target, now)

    def activate_chunks(self, writer: Writer) -> None:
        self.chunks.validate(self.store, self.indexer, self.domain, writer.uid, self.policy.version)
        self.chunks.activate(self.domain, writer.uid, self.policy.version, 0)

    def protected(self) -> dict:
        kept = {name: rows for name, rows in self.catalog.snapshot().items() if name != "ingest_counter"}
        return {**kept, **self.chunks.snapshot(), **self.worker.snapshot()}


def session_uid(seed: int, kind: str, index: int) -> str:
    return f"session:{hashlib.sha256(f'{seed}:{kind}:{index}'.encode()).hexdigest()[:32]}"


@dataclass(frozen=True)
class Variant:
    """``overload_spool_bytes`` shrinks every writer spool while central is down; ``drop_older_than`` makes the
    uploader discard aged backlog to catch up, which verify must grade as a failed run."""

    overload_spool_bytes: int | None = None
    drop_older_than: float | None = None
    restart_at: int | None = None


class Run:
    def __init__(self, spec: dict, seed: int, root: Path, variant: Variant = Variant()):
        self.spec, self.seed, self.root, self.variant, self.now = spec, seed, root, variant, 0.0
        self.central = Central(root / "central", spec)
        self.embedder = HashEmbedder(EmbeddingModel(**spec["model"]))
        self.query_embedder = HashEmbedder(self.embedder.model)
        projects = [spec["projects"][name] for name in ("granted", "other", "restricted")]
        self.writers = [self.execution("writer", index, projects) for index in range(spec["writers"])]
        self.background = [self.execution("background", item, projects) for item in range(spec["background_sessions"])]
        self.lost_cursor = {tuple(item) for item in spec["duplicates"]["lost_cursor_commit"]}
        self.lost_ack = {tuple(item) for item in spec["duplicates"]["lost_acknowledgement"]}
        self.ack_latency: list[float] = []
        self.overload_latency: list[float] = []
        self.search_seconds: list[float] = []
        self.visible: dict = {}
        self.series: list = []
        self.saved_cursor = None
        self.restarted: dict = {}
        self.counts = Counter()

    def execution(self, kind: str, index: int, projects: list[str]) -> Execution:
        uid = session_uid(self.seed, kind, index)
        folder = self.root / "executions" / f"{kind}-{index}"
        folder.mkdir(parents=True)
        project = projects[index % len(projects)]
        writer = Writer(f"{kind}-{index}", uid, project, folder / "transcript.jsonl", random.Random(f"{uid}"))
        writer.path.touch()
        self.central.register(writer)
        grant = Grant(f"execution:{writer.name}", self.central.domain, frozenset({project}))
        limits = self.spec["spool"]
        config = SpoolConfig(folder / "spool", limits["max_bytes"], limits["warn_bytes"])
        return Execution(writer, folder, grant, config, lambda: self.now)

    @property
    def executions(self) -> list[Execution]:
        return self.writers + self.background

    def capture(self, execution: Execution, tick: int, index: int) -> None:
        path = execution.writer.path
        before = execution.cursor()
        try:
            if (index, tick) in self.lost_cursor:
                batch = read_batch(path, before)
                if batch.records:
                    execution.spool.accept(batch)
                    self.counts["cursor_commits_lost"] += 1
            execution.collector.poll(path, execution.spool.accept)
        except SpoolFull:
            self.counts["spool_full_refusals"] += 1
            self.counts["cursor_moved_on_refusal"] += execution.cursor() != before

    def flush(self, execution: Execution, tick: int, index: int) -> None:
        lose = [(index, tick) in self.lost_ack]

        def upload(entry) -> None:
            if self.variant.drop_older_than is not None and self.now - entry.created > self.variant.drop_older_than:
                self.counts["dropped_batches"] += 1
                return
            self.central.upload(execution, entry)
            if lose[0]:
                lose[0] = False
                self.counts["acknowledgements_lost"] += 1
                raise LostAcknowledgement("acknowledgement lost after the central commit")
            execution.acked.append(byte_range(entry))
            execution.delivered.append(entry)

        before = len(execution.acked)
        result = execution.spool.flush(upload)
        if result.error not in EXPECTED_FLUSH_ERRORS:
            raise RuntimeError(f"{execution.writer.name} flush failed with {result.error}")
        for start, end in execution.acked[before:]:
            self.acknowledged(execution.writer, start, end)
        if execution.acked[before:]:
            self.central.index(execution.writer, self.now)

    def acknowledged(self, writer: Writer, start: int, end: int) -> None:
        window, seconds = self.spec["overload"], self.spec["tick_seconds"]
        for byte_end, written in writer.completed.items():
            if start < byte_end <= end:
                self.ack_latency.append(self.now - written)
                if window["from"] * seconds <= written < window["until"] * seconds:
                    self.overload_latency.append(self.now - written)

    def drain(self) -> dict:
        central = self.central
        return central.worker.drain(self.embedder, central.domain, self.spec["index_generation"], self.now)

    def warmup(self) -> None:
        generation = self.spec["index_generation"]
        self.central.worker.register(self.central.domain, generation, self.embedder.model)
        for execution in self.executions:
            turns = self.spec["warmup_turns"] if execution in self.writers else 1
            for turn in range(turns):
                execution.writer.append(turn - turns, self.now, False)
            self.capture(execution, 0, -1)
            self.flush(execution, 0, -1)
            self.central.activate_chunks(execution.writer)
        self.ack_latency.clear()
        while self.drain()["state"] != "drained":
            pass
        self.central.worker.activate(self.central.domain, generation, 0)

    def set_overload(self, tick: int) -> None:
        window = self.spec["overload"]
        down = window["from"] <= tick < window["until"]
        if down != self.central.down and self.variant.overload_spool_bytes:
            for execution in self.writers:
                execution.open(self.variant.overload_spool_bytes if down else None)
        self.central.down = down
        self.central.worker.set_paused(self.central.domain, self.spec["index_generation"], down)

    def tick(self, tick: int, writing: bool) -> None:
        self.now = tick * self.spec["tick_seconds"]
        self.set_overload(tick)
        for index, execution in enumerate(self.writers):
            if writing:
                split = (tick + index) % self.spec["split_every"] == 0 and tick < self.spec["ticks"]
                execution.writer.append(tick, self.now, split)
            else:
                execution.writer.finish(self.now)
            self.capture(execution, tick, index)
        if tick % self.spec["flush_every"] == 0 or not writing:
            for index, execution in enumerate(self.writers):
                self.flush(execution, tick, index)
        drained = self.drain()
        if tick % self.spec["search_every"] == 0:
            self.search()
        self.observe(tick, drained)

    def observe(self, tick: int, drained: dict) -> None:
        statuses = [execution.spool.status() for execution in self.writers]
        self.series.append(
            {
                "tick": tick,
                "central": "unavailable" if self.central.down else "available",
                "embeddings": drained["state"],
                "spool_pending_batches": sum(len(status.pending) for status in statuses),
                "archive_lag_seconds": max(status.archive_lag_seconds for status in statuses),
                "embedding_pending_chunks": drained["pending"],
                "embedding_backlog_age_seconds": drained["embedding_backlog_age_seconds"],
            }
        )

    def ask(self, grant: Grant, query: str, projects=None, limit: int = 10, domain=None) -> dict:
        body = {"schema_version": SCHEMA_VERSION, "access_domain": domain or self.central.domain, "query": query}
        body = {**body, "limit": limit, "context": 0, **({"projects": projects} if projects else {})}
        started = time.perf_counter()
        answer = search_api.search(self.central.searcher, grant, body, self.query_embedder)
        self.search_seconds.append(time.perf_counter() - started)
        return answer

    def grants(self) -> tuple[Grant, Grant]:
        projects, principal = self.spec["projects"], self.spec["principals"]["searcher"]
        narrow = Grant(principal, self.central.domain, frozenset({projects["granted"]}))
        wide = Grant(principal, self.central.domain, frozenset({projects["granted"], projects["other"]}))
        return narrow, wide

    def scoped(self, answer: dict, allowed: set[str]) -> None:
        self.counts["searches_answered"] += answer["state"] == "answered"
        for item in answer.get("results", []):
            self.counts["out_of_scope_results"] += not set(item["project_ids"]) <= allowed

    def search(self) -> None:
        narrow, wide = self.grants()
        projects = self.spec["projects"]
        granted, both = {projects["granted"]}, {projects["granted"], projects["other"]}
        for answer, allowed in (
            (self.ask(narrow, NEEDLE, limit=50), granted),
            (self.ask(wide, NEEDLE, [projects["granted"]], 50), granted),
            (self.ask(wide, NEEDLE, limit=50), both),
        ):
            self.scoped(answer, allowed)
            self.counts["needle_hits"] += len(answer.get("results", []))
        refused = self.ask(wide, NEEDLE, [projects["restricted"]])
        self.counts["restricted_searches_refused"] += refused["state"] == "rejected"
        for execution in self.writers:
            if execution.writer.project == projects["granted"]:
                self.markers(narrow, execution.writer)

    def markers(self, grant: Grant, writer: Writer) -> None:
        for tick, marker in writer.markers:
            if marker in self.visible or tick < 0:
                continue
            answer = self.ask(grant, f'"{marker}"')
            self.scoped(answer, set(grant.projects))
            if found(answer):
                self.visible[marker] = self.now - tick * self.spec["tick_seconds"]

    def settled(self) -> bool:
        pending = self.central.worker.pending(self.central.domain, self.spec["index_generation"], self.now)
        writers = all(not execution.writer.carry for execution in self.writers)
        spools = all(not execution.spool.status().pending for execution in self.writers)
        return writers and spools and not pending["pending"]

    def load(self) -> dict:
        self.warmup()
        for tick in range(1, self.spec["ticks"] + 1):
            if tick == self.variant.restart_at:
                self.restart()
            self.tick(tick, True)
            if tick == 1:
                self.saved_cursor = self.writers[0].cursor()
        for tick in range(self.spec["ticks"] + 1, self.spec["ticks"] + self.spec["drain_ticks"] + 1):
            self.tick(tick, False)
            if tick % self.spec["search_every"] == 0 and self.settled():
                return {"drained_at_tick": tick}
        return {"drained_at_tick": None}

    def pending_batches(self) -> list[list[int]]:
        return [execution.spool.status().pending for execution in self.writers]

    def restart(self) -> None:
        before = self.pending_batches()
        self.central.restart()
        for execution in self.executions:
            execution.open(self.variant.overload_spool_bytes if self.central.down else None)
        self.restarted = {"spool_backlog_survived": self.pending_batches() == before, "batches": sum(map(len, before))}

    def stale_cursor(self) -> dict:
        execution = self.writers[0]
        current = execution.cursor()
        try:
            execution.cursors.commit(self.saved_cursor)
        except CursorConflict as error:
            return {"refused": str(error), "cursor_unchanged": execution.cursor() == current}
        return {"refused": None, "cursor_unchanged": execution.cursor() == current}

    def verify(self) -> dict:
        central, failures, lines, hashes = self.central, [], 0, []
        for execution in self.executions:
            data = execution.writer.path.read_bytes()
            complete = data[: data.rfind(b"\n") + 1]
            lines += complete.count(b"\n")
            failures += self.verify_session(execution, complete)
            hashes.append(hashlib.sha256(complete).hexdigest())
        pending = central.worker.pending(central.domain, self.spec["index_generation"], self.now)["pending"]
        if pending:
            failures.append(f"{pending} chunks still wait for embedding")
        repeated = sorted(text for text, seen in self.embedder.texts.items() if seen > 1)
        if repeated:
            failures.append(f"{len(repeated)} unchanged chunk texts were embedded more than once")
        events = [central.catalog.events(central.domain, item.writer.uid, GENERATION) for item in self.executions]
        return {
            "complete": not failures,
            "failures": failures,
            "sessions": len(self.executions),
            "transcript_lines": lines,
            "authoritative_events": sum(map(len, events)),
            "source_sha256": hashlib.sha256("".join(hashes).encode()).hexdigest(),
            "duplicate_events_total": central.catalog.duplicate_events_total(central.domain),
            "embedded_texts": sum(self.embedder.texts.values()),
            "re_embedded_texts": len(repeated),
        }

    def verify_session(self, execution: Execution, complete: bytes) -> list[str]:
        central, writer, failures = self.central, execution.writer, []
        count = complete.count(b"\n")
        events = central.catalog.events(central.domain, writer.uid, GENERATION)
        if [event["sequence"] for event in events] != list(range(1, count + 1)):
            failures.append(f"{writer.name}: {len(events)} events for {count} complete lines")
        reference = {"access_domain": central.domain, "session_uid": writer.uid, "source_generation": GENERATION}
        try:
            whole = {**reference, "byte_start": 0, "byte_end": len(complete)}
            archived = archive.source_range(central.catalog, central.store, central.indexer, whole)
        except archive.EvidenceIntegrityError as error:
            failures.append(f"{writer.name}: archive {error}")
        else:
            if archived != complete:
                failures.append(f"{writer.name}: archived bytes differ from the transcript")
        status = execution.spool.status()
        if status.pending or status.lost or status.corrupt:
            failures.append(f"{writer.name}: spool still holds {len(status.pending)} batches")
        target = Target(central.domain, writer.uid, central.policy.version, self.spec["index_generation"])
        if not failures and central.worker.coverage(central.indexer, target)["state"] != "indexed":
            failures.append(f"{writer.name}: chunks without a current vector")
        return failures

    def invisible_markers(self) -> int:
        granted = self.spec["projects"]["granted"]
        writers = [execution.writer for execution in self.writers if execution.writer.project == granted]
        return sum(tick >= 0 and marker not in self.visible for writer in writers for tick, marker in writer.markers)

    def metrics(self) -> dict:
        return {
            "ingest_ack_latency_seconds": {
                "clock": "fake",
                "all": percentiles(self.ack_latency),
                "written_during_overload": percentiles(self.overload_latency),
            },
            "search_latency_seconds": {
                "clock": "wall, asserted by the wall clock test and printed by the measure command, not recorded here",
                "searches": len(self.search_seconds),
            },
            "marker_visibility_lag_seconds": {"clock": "fake", **percentiles(list(self.visible.values()))},
            "markers_never_visible": self.invisible_markers(),
            "peak": {name: max(row[name] for row in self.series) for name in PEAKS},
        }


def load_spec(path: Path = WORKLOAD) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def measure(seed: int, spec: dict) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        run = Run(spec, seed, Path(folder))
        started = time.perf_counter()
        drained = run.load()
        elapsed = time.perf_counter() - started
        verdict = run.verify()
    return {
        "seed": seed,
        "wall_seconds": round(elapsed, 3),
        **drained,
        "verdict": {"complete": verdict["complete"], "failures": verdict["failures"]},
        **run.metrics(),
        "search_latency_seconds": {"clock": "wall", **percentiles(run.search_seconds)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, action="append")
    args = parser.parse_args(argv)
    spec = load_spec()
    reports = [measure(seed, spec) for seed in args.seed or spec["seeds"]]
    json.dump(reports, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if all(report["verdict"]["complete"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
