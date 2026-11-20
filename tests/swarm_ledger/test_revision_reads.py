"""Tests for revision reads: the ack revision and the guard read of a members, metadata or record thread path read
only that path's rows, never the whole ledger."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api.services import Services
from scripts.swarm_ledger.repository import sqlite
from tests.swarm_ledger.test_sqlite import CONTENT, store

PATHS = ("members", "metadata", "phases/p1/comments", "tasks/t1/comments")


def over(repository: sqlite.SQLiteLedgerRepository) -> Services:
    return cast(Services, SimpleNamespace(repository=repository))


@pytest.fixture
def stored(tmp_path: Path) -> sqlite.SQLiteLedgerRepository:
    repository = store(tmp_path)
    repository.create("ledger", CONTENT)
    repository.apply_ops(
        "ledger",
        ops=[
            {"op": "join", "id": "j1", "by": "reader"},
            {"op": "add", "id": "c1", "thread": "phases/p1/comments", "text": "A note", "by": "reader"},
        ],
    )
    return repository


@pytest.mark.feature("ledger-cold-write")
@pytest.mark.parametrize("path", PATHS)
def test_a_revision_read_of_a_non_record_path_reads_only_its_rows(
    stored: sqlite.SQLiteLedgerRepository, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    entry = stored.current("ledger")
    whole = resources.read(entry.state, path, {}, entry.views)

    def refused(slug: str) -> None:
        raise AssertionError(f"{path} read the whole ledger {slug}")

    monkeypatch.setattr(stored, "current", refused)
    assert routes.ledger_read(over(stored), "ledger", path, {}) == whole


def test_a_paged_members_read_still_reads_the_whole_ledger(
    stored: sqlite.SQLiteLedgerRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    current = stored.current
    monkeypatch.setattr(stored, "current", lambda slug: reads.append(slug) or current(slug))
    routes.ledger_read(over(stored), "ledger", "members", {"limit": 1})
    assert reads == ["ledger"]


def test_a_thread_read_of_a_missing_record_is_missing(stored: sqlite.SQLiteLedgerRepository) -> None:
    with pytest.raises(routes.APIError) as refused:
        routes.ledger_read(over(stored), "ledger", "tasks/t9/comments", {})
    assert refused.value.status == 404


@pytest.mark.parametrize(
    ("path", "keys"),
    [
        ("members", ("_meta.members",)),
        ("tasks/t1", ("tasks/t1",)),
        ("tasks/t1/comments", ("tasks/t1",)),
        ("phases/p1/comments", ("phases/p1",)),
        ("tasks/t1/title", ()),
        ("alerts/a1/comments", ()),
        ("tasks", ()),
        ("chat", ()),
        ("members/reader", ()),
    ],
)
def test_own_rows_names_the_stored_parts_a_path_reads(path: str, keys: tuple[str, ...]) -> None:
    assert routes.own_rows(path) == keys


def test_the_metadata_rows_are_every_part_the_metadata_resource_holds() -> None:
    assert routes.own_rows("metadata") == resources.METADATA_KEYS
    assert set(resources.METADATA_KEYS) == {
        *resources.METADATA_FIELDS,
        *(f"_meta.{key}" for key in resources.METADATA_META),
    }


@pytest.mark.feature("ledger-cold-write")
def test_ack_reads_the_revision_from_the_metadata_rows_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reads: list[str] = []
    writes: list[list[dict[str, object]] | None] = []

    def resource(slug: str, path: str) -> dict[str, object]:
        reads.append(path)
        return {"_meta": {"rev": 7}}

    def call(slug: str, ops: list[dict[str, object]] | None = None) -> dict[str, object]:
        writes.append(ops)
        return {"applied": [op["id"] for op in ops or []], "rejected": [], "_meta": {"warnings": []}}

    monkeypatch.setattr(ledger, "resource", resource)
    monkeypatch.setattr(ledger, "call", call)
    ledger.cmd_ack(argparse.Namespace(slug="ledger", name="reader", rev=None))
    assert reads == ["metadata"]
    assert [[(op["op"], op["rev"]) for op in ops or []] for ops in writes] == [[("ack", 7)]]
    assert json.loads(capsys.readouterr().out) == {"acked": 7}
