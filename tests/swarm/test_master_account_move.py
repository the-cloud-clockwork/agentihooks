import pytest

pytestmark = pytest.mark.unit

LAUNCH = {"profile": "master", "harness": "claude", "account": "login", "model": "opus", "effort": "high"}


class _Chat:
    def __init__(self):
        self.lines = []

    def say(self, slug, text, by=None):
        self.lines.append((slug, text, by))


def _moved(previous, account, client):
    from scripts.swarm import master_move
    from scripts.swarm.store import AgentRecord

    task = {"handoff_envelope": {"launch": {**LAUNCH, "account": previous}}} if previous is not None else {}
    record = AgentRecord("master@a1b2c3-0002", "master", "master", harness="claude", account=account)
    return master_move.account_moved("proof", task, record, client)


def test_a_master_that_lands_on_another_account_names_it_in_ledger_chat():
    chat = _Chat()
    assert _moved("login", "second", chat) == ["master master@a1b2c3-0002 moved from account login to second"]
    assert chat.lines == [
        (
            "proof",
            "The master now runs on claude account second; it moved from account login. "
            "Open that account on claude.ai to reach it.",
            None,
        )
    ]


@pytest.mark.parametrize(
    ("previous", "account"), [("second", "second"), ("", "second"), (None, "second"), ("login", "")]
)
def test_a_master_that_keeps_its_account_posts_nothing(previous, account):
    chat = _Chat()
    assert _moved(previous, account, chat) == []
    assert chat.lines == []


def test_the_tick_names_the_new_account_when_a_master_successor_moves(monkeypatch):
    from scripts.swarm import ledger_client
    from scripts.swarm.store import MASTER
    from scripts.swarm.tick import Placed, _master
    from tests.swarm.test_tick_master import direct

    chat = _Chat()
    monkeypatch.setattr(ledger_client, "LedgerClient", lambda: chat)
    swarm, runtime = direct()
    spawn = runtime.spawn

    def moved(config, lane, name, task):
        placed = spawn(config, lane, name, task)
        return Placed(placed.pane_id, placed.harness, account="second")

    runtime.spawn = moved
    swarm.put_handoff("sw", MASTER, "Continue the master task.", envelope={"launch": LAUNCH})
    actions = _master("sw", swarm.config("sw"), swarm, runtime, 1)
    name = runtime.masters[0][0]
    assert actions == [f"spawned master {name}", f"master {name} moved from account login to second"]
    assert [(slug, by) for slug, _, by in chat.lines] == [("sw", None)]
    assert "account second" in chat.lines[0][1]
