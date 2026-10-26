"""Tests for the SV2-RET-03 migration command line."""

import json

import pytest

from hooks.memory.catalog import Catalog, Conflict
from scripts.swarm_v2 import migrations as cli
from tests import sv2_ret03_cases as cases

pytestmark = pytest.mark.unit


@pytest.fixture
def path(tmp_path):
    world = cases.World(tmp_path)
    world.catalog.close()
    return str(world.path)


def work(path: str) -> list:
    catalog = Catalog(path)
    rows = catalog.db.execute("SELECT chunk_id, queued_at FROM embedding_work ORDER BY chunk_id").fetchall()
    catalog.close()
    return rows


def run(capsys, *argv, clock=None) -> dict:
    capsys.readouterr()
    assert cli.main(list(argv), clock=clock) == 0
    return json.loads(capsys.readouterr().out)


def test_status_reports_remaining_rows_and_blocked_vectors(capsys, path):
    answer = run(capsys, "status", path)
    assert answer["state"] == "pending"
    assert answer["migration_rows_remaining"] == 9
    assert [item["mismatched_vectors"] for item in answer["blocked"]] == [2]


def test_staged_commands_carry_a_migration_through_activation(capsys, path):
    assert run(capsys, "startup", path)["added"] == ["0001-event-provenance"]
    status = run(capsys, "backfill", path, "--revision", "0", "--batch-size", "4", "--max-batches", "1")
    assert (status["rows_done"], status["revision"]) == (4, 1)
    status = run(capsys, "backfill", path, "--revision", "1")
    assert status["state"] == "backfilled"
    revision = status["revision"]
    assert run(capsys, "verify", path, "--revision", str(revision))["state"] == "verified"
    assert run(capsys, "activate", path, "--revision", str(revision + 1))["state"] == "active"
    assert run(capsys, "deactivate", path, "--revision", str(revision + 2))["state"] == "verified"


def test_drop_vectors_uses_the_clock_and_the_named_approval(capsys, path):
    argv = ("--actor", "op", "--role", "operator", "--domain", "corpus-ret03", "--generation", "idx-v1")
    answer = run(capsys, "drop-vectors", path, *argv, clock=lambda: 7.0)
    assert answer == {"dropped": ["c-legacy-1", "c-legacy-2"], "requeued": 2}
    assert work(path) == [("c-legacy-1", 7.0), ("c-legacy-2", 7.0)]


def test_drop_vectors_defaults_to_the_wall_clock(capsys, path, monkeypatch):
    monkeypatch.setattr(cli.time, "time", lambda: 11.0)
    argv = ("--actor", "op", "--role", "operator", "--domain", "corpus-ret03", "--generation", "idx-v1")
    run(capsys, "drop-vectors", path, *argv)
    assert work(path) == [("c-legacy-1", 11.0), ("c-legacy-2", 11.0)]


@pytest.mark.parametrize(("role", "domain"), [("engineer", "corpus-ret03"), ("operator", "")])
def test_drop_vectors_without_a_named_operator_approval_changes_nothing(capsys, path, role, domain):
    argv = ("--actor", "op", "--role", role, "--domain", domain, "--generation", "idx-v1")
    with pytest.raises(Conflict):
        cli.main(["drop-vectors", path, *argv])
    assert work(path) == []
    assert run(capsys, "status", path)["blocked"][0]["mismatched_vectors"] == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["verify"],
        ["status", "--revision", "1"],
        ["drop-vectors", "--actor", "op", "--role", "operator", "--domain", "d"],
        ["status", "--actor", "op"],
    ],
)
def test_arguments_belong_to_their_command(capsys, path, argv):
    with pytest.raises(SystemExit) as raised:
        cli.main([argv[0], path, *argv[1:]])
    assert raised.value.code == 2
