"""Scoped evidence search: lexical and vector candidates over active projections, fused by reciprocal rank."""

import json
import math
import re
from dataclasses import dataclass

from hooks.memory import embeddings
from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import COUNT, COUNTER, Catalog, Conflict
from hooks.memory.chunking import locator
from hooks.memory.embeddings import Embedder, TransientEmbeddingError
from hooks.memory.index_worker import IndexWorker

SCOPED = (
    "SELECT s.record FROM search_chunk s JOIN chunk_cutover c ON c.access_domain = s.access_domain "
    "AND c.session_uid = s.session_uid AND c.chunker_version = s.chunker_version "
    "JOIN session_record r ON r.access_domain = s.access_domain AND r.session_uid = s.session_uid "
    "WHERE s.access_domain = ? AND r.state = 'retained' AND NOT EXISTS (SELECT 1 FROM session_event e "
    "WHERE e.access_domain = s.access_domain AND e.session_uid = s.session_uid AND e.project_id IS NOT NULL "
    "AND e.project_id NOT IN (SELECT value FROM json_each(?))) AND EXISTS (SELECT 1 FROM session_event e "
    "WHERE e.access_domain = s.access_domain AND e.session_uid = s.session_uid "
    "AND e.project_id IN (SELECT value FROM json_each(?))) "
    "ORDER BY s.session_uid, s.source_generation, s.first_sequence, s.chunk_id"
)
VECTORS = (
    "SELECT chunk_id, content_hash, vector FROM chunk_vector WHERE access_domain = ? AND index_generation = ? "
    "ORDER BY chunk_id"
)
TOKEN = re.compile(r"[\w.:]+")
PHRASE = re.compile(r'"([^"]*)"')
STOPWORDS = frozenset(
    "a an and are as at be by did do does for from how in is it of on or the this that to was what when where why with".split()
)
RRF_K = 60
CANDIDATE_FACTOR = 4
MAX_LIMIT = 50
MAX_CONTEXT = 3
RESULT_KEYS = ("chunk_id", "session_uid", "turn_id", "kind", "project_ids", "preview", "truncated")
SCOPE_REJECTIONS = "session_search_scope_rejections"
SCOPE_REFUSED = "the grant does not cover the requested search scope"
BAD_BOUNDS = f"limit must be 1 to {MAX_LIMIT} and context 0 to {MAX_CONTEXT}"


@dataclass(frozen=True)
class Query:
    access_domain: str
    text: str
    projects: tuple[str, ...] = ()
    limit: int = 10
    context: int = 1
    group_sessions: bool = False


class ScopeRefused(Exception):
    pass


class _Unavailable(Exception):
    pass


def tokens(text: str) -> list[str]:
    return [word for word in (raw.strip(".:").lower() for raw in TOKEN.findall(text)) if word]


def parse(text: str) -> tuple[list[str], list[str]]:
    phrases = sorted({phrase.lower() for phrase in PHRASE.findall(text) if phrase.strip()})
    terms = sorted({word for word in tokens(PHRASE.sub(" ", text)) if word not in STOPWORDS})
    return phrases, terms


def _matched(chunk: dict, units: list[tuple[str, bool]]) -> list[str]:
    text = chunk["preview"].lower()
    words = set(tokens(chunk["preview"]))
    return [unit for unit, phrase in units if (unit in text if phrase else unit in words)]


def lexical(chunks: list[dict], phrases: list[str], terms: list[str]) -> list[tuple[str, float, list[str]]]:
    units = [(phrase, True) for phrase in phrases] + [(term, False) for term in terms]
    found = [(chunk["chunk_id"], _matched(chunk, units)) for chunk in chunks]
    found = [(chunk_id, matched) for chunk_id, matched in found if matched and set(phrases) <= set(matched)]
    frequency = {unit: sum(unit in matched for _, matched in found) for unit, _ in units}
    scored = [
        (chunk_id, round(sum(math.log(1 + len(chunks) / frequency[unit]) for unit in matched), 6), matched)
        for chunk_id, matched in found
    ]
    return sorted(scored, key=lambda item: (-item[1], item[0]))


def cosine(left: list[float], right: list[float]) -> float:
    norm = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right)) / norm if norm else 0.0


def fuse(lexical_ranked: list, vector_ranked: list) -> list[tuple[str, dict]]:
    explain: dict[str, dict] = {}
    for rank, (chunk_id, score, matched) in enumerate(lexical_ranked, 1):
        part = {"rank": rank, "score": score, "matched": matched}
        explain.setdefault(chunk_id, {"lexical": None, "vector": None})["lexical"] = part
    for rank, (chunk_id, score) in enumerate(vector_ranked, 1):
        explain.setdefault(chunk_id, {"lexical": None, "vector": None})["vector"] = {"rank": rank, "score": score}
    for entry in explain.values():
        entry["fused"] = round(
            sum(1 / (RRF_K + part["rank"]) for part in (entry["lexical"], entry["vector"]) if part), 6
        )
    return sorted(explain.items(), key=lambda item: (-item[1]["fused"], item[0]))


def _brief(chunk: dict) -> dict:
    return {
        "chunk_id": chunk["chunk_id"],
        "kind": chunk["kind"],
        "preview": chunk["preview"],
        "locator": locator(chunk),
    }


def _results(chunks: list[dict], fused: list[tuple[str, dict]], width: int) -> list[dict]:
    sessions: dict[str, list[dict]] = {}
    position = {}
    for chunk in chunks:
        ordered = sessions.setdefault(chunk["session_uid"], [])
        position[chunk["chunk_id"]] = (ordered, len(ordered))
        ordered.append(chunk)
    results = []
    for chunk_id, explain in fused:
        ordered, index = position[chunk_id]
        before, after = ordered[max(0, index - width) : index], ordered[index + 1 : index + 1 + width]
        context = {"before": [_brief(item) for item in before], "after": [_brief(item) for item in after]}
        chunk = ordered[index]
        results.append(
            {
                **{key: chunk[key] for key in RESULT_KEYS},
                "locator": locator(chunk),
                "explain": explain,
                "context": context,
            }
        )
    return results


def grouped(results: list[dict]) -> list[dict]:
    sessions: dict[str, dict] = {}
    for result in results:
        entry = sessions.setdefault(
            result["session_uid"],
            {"session_uid": result["session_uid"], "best_fused": result["explain"]["fused"], "chunk_ids": []},
        )
        entry["chunk_ids"].append(result["chunk_id"])
    return list(sessions.values())


def relevance(results: list[dict], expected: list[str]) -> dict:
    ranks = [index for index, result in enumerate(results, 1) if result["chunk_id"] in expected]
    return {"recall": round(len(ranks) / len(expected), 6), "reciprocal_rank": round(1 / ranks[0], 6) if ranks else 0.0}


class SessionSearch:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog, self.worker = catalog, IndexWorker(catalog)

    def _allowed(self, grant: Grant, query: Query) -> list[str]:
        if query.access_domain != grant.corpus or not set(query.projects) <= grant.projects:
            self.catalog.db.execute(COUNT, (grant.corpus, SCOPE_REJECTIONS, 1))
            raise ScopeRefused(SCOPE_REFUSED)
        return sorted(query.projects or grant.projects)

    def _scoped(self, domain: str, projects: list[str]) -> list[dict]:
        allowed = json.dumps(projects)
        rows = self.catalog.db.execute(SCOPED, (domain, allowed, allowed)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _query_vector(self, domain: str, text: str, embedder: Embedder | None) -> tuple[dict, list[float]]:
        generation = self.worker.active(domain)["index_generation"]
        if generation is None:
            raise _Unavailable("no_active_index")
        model = self.worker.model(domain, generation)
        if embedder is None or embedder.model != model:
            raise _Unavailable("no_embedder" if embedder is None else "model_mismatch")
        try:
            vector = embeddings.checked(model, [text], embedder.embed([text]))[0]
        except TransientEmbeddingError:
            raise _Unavailable("provider_unavailable") from None
        except Conflict:
            raise _Unavailable("provider_answer_invalid") from None
        return {"state": "used", "index_generation": generation, "model": model.model}, vector

    def _vector(self, query: Query, embedder: Embedder | None, chunks: list[dict]) -> tuple[list, dict]:
        try:
            label, wanted = self._query_vector(query.access_domain, query.text, embedder)
        except _Unavailable as reason:
            return [], {"state": "unavailable", "reason": str(reason)}
        current = {chunk["chunk_id"]: chunk["content_hash"] for chunk in chunks}
        rows = self.catalog.db.execute(VECTORS, (query.access_domain, label["index_generation"])).fetchall()
        scored = [
            (row[0], round(cosine(wanted, json.loads(row[2])), 6)) for row in rows if current.get(row[0]) == row[1]
        ]
        ranked = sorted((item for item in scored if item[1] > 0), key=lambda item: (-item[1], item[0]))
        return ranked[: query.limit * CANDIDATE_FACTOR], label

    def search(self, grant: Grant, query: Query, embedder: Embedder | None = None) -> dict:
        allowed = self._allowed(grant, query)
        if not (1 <= query.limit <= MAX_LIMIT and 0 <= query.context <= MAX_CONTEXT):
            raise ValueError(BAD_BOUNDS)
        chunks = self._scoped(query.access_domain, allowed)
        phrases, terms = parse(query.text)
        lexical_ranked = lexical(chunks, phrases, terms)[: query.limit * CANDIDATE_FACTOR]
        vector_ranked, label = self._vector(query, embedder, chunks)
        results = _results(chunks, fuse(lexical_ranked, vector_ranked)[: query.limit], query.context)
        answer = {
            "mode": "hybrid" if label["state"] == "used" else "lexical_only",
            "vector": label,
            "fusion": {"method": "reciprocal_rank", "k": RRF_K},
            "query": {"phrases": phrases, "terms": terms},
            "results": results,
        }
        return {**answer, "sessions": grouped(results)} if query.group_sessions else answer

    def scope_rejections_total(self, domain: str) -> int:
        row = self.catalog.db.execute(COUNTER, (domain, SCOPE_REJECTIONS)).fetchone()
        return row[0] if row else 0
