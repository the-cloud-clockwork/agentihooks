import hashlib
import json
import tempfile
from pathlib import Path

from hooks.memory.catalog import Catalog
from hooks.memory.embeddings import EmbeddingModel, TransientEmbeddingError
from hooks.memory.index_worker import BACKLOG, IndexWorker, Limits, Target
from tests.sv2_idx02_cases import Session, grant, history, refusal

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/session-embeddings.json"
EVIDENCE_CLASS = (
    "mocked: an in-process caller, a SQLite catalog file, a local content addressed object folder and a deterministic "
    "hash embedder stand in for the session service, the central database, the archive bucket and the embedding "
    "provider; no model, HTTP server or Pod runs"
)


class FixtureEmbedder:
    def __init__(self, model, width=None, fail_calls=(), before=None):
        self.model, self.width, self.fail_calls, self.before = model, width or model.dimension, set(fail_calls), before
        self.calls, self.texts = 0, []

    def embed(self, texts):
        self.calls += 1
        if self.before:
            self.before()
        if self.calls in self.fail_calls:
            raise TransientEmbeddingError("429 rate limited")
        self.texts += texts
        return [vector(text, self.width) for text in texts]


def vector(text, width):
    digest = hashlib.sha256(text.encode()).digest()
    return [round(digest[index] / 255, 6) for index in range(width)]


def model(inputs, name="model"):
    return EmbeddingModel(**inputs[name])


def target(inputs, generation="index_generation", corpus=None):
    version = inputs["policies"]["current"]["version"]
    return Target(corpus or inputs["grant"]["corpus"], inputs["session_uid"], version, inputs[generation])


class Index:
    def __init__(self, inputs, root):
        self.inputs, self.session = inputs, Session(inputs, root)
        self.domain, self.generation = inputs["grant"]["corpus"], inputs["index_generation"]
        self.open()

    def open(self):
        self.worker = IndexWorker(self.session.catalog, Limits(**self.inputs["limits"]))

    def restart(self):
        self.session.restart()
        self.open()

    def enqueue(self, now, generation="index_generation"):
        return self.worker.enqueue(grant(self.inputs), target(self.inputs, generation), now)

    def drain(self, embedder, now, generation="index_generation"):
        return self.worker.drain(embedder, self.domain, self.inputs[generation], now)

    def coverage(self, generation="index_generation"):
        return self.worker.coverage(grant(self.inputs), target(self.inputs, generation))

    def vectors(self, generation="index_generation"):
        return self.worker.vectors(grant(self.inputs), target(self.inputs, generation))

    def work(self):
        return [row[2:] for row in self.worker.snapshot()["embedding_work"]]

    def protected(self):
        return {**self.session.protected(), **self.worker.snapshot()}


def ids(vectors):
    return sorted(item["chunk_id"] for item in vectors)


def positive(inputs, root):
    earlier, appended = history(inputs)
    clock = inputs["clock"]
    index = Index(inputs, root)
    index.session.append(earlier)
    index.session.build()
    index.worker.register(index.domain, index.generation, model(inputs))
    first = index.enqueue(clock["queued"])
    queued_age = index.worker.pending(index.domain, index.generation, clock["outage"])
    embedder = FixtureEmbedder(model(inputs))
    drained = index.drain(embedder, clock["outage"])
    before = index.vectors()
    first_calls = embedder.calls
    index.session.append(appended)
    grown = index.session.build()
    second = index.enqueue(clock["appended"])
    appended_drain = index.drain(embedder, clock["appended"])
    after = index.vectors()
    new_texts = embedder.texts[len(before) :]
    unchanged = all(item in after for item in before)
    assert sorted(second["queued"]) == sorted(grown["inserted"]) == sorted(appended_drain["embedded"])
    assert len(new_texts) == len(grown["inserted"]) and unchanged and second["dropped"] == []
    assert all(
        item["model"] == inputs["model"]["model"] and len(item["vector"]) == inputs["model"]["dimension"]
        for item in after
    )
    coverage = index.coverage()
    assert coverage["state"] == "indexed"
    revision = index.worker.activate(index.domain, index.generation, 0)
    return {
        "first_enqueue": {"queued": len(first["queued"]), "embedded": first["embedded"]},
        "backlog_before_drain": queued_age,
        "first_drain": {key: drained[key] for key in ("state", "pending", BACKLOG)},
        "first_drain_embedded": len(drained["embedded"]),
        "first_drain_provider_calls": first_calls,
        "appended_build_inserted": sorted(grown["inserted"]),
        "appended_enqueue": {"queued": sorted(second["queued"]), "embedded": second["embedded"]},
        "appended_drain": {key: appended_drain[key] for key in ("state", "pending", BACKLOG)},
        "appended_provider_texts": len(new_texts),
        "earlier_vectors_unchanged": unchanged,
        "coverage": coverage,
        "active": {**index.worker.active(index.domain), "revision_returned": revision},
        "vector_ids": ids(after),
    }


def rejection(inputs, root):
    clock = inputs["clock"]
    index = Index(inputs, root)
    index.session.append(inputs["turns"])
    index.session.build()
    index.worker.register(index.domain, index.generation, model(inputs))
    index.enqueue(clock["queued"])
    before = index.protected()
    wrong_width = FixtureEmbedder(model(inputs), inputs["mismatched_width"])
    other = FixtureEmbedder(model(inputs, "other_family"))
    rebound = EmbeddingModel(inputs["model"]["model"], inputs["model"]["dimension"] + 1)
    foreign = grant(inputs, inputs["foreign_corpus"])
    attempts = {
        "other_model_family": refusal(lambda: index.drain(other, clock["outage"])),
        "dimension_mismatch": refusal(lambda: index.drain(wrong_width, clock["outage"])),
        "rebind_generation_dimension": refusal(lambda: index.worker.register(index.domain, index.generation, rebound)),
        "unbounded_model": refusal(lambda: index.worker.register(index.domain, "fixture-empty", EmbeddingModel("", 0))),
        "unregistered_generation": refusal(
            lambda: index.worker.enqueue(grant(inputs), target(inputs, "next_index_generation"), clock["outage"])
        ),
        "foreign_corpus_enqueue": refusal(
            lambda: index.worker.enqueue(foreign, target(inputs, corpus=inputs["foreign_corpus"]), clock["outage"])
        ),
        "foreign_corpus_vectors": refusal(
            lambda: index.worker.vectors(foreign, target(inputs, corpus=inputs["foreign_corpus"]))
        ),
        "activate_incomplete": refusal(lambda: index.worker.activate(index.domain, index.generation, 0)),
    }
    untouched = index.protected() == before
    assert untouched and all(item["error"] for item in attempts.values())
    assert other.calls == 0 and wrong_width.calls == 1
    diagnostics = json.dumps(attempts, ensure_ascii=False)
    for hidden in ("rate limited", "personal-corpus", "worker-b", "café", "fixture-hash-embedder"):
        assert hidden not in diagnostics
    corrected = index.drain(FixtureEmbedder(model(inputs)), clock["recovered"])
    assert corrected["state"] == "drained"
    return {
        "rejected": attempts,
        "provider_calls": {"other_model_family": other.calls, "dimension_mismatch": wrong_width.calls},
        "protected_state_unchanged": untouched,
        "pending_after_rejections": index.worker.pending(index.domain, index.generation, clock["outage"]),
        "corrected": {key: corrected[key] for key in ("state", "pending", BACKLOG)},
        "corrected_embedded": len(corrected["embedded"]),
        "coverage": index.coverage(),
    }


def outage(index, inputs):
    clock = inputs["clock"]
    index.session.append(history(inputs)[0])
    index.session.build()
    index.worker.register(index.domain, index.generation, model(inputs))
    queued = index.enqueue(clock["queued"])
    limited = FixtureEmbedder(model(inputs), fail_calls={inputs["rate_limited_call"]})
    report = index.drain(limited, clock["outage"])
    assert report["state"] == "deferred" and report["embedded"] and report["failed"]
    return queued, report


def stale_attempt(index, inputs, root):
    clock = inputs["clock"]
    index.session.append(history(inputs)[1])
    grown = index.session.build()
    index.enqueue(clock["appended"])
    rival = IndexWorker(Catalog(root / "central/catalog.sqlite3"), Limits(**inputs["limits"]))
    winner = {}

    def race():
        winner.update(rival.drain(FixtureEmbedder(model(inputs)), index.domain, index.generation, clock["appended"]))

    late = index.drain(FixtureEmbedder(model(inputs), before=race), clock["appended"])
    rival.catalog.close()
    assert late["embedded"] == [] and sorted(winner["embedded"]) == sorted(grown["inserted"])
    return {"late_attempt_written": late["embedded"], "rival_written": sorted(winner["embedded"])}


def rollback(index, inputs, active_vectors):
    clock = inputs["clock"]
    revision = index.worker.activate(index.domain, index.generation, 0)
    following = inputs["next_index_generation"]
    index.worker.register(index.domain, following, model(inputs, "next_model"))
    index.enqueue(clock["appended"], "next_index_generation")
    nxt = FixtureEmbedder(model(inputs, "next_model"), fail_calls={2})
    partial = index.drain(nxt, clock["appended"], "next_index_generation")
    refused = refusal(lambda: index.worker.activate(index.domain, following, revision))
    index.worker.set_paused(index.domain, following, True)
    paused = index.drain(nxt, clock["appended"], "next_index_generation")
    kept = (
        index.vectors() == active_vectors and index.worker.active(index.domain)["index_generation"] == index.generation
    )
    assert refused["error"] and paused["state"] == "paused" and nxt.calls == 2 and kept
    return {
        "active": index.worker.active(index.domain),
        "new_generation_partial": {key: partial[key] for key in ("state", "pending")},
        "activate_incomplete_new_generation": refused,
        "paused_drain": {key: paused[key] for key in ("state", "embedded", "pending")},
        "previous_generation_vectors_kept": kept,
    }


def recovery(inputs, root):
    clock = inputs["clock"]
    index = Index(inputs, root)
    queued, report = outage(index, inputs)
    incomplete = index.coverage()
    work = index.work()
    assert incomplete["state"] == "incomplete"
    index.restart()
    replay = index.enqueue(clock["restart"])
    assert replay["queued"] == [] and replay["embedded"] == len(report["embedded"])
    age = index.worker.pending(index.domain, index.generation, clock["recovered"])
    recovered = FixtureEmbedder(model(inputs))
    resumed = index.drain(recovered, clock["recovered"])
    assert set(resumed["embedded"]).isdisjoint(report["embedded"])
    assert recovered.calls * inputs["limits"]["batch_size"] >= len(recovered.texts) == len(resumed["embedded"])
    again = index.drain(recovered, clock["recovered"])
    assert again["embedded"] == [] and again["state"] == "drained"
    raced = stale_attempt(index, inputs, root)
    active_vectors = index.vectors()
    events_before = index.session.catalog.events(index.domain, inputs["session_uid"], inputs["source_generation"])
    rehearsal = rollback(index, inputs, active_vectors)
    events_after = index.session.catalog.events(index.domain, inputs["session_uid"], inputs["source_generation"])
    assert events_before == events_after
    return {
        "queued": len(queued["queued"]),
        "outage_drain": {key: report[key] for key in ("state", "pending", BACKLOG)},
        "outage_embedded": len(report["embedded"]),
        "outage_failed": len(report["failed"]),
        "work_after_outage": work,
        "coverage_during_outage": incomplete,
        "replayed_enqueue": replay,
        "backlog_before_recovery": age,
        "recovered_drain": {key: resumed[key] for key in ("state", "pending", BACKLOG)},
        "recovered_provider_texts": len(recovered.texts),
        "recovered_reembedded_earlier": sorted(set(resumed["embedded"]) & set(report["embedded"])),
        "replayed_drain": {key: again[key] for key in ("state", "embedded", "pending")},
        "coverage_after_recovery": index.coverage(),
        "stale_attempt": raced,
        "rollback_rehearsal": {**rehearsal, "source_events_unchanged": events_before == events_after},
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as folder:
        observed = CASES[case](inputs, Path(folder))
    return {
        "case": f"T-SV2-IDX-03-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        "observed": observed,
    }
