import pytest

from scripts.ledger_knowledge.chunks import WINDOW, Chunk, chunk_ledger


def ledger() -> dict:
    return {
        "title": "Swarm Design",
        "overview": "Ledger overview",
        "phases": [
            {
                "id": "p1",
                "title": "Agents understand",
                "description": "Mission text",
                "comments": [{"id": "c1", "text": "phase note"}, {"id": "c0", "text": "gone", "deleted": True}],
            }
        ],
        "tasks": [
            {
                "id": "t1",
                "title": "Knowledge index",
                "description": "Embed the ledger",
                "phase": "p1",
                "proof": {"run": "42"},
                "comments": [{"id": "c2", "text": "landed"}],
            },
            {"id": "t2", "title": "Loose", "phase": ""},
            {"id": "t3", "title": "Removed", "phase": "p1", "deleted": True},
        ],
        "questions": [{"id": "q1", "title": "Which model"}],
        "followups": [{"id": "f1", "text": "follow"}],
        "notes": [{"id": "n1", "text": "note"}],
        "chat": [{"id": "m1", "text": "chat is left out"}],
    }


def test_every_item_is_chunked_under_its_parent_in_document_order() -> None:
    assert chunk_ledger(ledger()) == [
        Chunk("ledger#0", "ledger", "", "ledger", "Swarm Design", "Swarm Design\n\nLedger overview"),
        Chunk("phases/p1#0", "phases/p1", "ledger", "phase", "Agents understand", "Agents understand\n\nMission text"),
        Chunk("phases/p1/comments/c1#0", "phases/p1/comments/c1", "phases/p1", "comment", "phase note", "phase note"),
        Chunk("tasks/t1#0", "tasks/t1", "phases/p1", "task", "Knowledge index", "Knowledge index\n\nEmbed the ledger"),
        Chunk("tasks/t1/proof#0", "tasks/t1/proof", "tasks/t1", "proof", "Knowledge index", '{"run": "42"}'),
        Chunk("tasks/t1/comments/c2#0", "tasks/t1/comments/c2", "tasks/t1", "comment", "landed", "landed"),
        Chunk("tasks/t2#0", "tasks/t2", "ledger", "task", "Loose", "Loose"),
        Chunk("questions/q1#0", "questions/q1", "ledger", "question", "Which model", "Which model"),
        Chunk("followups/f1#0", "followups/f1", "ledger", "followup", "follow", "follow"),
        Chunk("notes/n1#0", "notes/n1", "ledger", "note", "note", "note"),
    ]


def test_an_empty_document_still_has_its_root_chunk() -> None:
    assert chunk_ledger({}) == [Chunk("ledger#0", "ledger", "", "ledger", "", "")]


def test_an_item_without_text_keeps_one_empty_chunk() -> None:
    assert chunk_ledger({"notes": [{"id": "n"}]})[1] == Chunk("notes/n#0", "notes/n", "ledger", "note", "", "")


@pytest.mark.parametrize(
    ("proof", "text"),
    [
        ("run 42 green", "run 42 green"),
        ({"b": 1, "a": 2}, '{"a": 2, "b": 1}'),
        ({"note": "é"}, '{"note": "é"}'),
    ],
)
def test_a_proof_is_its_own_child_of_the_task(proof: object, text: str) -> None:
    chunks = chunk_ledger({"tasks": [{"id": "t", "title": "T", "proof": proof}]})
    assert chunks[2] == Chunk("tasks/t/proof#0", "tasks/t/proof", "tasks/t", "proof", "T", text)


def test_a_long_body_becomes_several_small_chunks_of_one_item() -> None:
    paragraphs = "\n\n".join("word " * 100 for _ in range(4))
    chunks = chunk_ledger({"tasks": [{"id": "t", "description": paragraphs}]})[1:]
    assert [chunk.key for chunk in chunks] == [f"tasks/t#{index}" for index in range(len(chunks))]
    assert len(chunks) > 1
    assert all(len(chunk.text) <= WINDOW for chunk in chunks)
    assert {chunk.ref for chunk in chunks} == {"tasks/t"}
    assert WINDOW == 600


def test_a_title_falls_back_to_the_item_text() -> None:
    chunk = chunk_ledger({"followups": [{"id": "f", "text": "only text"}]})[1]
    assert (chunk.title, chunk.text) == ("only text", "only text")
