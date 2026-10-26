import pytest

from tests.swarm.test_cli import env, run

_fixture = env
CI = "ci@a1b2c3-0001"
ESCALATED = {
    "attempts": [{"after_critic": False}, {"after_critic": False}, {"after_critic": True}],
    "critic": {"resolution": "scale the pool"},
    "escalation": {"reason": "the pool quota is spent"},
}


@pytest.fixture
def started(env):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    return store, ledger


def test_a_block_at_delegate_without_the_protocol_is_refused(started, capsys):
    _, ledger = started
    capsys.readouterr()
    assert run("sw", "--as", CI, "block", "waiting on a token") == 1
    assert "resolve attempt tasks/t2" in capsys.readouterr().err
    assert ledger.rows["t2"].get("state") != "blocked"


def test_a_block_after_the_escalation_lands(started):
    _, ledger = started
    ledger.rows["t2"]["blocker"] = ESCALATED
    assert run("sw", "--as", CI, "block", "waiting on a token") == 0
    assert ledger.rows["t2"]["state"] == "blocked"


def test_a_block_at_assist_needs_no_record(started):
    store, ledger = started
    store.update("sw", autonomy="assist")
    assert run("sw", "--as", CI, "block", "waiting on a token") == 0
    assert ledger.rows["t2"]["state"] == "blocked"
