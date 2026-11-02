import hashlib
import math
from pathlib import Path

import pytest

from scripts.ledger_knowledge.chunks import Chunk
from scripts.ledger_knowledge.index import DIMENSION, HashEmbedder, Hit, KnowledgeIndex, Refresh, digest


class CountingEmbedder(HashEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return super().embed(texts)


ROOT = Chunk("ledger#0", "ledger", "", "ledger", "Swarm", "swarm ledger")
PHASE = Chunk("phases/p#0", "phases/p", "ledger", "phase", "Understand", "agents understand the ledger")
TASK = Chunk("tasks/t#0", "tasks/t", "phases/p", "task", "Index", "knowledge index embedding")
COMMENT = Chunk("tasks/t/comments/c#0", "tasks/t/comments/c", "tasks/t", "comment", "c", "vectors landed today")


def index(tmp_path: Path, embedder: HashEmbedder | None = None) -> KnowledgeIndex:
    return KnowledgeIndex(tmp_path / "deep" / "slug.sqlite3", embedder or HashEmbedder())


def bucket(word: str, dimension: int = DIMENSION) -> int:
    return int(hashlib.sha256(word.encode()).hexdigest()[:8], 16) % dimension


def test_hash_embedder_counts_lowercased_words_into_unit_vectors() -> None:
    (vector,) = HashEmbedder().embed(["Alpha, alpha beta"])
    assert len(vector) == DIMENSION == 512
    assert math.isclose(sum(value * value for value in vector), 1.0)
    expected = [0.0] * DIMENSION
    expected[bucket("alpha")] += 2.0
    expected[bucket("beta")] += 1.0
    norm = math.sqrt(sum(value * value for value in expected))
    assert vector == [value / norm for value in expected]


def test_hash_embedder_takes_its_dimension_and_leaves_empty_text_zero() -> None:
    embedder = HashEmbedder(8)
    assert embedder.dimension == 8
    assert embedder.embed(["", "x1"]) == [[0.0] * 8, [1.0 if i == bucket("x1", 8) else 0.0 for i in range(8)]]


def test_the_index_file_and_its_folder_are_created(tmp_path: Path) -> None:
    built = index(tmp_path)
    assert built.path == tmp_path / "deep" / "slug.sqlite3"
    assert built.path.is_file()
    assert built.refreshed_at() == 0
    built.refresh([ROOT], 5)
    assert index(tmp_path).get(ROOT.key) == ROOT


def test_refresh_embeds_only_new_and_changed_chunks(tmp_path: Path) -> None:
    embedder = CountingEmbedder()
    built = index(tmp_path, embedder)
    assert built.refresh([ROOT, PHASE, TASK], 1000) == Refresh(added=3, changed=0, removed=0, unchanged=0)
    assert built.refresh([ROOT, PHASE, TASK], 2000) == Refresh(added=0, changed=0, removed=0, unchanged=3)
    edited = Chunk(TASK.key, TASK.ref, TASK.parent, TASK.kind, TASK.title, "rewritten body")
    assert built.refresh([ROOT, edited, COMMENT], 3000) == Refresh(added=1, changed=1, removed=1, unchanged=1)
    assert embedder.calls == [[ROOT.text, PHASE.text, TASK.text], ["rewritten body", COMMENT.text]]
    assert built.refreshed_at() == 3000
    assert built.get(PHASE.key) is None
    assert built.get(TASK.key) == edited


def test_digest_changes_with_any_field() -> None:
    assert (
        digest(TASK)
        == hashlib.sha256(
            b'["tasks/t#0", "tasks/t", "phases/p", "task", "Index", "knowledge index embedding"]'
        ).hexdigest()
    )
    assert digest(TASK) != digest(Chunk(TASK.key, TASK.ref, "ledger", TASK.kind, TASK.title, TASK.text))


def test_a_query_returns_the_matching_child_with_its_parent_phase(tmp_path: Path) -> None:
    built = index(tmp_path)
    built.refresh([ROOT, PHASE, TASK, COMMENT], 1000)
    (hit,) = built.query("knowledge embedding", 1)
    assert hit.chunk == TASK
    assert hit.parents == [PHASE, ROOT]
    assert math.isclose(hit.score, 2 / math.sqrt(2 * 3))
    (deep,) = built.query("vectors landed", 1)
    assert deep == Hit(COMMENT, deep.score, [TASK, PHASE, ROOT])


def test_a_query_ranks_by_score_then_key_and_drops_unrelated_chunks(tmp_path: Path) -> None:
    built = index(tmp_path)
    twin = Chunk("notes/a#0", "notes/a", "ledger", "note", "a", "ledger")
    built.refresh([ROOT, PHASE, TASK, twin], 1000)
    hits = built.query("ledger", 10)
    assert [hit.chunk.key for hit in hits] == ["notes/a#0", "ledger#0", "phases/p#0"]
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)
    assert [hit.chunk.key for hit in built.query("ledger", 2)] == ["notes/a#0", "ledger#0"]
    assert built.query("nothing matches", 5) == []


def test_a_parent_chain_stops_at_a_missing_or_repeated_parent(tmp_path: Path) -> None:
    built = index(tmp_path)
    looped = Chunk("x#0", "x", "x", "note", "x", "loop word")
    orphan = Chunk("y#0", "y", "gone", "note", "y", "orphan word")
    built.refresh([looped, orphan], 1000)
    assert built.query("loop", 1)[0].parents == [looped]
    assert built.query("orphan", 1)[0].parents == []


def test_changes_lists_newest_first_and_counts_items_per_kind(tmp_path: Path) -> None:
    built = index(tmp_path)
    built.refresh([ROOT, PHASE], 1000)
    second = Chunk("phases/p#1", "phases/p", "ledger", "phase", "Understand", "second piece")
    built.refresh([ROOT, PHASE, second, TASK], 2000)
    assert built.changes(10) == [(2000, second), (2000, TASK), (1000, ROOT), (1000, PHASE)]
    assert built.changes(1) == [(2000, second)]
    assert built.counts() == {"ledger": 1, "phase": 1, "task": 1}


@pytest.mark.parametrize("key", ["missing#0", ""])
def test_get_answers_none_for_an_unknown_key(tmp_path: Path, key: str) -> None:
    assert index(tmp_path).get(key) is None
