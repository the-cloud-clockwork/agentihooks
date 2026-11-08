import hashlib
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hooks.memory.embedding_process import Provider
from scripts.ledger_knowledge import cli, embedders
from scripts.ledger_knowledge.chunks import Chunk
from scripts.ledger_knowledge.index import HashEmbedder, KnowledgeIndex

CONCEPTS = {
    "reboot": 0,
    "restart": 0,
    "stuck": 1,
    "frozen": 1,
    "hung": 1,
    "daemon": 2,
    "process": 2,
    "service": 2,
    "invoice": 3,
    "billing": 3,
}
MODEL = "concept-small"
TASK = Chunk("tasks/t#0", "tasks/t", "", "task", "Reboot", "reboot a stuck daemon")
OTHER = Chunk("tasks/o#0", "tasks/o", "", "task", "Billing", "billing invoice")
QUESTION = "restart frozen process"


def concept_vector(text: str) -> list[float]:
    vector = [0.0, 0.0, 0.0, 0.0]
    for word in text.lower().split():
        if word in CONCEPTS:
            vector[CONCEPTS[word]] += 1.0
    return vector


class ConceptServer(BaseHTTPRequestHandler):
    batches: list[int] = []
    authorizations: list[str] = []

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).authorizations.append(self.headers["Authorization"])
        type(self).batches.append(len(body["input"]))
        data = [{"index": i, "embedding": concept_vector(text)} for i, text in enumerate(body["input"])]
        payload = json.dumps({"model": body["model"], "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def server(no_proxy: None) -> Iterator[str]:
    ConceptServer.batches = []
    ConceptServer.authorizations = []
    httpd = HTTPServer(("127.0.0.1", 0), ConceptServer)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1/embeddings"
    httpd.shutdown()
    httpd.server_close()


def semantic_environ(url: str, **extra: str) -> dict[str, str]:
    return {
        "AGENTIHOOKS_KNOWLEDGE_EMBEDDER": "semantic",
        "AGENTIHOOKS_KNOWLEDGE_EMBED_URL": url,
        "AGENTIHOOKS_KNOWLEDGE_EMBED_MODEL": MODEL,
        "AGENTIHOOKS_KNOWLEDGE_EMBED_DIMENSION": "4",
        "AGENTIHOOKS_KNOWLEDGE_EMBED_TOKEN": "secret-value",
        **extra,
    }


def refs(index: KnowledgeIndex) -> list[str]:
    index.refresh([TASK, OTHER], 1)
    return [hit.chunk.ref for hit in index.query(QUESTION, 5)]


def test_the_hash_embedder_is_the_default_choice() -> None:
    choice = embedders.choose({})
    assert choice.name == "hash"
    assert isinstance(choice.embedder, HashEmbedder)
    assert embedders.choose({"AGENTIHOOKS_KNOWLEDGE_EMBEDDER": "hash"}).name == "hash"


def test_a_differently_worded_question_misses_with_hash_and_finds_with_semantic(tmp_path: Path, server: str) -> None:
    assert refs(KnowledgeIndex(tmp_path / "hash.sqlite3", HashEmbedder())) == []
    semantic = embedders.choose(semantic_environ(server))
    assert refs(KnowledgeIndex(tmp_path / "semantic.sqlite3", semantic.embedder)) == ["tasks/t"]


def test_semantic_choice_wraps_the_memory_provider_in_bounded_batches(server: str) -> None:
    choice = embedders.choose(semantic_environ(server))
    assert isinstance(choice.embedder, embedders.Batched)
    assert isinstance(choice.embedder.inner, Provider)
    assert choice.embedder.size == embedders.BATCH == 32
    assert "secret-value" not in repr(choice)
    batched = embedders.Batched(choice.embedder.inner, 2)
    vectors = batched.embed(["reboot", "stuck", "daemon", "billing", "hung"])
    assert ConceptServer.batches == [2, 2, 1]
    assert ConceptServer.authorizations == ["Bearer secret-value"] * 3
    assert [vector.index(1.0) for vector in vectors] == [0, 1, 2, 3, 1]
    assert batched.embed([]) == []
    assert ConceptServer.batches == [2, 2, 1]


def test_semantic_vectors_are_unit_length_so_ranking_is_cosine(server: str) -> None:
    long, short, empty = embedders.choose(semantic_environ(server)).embedder.embed(
        ["reboot restart reboot stuck", "daemon", "none"]
    )
    assert long == pytest.approx([3 / 10**0.5, 1 / 10**0.5, 0.0, 0.0])
    assert short == [0.0, 0.0, 1.0, 0.0]
    assert empty == [0.0, 0.0, 0.0, 0.0]


def test_cli_reports_a_provider_outage_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_proxy: None
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environ = semantic_environ(f"http://127.0.0.1:{port}/v1/embeddings", AGENTIHOOKS_HOME=str(tmp_path))
    assert cli.main(["refresh", "--ledger", "rig"], environ, lambda slug: {"title": "t"}, lambda: 1.0) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "knowledge: embedding provider unavailable\n")


def test_each_embedder_names_its_own_index_file(server: str) -> None:
    first = embedders.choose(semantic_environ(server)).name
    other_model = embedders.choose(semantic_environ(server, AGENTIHOOKS_KNOWLEDGE_EMBED_MODEL="other")).name
    other_dimension = embedders.choose(semantic_environ(server, AGENTIHOOKS_KNOWLEDGE_EMBED_DIMENSION="8")).name
    assert first == "semantic-" + hashlib.sha256(f"{MODEL}\n4".encode()).hexdigest()[:12]
    assert len({first, other_model, other_dimension, "hash"}) == 4
    assert first == embedders.choose(semantic_environ(server)).name


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"AGENTIHOOKS_KNOWLEDGE_EMBEDDER": "words"}, "AGENTIHOOKS_KNOWLEDGE_EMBEDDER must be hash or semantic"),
        (
            {"AGENTIHOOKS_KNOWLEDGE_EMBEDDER": "semantic"},
            "semantic embedder needs AGENTIHOOKS_KNOWLEDGE_EMBED_URL, AGENTIHOOKS_KNOWLEDGE_EMBED_MODEL,"
            " AGENTIHOOKS_KNOWLEDGE_EMBED_DIMENSION, AGENTIHOOKS_KNOWLEDGE_EMBED_TOKEN",
        ),
        (semantic_environ("http://example.com/v1/embeddings"), "embedding URL requires HTTPS or loopback HTTP"),
        (semantic_environ("https://user:pw@example.com/v1"), "embedding URL must not carry credentials"),
        (semantic_environ("https://example.com/v1?key=1"), "embedding URL must not carry credentials"),
    ],
)
def test_a_bad_embedder_setting_is_refused(environ: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        embedders.choose(environ)


def test_https_url_is_accepted() -> None:
    assert embedders.choose(semantic_environ("https://example.com/v1/embeddings")).name.startswith("semantic-")


def test_cli_asks_through_the_chosen_embedder_and_its_own_file(
    tmp_path: Path, server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    document = {"tasks": [{"id": "t1", "title": "Reboot", "description": "reboot a stuck daemon"}]}
    environ = semantic_environ(server, AGENTIHOOKS_HOME=str(tmp_path))
    assert cli.main(["ask", QUESTION, "--ledger", "rig"], environ, lambda slug: document, lambda: 1.0) == 0
    answer = json.loads(capsys.readouterr().out)
    assert [hit["ref"] for hit in answer["hits"]] == ["tasks/t1"]
    name = embedders.choose(environ).name
    assert (tmp_path / "knowledge" / f"rig.{name}.sqlite3").is_file()
    assert not (tmp_path / "knowledge" / "rig.sqlite3").exists()


def test_cli_refuses_a_bad_embedder_setting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    environ = {"AGENTIHOOKS_HOME": str(tmp_path), "AGENTIHOOKS_KNOWLEDGE_EMBEDDER": "words"}
    assert cli.main(["ask", "q", "--ledger", "rig"], environ, lambda slug: {}, lambda: 1.0) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "knowledge: AGENTIHOOKS_KNOWLEDGE_EMBEDDER must be hash or semantic\n"
