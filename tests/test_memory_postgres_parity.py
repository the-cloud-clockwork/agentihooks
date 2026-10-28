"""Postgres with pgvector gives the session tools and search the same answers and rows as SQLite on their fixtures."""

import hashlib
import json
import os

import pytest

from hooks.memory import postgres
from hooks.memory.catalog import Catalog
from tests import sv2_idx02_cases, sv2_idx04_cases, sv2_idx05_cases
from tests.sv2_idx05_cases import Bank, body, grant

URL = os.environ.get("MEMORY_TEST_POSTGRES_URL", "")
VECTOR_COLUMN = 8

pytestmark = pytest.mark.skipif(not URL, reason="the Writer lock workflow sets MEMORY_TEST_POSTGRES_URL")


@pytest.fixture
def on_postgres(monkeypatch):
    import psycopg2

    admin = psycopg2.connect(URL)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
    made = set()

    def opened(path):
        schema = "memory_" + hashlib.sha256(str(path).encode()).hexdigest()[:20]
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        made.add(schema)
        return Catalog(f"{URL}?options=-csearch_path%3D{schema},public")

    def switch():
        for module in (sv2_idx02_cases, sv2_idx04_cases):
            monkeypatch.setattr(module, "Catalog", opened)

    yield switch
    monkeypatch.undo()
    with admin.cursor() as cursor:
        for schema in made:
            cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    admin.close()


def tools_script(bank):
    inputs = bank.inputs
    first = bank.ask("session_list", body(inputs, limit=2))
    second = bank.ask("session_list", body(inputs, limit=2, cursor=first["next_cursor"]))
    searched = bank.ask("session_search", body(inputs, query=inputs["queries"]["hit"], context=1))
    cited = searched["results"][0]["citation"]
    answers = [
        first,
        second,
        searched,
        bank.ask("session_search", body(inputs, query=inputs["queries"]["readme"], group_sessions=True)),
        bank.ask("session_records", body(inputs, citation=cited, before=1, after=1)),
        bank.ask("session_get", body(inputs, session_uid=cited["session_uid"])),
        bank.ask("session_list", body(inputs), granted=grant(inputs, narrow=True)),
        bank.ask("session_list", {**body(inputs), "access_domain": inputs["foreign_corpus"]}),
    ]
    bank.revoke("readme")
    bank.restart()
    answers.append(bank.ask("session_list", body(inputs)))
    return answers, bank.protected(), bank.counter("session_search_scope_rejections")


def test_session_tools_answer_and_store_the_same_on_postgres(on_postgres, tmp_path):
    inputs = json.loads(sv2_idx05_cases.FIXTURE.read_text(encoding="utf-8"))
    on_sqlite = tools_script(Bank(inputs, tmp_path / "sqlite"))
    on_postgres()
    bank = Bank(inputs, tmp_path / "postgres")
    assert isinstance(bank.corpus.catalog.db, postgres.Connection)
    assert tools_script(bank) == on_sqlite
    assert on_sqlite[0][1]["total"] == 3 and on_sqlite[2] >= 1


def normalized(rows):
    vectors = rows.get("chunk_vector", [])
    parsed = [(*row[:VECTOR_COLUMN], [float(value) for value in json.loads(row[VECTOR_COLUMN])]) for row in vectors]
    return {**rows, "chunk_vector": parsed}


def search_script(corpus):
    inputs, provider = corpus.inputs, sv2_idx04_cases.embedder(corpus.inputs)
    requests = [
        sv2_idx04_cases.request(inputs, inputs["queries"]["recall"], context=1),
        sv2_idx04_cases.request(inputs, inputs["queries"]["symbol"], limit=3),
        sv2_idx04_cases.request(inputs, inputs["queries"]["stopwords"], group_sessions=True),
    ]
    answers = [corpus.ask(request, provider=provider) for request in requests]
    answers.append(corpus.ask(requests[0]))
    return answers, normalized(corpus.protected()), corpus.rejections()


def test_hybrid_search_answers_and_stores_the_same_on_postgres(on_postgres, tmp_path):
    inputs = json.loads(sv2_idx04_cases.FIXTURE.read_text(encoding="utf-8"))
    sqlite_corpus = sv2_idx04_cases.Corpus(inputs, tmp_path / "sqlite")
    sqlite_corpus.index()
    on_sqlite = search_script(sqlite_corpus)
    on_postgres()
    corpus = sv2_idx04_cases.Corpus(inputs, tmp_path / "postgres")
    corpus.index()
    stored = corpus.catalog.db.execute("SELECT pg_typeof(vector)::text FROM chunk_vector LIMIT 1").fetchone()
    assert stored == ("vector",)
    assert search_script(corpus) == on_sqlite
    assert on_sqlite[0][0]["mode"] == "hybrid" and on_sqlite[0][3]["mode"] == "lexical_only"
