import math
from pathlib import Path

from scripts.ledger_knowledge import service
from scripts.ledger_knowledge.chunks import Chunk
from scripts.ledger_knowledge.index import HashEmbedder, KnowledgeIndex, Refresh


class Reader:
    def __init__(self, document: dict) -> None:
        self.document = document
        self.reads = 0

    def __call__(self) -> dict:
        self.reads += 1
        return self.document


def ledger() -> dict:
    return {
        "title": "Swarm Design",
        "overview": "agents understand",
        "phases": [{"id": "p1", "title": "Mission phase"}],
        "tasks": [{"id": "t1", "title": "Knowledge index", "phase": "p1"}],
    }


def built(tmp_path: Path) -> KnowledgeIndex:
    return KnowledgeIndex(tmp_path / "k.sqlite3", HashEmbedder())


def test_the_index_path_sits_under_the_agentihooks_home(tmp_path: Path) -> None:
    path = service.index_path("rig", {"AGENTIHOOKS_HOME": str(tmp_path)})
    assert path == tmp_path / "knowledge" / "rig.sqlite3"


def test_refresh_chunks_the_document_into_the_index(tmp_path: Path) -> None:
    index = built(tmp_path)
    assert service.refresh(index, ledger(), 7) == Refresh(added=3, changed=0, removed=0, unchanged=0)
    assert index.refreshed_at() == 7


def test_ask_answers_what_the_ledger_is_with_hits_under_their_phase(tmp_path: Path) -> None:
    index = built(tmp_path)
    read = Reader(ledger())
    answer = service.ask(index, read, "knowledge index", 5000)
    assert read.reads == 1
    assert answer["ledger"] == {"title": "Swarm Design", "counts": {"ledger": 1, "phase": 1, "task": 1}}
    assert answer["refreshed_at"] == 5000
    (hit,) = answer["hits"]
    assert hit == {
        "score": 1.0,
        "key": "tasks/t1#0",
        "ref": "tasks/t1",
        "parent": "phases/p1",
        "kind": "task",
        "title": "Knowledge index",
        "text": "Knowledge index",
        "parents": [
            {
                "key": "phases/p1#0",
                "ref": "phases/p1",
                "parent": "ledger",
                "kind": "phase",
                "title": "Mission phase",
                "text": "Mission phase",
            },
            {
                "key": "ledger#0",
                "ref": "ledger",
                "parent": "",
                "kind": "ledger",
                "title": "Swarm Design",
                "text": "Swarm Design\n\nagents understand",
            },
        ],
    }


def test_an_added_task_is_searchable_within_minutes(tmp_path: Path) -> None:
    assert service.REFRESH_SECONDS == 120
    index = built(tmp_path)
    document = ledger()
    read = Reader(document)
    service.ask(index, read, "anything", 1000)
    document["tasks"].append({"id": "t2", "title": "Widget search", "phase": "p1"})
    early = service.ask(index, read, "widget", 1000 + service.REFRESH_SECONDS * 1000 - 1)
    assert (read.reads, early["hits"]) == (1, [])
    later = service.ask(index, read, "widget", 1000 + service.REFRESH_SECONDS * 1000)
    assert read.reads == 2
    assert [(hit["ref"], hit["parents"][0]["ref"]) for hit in later["hits"]] == [("tasks/t2", "phases/p1")]
    assert (later["evolving"][0]["ref"], later["evolving"][0]["at"]) == ("tasks/t2", 121000)


def test_ask_rounds_scores_and_keeps_the_default_hit_limit(tmp_path: Path) -> None:
    index = built(tmp_path)
    notes = [{"id": f"n{i}", "text": f"shared word{i}"} for i in range(7)]
    read = Reader({"title": "T", "notes": notes})
    answer = service.ask(index, read, "shared extra", 1)
    assert len(answer["hits"]) == 5
    assert answer["hits"][0]["score"] == round(1 / math.sqrt(2) / math.sqrt(2), 4) == 0.5
    assert [hit["score"] for hit in service.ask(index, read, "shared x y", 2)["hits"][:1]] == [0.4082]


def test_evolving_lists_the_newest_changes_up_to_its_cap_without_a_title(tmp_path: Path) -> None:
    assert service.CHANGES == 10
    index = built(tmp_path)
    index.refresh([Chunk(f"notes/{i:02}#0", f"notes/{i:02}", "ledger", "note", "", "") for i in range(12)], 0)
    answer = service.ask(index, Reader({}), "q", 1)
    assert answer["ledger"]["title"] == ""
    assert [entry["key"] for entry in answer["evolving"]] == [f"notes/{i:02}#0" for i in range(10)]
