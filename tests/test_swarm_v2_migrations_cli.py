"""Tests for the SV2-RET-03 migration command line."""

import json

import pytest

from hooks.memory.catalog import Catalog, Conflict
from scripts.gates.base import Who
from scripts.swarm_v2 import migrations as cli
from tests import sv2_ret03_cases as cases

pytestmark = pytest.mark.unit
SCOPE = ("--domain", "corpus-ret03", "--generation", "idx-v1")
OPERATOR = Who()


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


def run(capsys, *argv, clock=None, who=OPERATOR) -> dict:
    capsys.readouterr()
    assert cli.main(list(argv), clock=clock, who=who) == 0
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


def test_drop_vectors_from_the_operator_shell_uses_the_clock(capsys, path):
    answer = run(capsys, "drop-vectors", path, *SCOPE, clock=lambda: 7.0)
    assert answer == {"dropped": ["c-legacy-1", "c-legacy-2"], "requeued": ["c-legacy-1", "c-legacy-2"]}
    assert work(path) == [("c-legacy-1", 7.0), ("c-legacy-2", 7.0)]


def test_drop_vectors_defaults_to_the_wall_clock_and_the_session_identity(capsys, path, monkeypatch):
    monkeypatch.setattr(cli.time, "time", lambda: 11.0)
    monkeypatch.setattr(cli.Who, "from_env", classmethod(lambda cls: OPERATOR))
    capsys.readouterr()
    assert cli.main(["drop-vectors", path, *SCOPE]) == 0
    assert work(path) == [("c-legacy-1", 11.0), ("c-legacy-2", 11.0)]


@pytest.mark.parametrize(
    ("who", "scope"),
    [
        (Who(name="engineer@1"), SCOPE),
        (Who(swarm="rig"), SCOPE),
        (OPERATOR, ("--domain", "", "--generation", "idx-v1")),
    ],
)
def test_drop_vectors_without_an_operator_approval_changes_nothing(capsys, path, who, scope):
    with pytest.raises(Conflict):
        cli.main(["drop-vectors", path, *scope], who=who)
    assert work(path) == []
    assert run(capsys, "status", path)["blocked"][0]["mismatched_vectors"] == 2


@pytest.mark.parametrize(
    ("who", "expected"),
    [
        (Who(name="engineer@1", swarm="rig"), ("engineer@1", "agent")),
        (Who(swarm="rig"), ("rig", "agent")),
        (OPERATOR, ("operator", "operator")),
    ],
)
def test_the_approval_comes_from_the_session_identity(who, expected):
    found = cli.approval(who, "d", "g")
    assert (found.actor, found.role, found.migration_id, found.access_domain, found.index_generation) == (
        *expected,
        "0002-vector-dimension",
        "d",
        "g",
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["verify"],
        ["status", "--revision", "1"],
        ["drop-vectors", "--domain", "d"],
        ["status", "--generation", "g"],
    ],
)
def test_arguments_belong_to_their_command(capsys, path, argv):
    with pytest.raises(SystemExit) as raised:
        cli.main([argv[0], path, *argv[1:]], who=OPERATOR)
    assert raised.value.code == 2
