"""Tests for the SV2-RET-03 migration command line."""

import json
from pathlib import Path

import pytest

from hooks.memory.catalog import Conflict
from scripts.swarm_v2 import migrations as cli
from tests import sv2_ret03_cases as cases

pytestmark = pytest.mark.unit


@pytest.fixture
def path(tmp_path):
    world = cases.World(tmp_path)
    world.catalog.close()
    return str(world.path)


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
    world = cases.World.__new__(cases.World)
    world.path = Path(path)
    world.open()
    assert world.work() == [["c-legacy-1", 7.0], ["c-legacy-2", 7.0]]
    world.catalog.close()


def test_drop_vectors_with_another_role_is_refused(capsys, path):
    argv = ("--actor", "op", "--role", "engineer", "--domain", "corpus-ret03", "--generation", "idx-v1")
    with pytest.raises(Conflict):
        cli.main(["drop-vectors", path, *argv])


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
