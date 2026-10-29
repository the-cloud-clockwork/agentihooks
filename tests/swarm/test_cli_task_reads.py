import json

import pytest

from scripts.handoff import envelope as handoff_envelope
from scripts.swarm import cli
from scripts.swarm.store import SwarmError
from tests.swarm.test_cli import _handoff_doc, env, run

_fixture = env


@pytest.fixture
def started(env):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    return ledger


def test_handoff_reads_its_own_task_by_id_and_never_the_tasks_collection(started, tmp_path, capsys, monkeypatch):
    reads, by_id = [], started.task
    monkeypatch.setattr(started, "task", lambda slug, task_id: reads.append((slug, task_id)) or by_id(slug, task_id))
    monkeypatch.setattr(started, "tasks", lambda slug: pytest.fail("handoff read the whole tasks collection"))
    capsys.readouterr()
    assert cli.main(["sw", "--as", "engineer@a1b2c3-0001", "handoff", str(_handoff_doc(tmp_path))]) == 0
    assert ("sw", "t1") in reads and {slug for slug, _ in reads} == {"sw"}
    assert json.loads(capsys.readouterr().out)["envelope"]["claims"] == ["t1"]


def test_handoff_names_claims_unknown_when_the_ledger_refuses_the_task_read(started, tmp_path, capsys, monkeypatch):
    def refuse(slug, task_id):
        raise SwarmError("ledger sw refused")

    monkeypatch.setattr(started, "task", refuse)
    capsys.readouterr()
    assert cli.main(["sw", "--as", "engineer@a1b2c3-0001", "handoff", str(_handoff_doc(tmp_path))]) == 0
    assert json.loads(capsys.readouterr().out)["envelope"]["claims"] == handoff_envelope.UNKNOWN
