import json

import pytest

from scripts.handoff import transfers
from scripts.swarm import cli
from tests.swarm.test_cli import env

pytestmark = pytest.mark.unit
_fixture = env
DOC = "# Handoff v2\n## Next\nRead the saved proof.\n## Read first\nNone\n"


def test_confirmation_command_records_continuity_but_never_infers_binding(env, capsys):
    store, ledger, runtime = env
    cli.main(["sw", "create", "--repo", "/repo"])
    cli.main(["sw", "start"])
    agent = next(a for a in store.agents("sw") if a.lane == "eng")
    row = transfers.record(store, "sw", agent, "recycle", DOC, 1)
    transfers.attach(store, "sw", agent, 2)
    assert cli.main(["sw", "--as", agent.name, "confirm-handoff", row["id"], "--next", "Read the saved proof."]) == 0
    capsys.readouterr()
    assert cli.main(["sw", "status", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)["transfers"][0]
    assert result["continuity"]["state"] == "confirmed"
    assert result["binding"]["state"] == "pending"
    runtime.live.discard(agent.name)
    assert cli.main(["sw", "--as", agent.name, "confirm-handoff", row["id"], "--next", "Read the saved proof."]) == 1


def test_a_worker_cannot_decide_another_seats_restore(env, capsys):
    store, _, _ = env
    cli.main(["sw", "create", "--repo", "/repo"])
    cli.main(["sw", "start"])
    agent = next(a for a in store.agents("sw") if a.lane == "eng")
    assert cli.main(["sw", "--as", agent.name, "restore-decision", agent.name, "fresh"]) == 1
    assert "Only the master or operator" in capsys.readouterr().err
