import hashlib
import json
import tempfile
from pathlib import Path

from hooks.memory import archive
from hooks.memory.api import search as api
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant
from hooks.memory.catalog import Catalog
from hooks.memory.embeddings import EmbeddingModel, TransientEmbeddingError
from hooks.memory.index_worker import IndexWorker, Limits, Target
from hooks.memory.search import SessionSearch, relevance, tokens
from tests.sv2_idx02_cases import Session, refusal

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/session-search.json"
EVIDENCE_CLASS = (
    "mocked: an in-process caller, a SQLite catalog file, a local content addressed object folder and a deterministic "
    "concept embedder stand in for the session service, the central database, the archive bucket and the embedding "
    "provider; no model, HTTP server, vector database or Pod runs"
)
HIDDEN = ("antoncore-private", "personal-corpus", "agentibrain-kernel", "database is locked", "search-kernel")


class ConceptEmbedder:
    def __init__(self, model, concepts, down=False):
        self.model, self.concepts, self.down, self.calls = model, concepts, down, 0

    def embed(self, texts):
        self.calls += 1
        if self.down:
            raise TransientEmbeddingError("vector service unavailable")
        return [self.vector(text) for text in texts]

    def vector(self, text):
        words = tokens(text)
        return [float(sum(word in concept for word in words)) for concept in self.concepts]


def grant(inputs, projects=None, corpus=None):
    data = inputs["grant"]
    return Grant(data["principal"], corpus or data["corpus"], frozenset(projects or data["projects"]))


def embedder(inputs, name="model", down=False):
    return ConceptEmbedder(EmbeddingModel(**inputs[name]), [set(concept) for concept in inputs["concepts"]], down)


def session_inputs(inputs, item):
    keys = ("session_uid", "source_generation", "origin")
    fields = {key: item[key] for key in keys}
    projects = {**inputs["grant"], "projects": [item["project"]]}
    return {**inputs, **fields, "parent_session_uid": None, "grant": projects}


def request(inputs, query, **fields):
    body = {"schema_version": SCHEMA_VERSION, "access_domain": inputs["grant"]["corpus"], "query": query}
    return {**body, **fields}


class Corpus:
    def __init__(self, inputs, root):
        self.inputs, self.root = inputs, root
        self.sessions = {item["name"]: Session(session_inputs(inputs, item), root) for item in inputs["sessions"]}
        for item in inputs["sessions"]:
            session = self.sessions[item["name"]]
            session.append(item["turns"])
            session.build()
            session.validate()
            session.activate("current", 0)
        self.open()

    def open(self):
        self.catalog = Catalog(self.root / "central/catalog.sqlite3")
        self.searcher = SessionSearch(self.catalog)
        self.worker = IndexWorker(self.catalog, Limits(**self.inputs["limits"]))

    def restart(self):
        self.catalog.close()
        self.open()

    def index(self, generation="index_generation", model="model"):
        domain, name, clock = self.inputs["grant"]["corpus"], self.inputs[generation], self.inputs["clock"]
        self.worker.register(domain, name, EmbeddingModel(**self.inputs[model]))
        version = self.inputs["policies"]["current"]["version"]
        for item in self.inputs["sessions"]:
            target = Target(domain, item["session_uid"], version, name)
            self.worker.enqueue(grant(self.inputs), target, clock["queued"])
        drained = self.worker.drain(embedder(self.inputs, model), domain, name, clock["drained"])
        assert drained["state"] == "drained"
        return self.worker.activate(domain, name, self.worker.active(domain)["revision"])

    def labels(self):
        named = {}
        for item in self.inputs["sessions"]:
            chunks = self.sessions[item["name"]].chunks("current")
            turns = list(dict.fromkeys(chunk["turn_id"] for chunk in chunks))
            for chunk in chunks:
                named[f"{item['name']}:{turns.index(chunk['turn_id'])}:{chunk['kind']}"] = chunk["chunk_id"]
        return named

    def ask(self, body, granted=None, provider=None):
        return api.search(self.searcher, granted or grant(self.inputs), body, provider)

    def protected(self):
        kept = {name: rows for name, rows in self.catalog.snapshot().items() if name != "ingest_counter"}
        return {**kept, **self.worker.chunks.snapshot(), **self.worker.snapshot()}

    def rejections(self):
        return self.searcher.scope_rejections_total(self.inputs["grant"]["corpus"])


def hits(answer):
    return [item["chunk_id"] for item in answer.get("results", [])]


def named(labels, answer):
    by_id = {value: key for key, value in labels.items()}
    return [by_id[chunk_id] for chunk_id in hits(answer)]


def evidence_bytes(corpus, result):
    store = archive.ObjectStore(corpus.root / "central/objects")
    return b"".join(archive.source_range(corpus.catalog, store, grant(corpus.inputs), ref) for ref in result["locator"])


def summary(answer, labels):
    return {
        "state": answer["state"],
        "mode": answer["mode"],
        "vector": answer["vector"],
        "results": [
            {
                "label": named(labels, {"results": [item]})[0],
                "explain": item["explain"],
                "context": context(item, labels),
            }
            for item in answer["results"]
        ],
    }


def context(item, labels):
    by_id = {value: key for key, value in labels.items()}
    return {side: [by_id[entry["chunk_id"]] for entry in item["context"][side]] for side in ("before", "after")}


def positive(inputs, root):
    corpus = Corpus(inputs, root)
    corpus.index()
    labels, expected = corpus.labels(), inputs["expected"]
    authoritative = corpus.protected()
    narrowed = request(inputs, inputs["queries"]["recall"], projects=[inputs["project"]], group_sessions=True)
    answer = corpus.ask(narrowed, provider=embedder(inputs))
    found = named(labels, answer)
    single = corpus.ask(
        request(inputs, inputs["queries"]["recall"]), grant(inputs, [inputs["project"]]), embedder(inputs)
    )
    wide = corpus.ask(request(inputs, inputs["queries"]["recall"]), provider=embedder(inputs))
    symbol = corpus.ask(request(inputs, inputs["queries"]["symbol"], context=0), provider=embedder(inputs))
    stop = corpus.ask(request(inputs, inputs["queries"]["stopwords"]), provider=embedder(inputs))
    wanted = expected["recall_lexical"] + expected["recall_semantic"]
    kernel = [label for label in labels if label.startswith("kernel:")]
    top = answer["results"][0]
    assert answer["mode"] == "hybrid" and set(wanted) <= set(found) and not set(kernel) & set(found)
    assert named(labels, single) == found and set(kernel) & set(named(labels, wide))
    assert named(labels, symbol)[0] == expected["symbol"][0] and stop["results"] == []
    assert b"database is locked" in evidence_bytes(corpus, top)
    assert corpus.protected() == authoritative
    return {
        "recall": summary(answer, labels),
        "recall_sessions": [
            {"session": item["session_uid"].rsplit(":", 1)[1], "hits": len(item["chunk_ids"])}
            for item in answer["sessions"]
        ],
        "recall_query": answer["query"],
        "recall_fusion": answer["fusion"],
        "single_project_grant_matches_narrowed": named(labels, single) == found,
        "unnarrowed_includes_other_project": sorted(set(kernel) & set(named(labels, wide))),
        "other_project_excluded": sorted(set(kernel) - set(found)),
        "symbol": summary(symbol, labels),
        "stopwords_only": {"state": stop["state"], "results": len(stop["results"]), "query": stop["query"]},
        "top_hit_evidence_has_error_string": True,
        "authoritative_state_unchanged": True,
        "session_search_relevance": {
            "recall_lexical": relevance(answer["results"], [labels[label] for label in expected["recall_lexical"]]),
            "recall_semantic": relevance(answer["results"], [labels[label] for label in expected["recall_semantic"]]),
            "symbol": relevance(symbol["results"], [labels[label] for label in expected["symbol"]]),
        },
        "session_search_scope_rejections": corpus.rejections(),
    }


def rejection(inputs, root):
    corpus = Corpus(inputs, root)
    corpus.index()
    before = corpus.protected()
    query = inputs["queries"]["recall"]
    provider = embedder(inputs)
    attempts = {
        "unauthorized_project": corpus.ask(
            request(inputs, query, projects=[inputs["unauthorized_project"]]), None, provider
        ),
        "authorized_and_unauthorized": corpus.ask(
            request(inputs, query, projects=[inputs["project"], inputs["unauthorized_project"]]), None, provider
        ),
        "foreign_corpus": corpus.ask(
            {**request(inputs, query), "access_domain": inputs["foreign_corpus"]}, None, provider
        ),
        "project_outside_a_narrow_grant": corpus.ask(
            request(inputs, query, projects=[inputs["other_project"]]), grant(inputs, [inputs["project"]]), provider
        ),
    }
    invalid = {
        "unknown_field": corpus.ask({**request(inputs, query), "index_generation": "fixture-search-v0"}),
        "limit_above_bound": corpus.ask(request(inputs, query, limit=51)),
        "context_above_bound": corpus.ask(request(inputs, query, context=4)),
    }
    untouched = corpus.protected() == before
    diagnostics = json.dumps({**attempts, **invalid}, ensure_ascii=False)
    shapes = {name: sorted(answer) for name, answer in attempts.items()}
    details = {answer["error"]["detail"] for answer in attempts.values()}
    assert untouched and provider.calls == 0 and len(details) == 1
    assert all(shape == ["error", "operation_id", "schema_version", "state"] for shape in shapes.values())
    assert not any(hidden in diagnostics for hidden in HIDDEN)
    rejected = corpus.rejections()
    corrected = corpus.ask(request(inputs, query, projects=[inputs["project"]]), None, provider)
    assert corrected["state"] == "answered" and corpus.rejections() == rejected
    return {
        "rejected": {name: answer["error"] for name, answer in attempts.items()},
        "invalid": {name: answer["error"] for name, answer in invalid.items()},
        "response_fields": shapes,
        "provider_calls_while_rejected": 0,
        "protected_state_unchanged": untouched,
        "hidden_names_absent_from_diagnostics": True,
        "session_search_scope_rejections": rejected,
        "corrected": {"state": corrected["state"], "mode": corrected["mode"], "results": len(corrected["results"])},
    }


def capture(corpus, inputs):
    item = inputs["sessions"][0]
    session = corpus.sessions[item["name"]]
    return session.append(inputs["appended"])


def rollback(corpus, inputs, labels, hybrid):
    domain, query = (
        inputs["grant"]["corpus"],
        request(inputs, inputs["queries"]["recall"], projects=[inputs["project"]]),
    )
    first = corpus.worker.active(domain)
    corpus.index("next_index_generation", "next_model")
    routed_next = corpus.ask(query, provider=embedder(inputs, "next_model"))
    current = corpus.worker.active(domain)
    stale = refusal(lambda: corpus.worker.activate(domain, inputs["index_generation"], first["revision"]))
    restored = corpus.worker.activate(domain, inputs["index_generation"], current["revision"])
    captured = capture(corpus, inputs)
    routed_back = corpus.ask(query, provider=embedder(inputs))
    mismatch = corpus.ask(query, provider=embedder(inputs, "next_model"))
    kept = hits(routed_back) == hits(hybrid)
    assert stale["error"] and kept and captured["state"] == "committed" and mismatch["mode"] == "lexical_only"
    return {
        "next_generation_answer": {"mode": routed_next["mode"], "vector": routed_next["vector"]},
        "stale_rollback_refused": stale,
        "rolled_back_to": {**corpus.worker.active(domain), "revision_returned": restored},
        "capture_during_rollback": {"state": captured["state"], "durable_through": captured["durable_through"]},
        "routed_back_answer": {"mode": routed_back["mode"], "vector": routed_back["vector"]},
        "routed_back_matches_first_generation": kept,
        "next_model_after_rollback": {"mode": mismatch["mode"], "vector": mismatch["vector"]},
    }


def recovery(inputs, root):
    corpus = Corpus(inputs, root)
    labels = corpus.labels()
    query = request(inputs, inputs["queries"]["recall"], projects=[inputs["project"]])
    unindexed = corpus.ask(query, provider=embedder(inputs))
    corpus.index()
    before = corpus.protected()
    down = embedder(inputs, down=True)
    outage = corpus.ask(query, provider=down)
    replay = corpus.ask(query, provider=down)
    corpus.restart()
    restored = corpus.ask(query, provider=embedder(inputs))
    again = corpus.ask(query, provider=embedder(inputs))
    untouched = corpus.protected() == before
    semantic = inputs["expected"]["recall_semantic"]
    assert outage["mode"] == "lexical_only" and outage["results"] and replay == outage and down.calls == 2
    assert not set(semantic) & set(named(labels, outage)) and set(semantic) <= set(named(labels, restored))
    assert again == restored and untouched and unindexed["mode"] == "lexical_only"
    return {
        "before_index": {"mode": unindexed["mode"], "vector": unindexed["vector"], "results": named(labels, unindexed)},
        "outage": summary(outage, labels),
        "outage_replay_identical": replay == outage,
        "outage_operation_id_stable": replay["operation_id"] == outage["operation_id"],
        "restored": summary(restored, labels),
        "restored_replay_identical": again == restored,
        "semantic_hits_missing_during_outage": sorted(set(semantic) - set(named(labels, outage))),
        "protected_state_unchanged": untouched,
        "session_search_relevance": {
            "outage": relevance(outage["results"], [labels[label] for label in semantic]),
            "restored": relevance(restored["results"], [labels[label] for label in semantic]),
        },
        "rollback_rehearsal": rollback(corpus, inputs, labels, restored),
        "session_search_scope_rejections": corpus.rejections(),
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as folder:
        observed = CASES[case](inputs, Path(folder))
    return {
        "case": f"T-SV2-IDX-04-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        "observed": observed,
    }
