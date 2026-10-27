import pytest

from scripts.inbox import channel, cli, exits
from scripts.inbox.store import InboxStore
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

SLUG = "sw"
DOOR = f"master@{SLUG}"
ENG = AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", seat=f"eng-1@{SLUG}")
BOSS = AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm", seat=DOOR)
SPARE = AgentRecord("sw-master-2", MASTER, MASTER, pane_id="pm2", seat=f"master-2@{SLUG}")


@pytest.fixture
def inbox(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 1, 1))
    for agent in (ENG, BOSS, SPARE):
        store.put_agent(SLUG, agent)
        store.seats.occupy(agent.seat, agent.name, 1)
    box = InboxStore(store.redis)
    monkeypatch.setattr(cli, "connect", lambda: box)
    monkeypatch.setattr(cli, "registered_name", lambda: "")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return box


def run_as(monkeypatch, agent, *argv):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", agent.name)
    return cli.main(list(argv))


def test_a_lane_agent_send_to_the_operator_goes_to_its_front_door(inbox, monkeypatch):
    assert run_as(monkeypatch, ENG, "send", "operator", "the pull request is in the merge queue") == 0
    assert inbox.inbox("operator") == []
    [item] = inbox.open_mail(DOOR)
    assert (item.sender, item.text) == (ENG.name, "the pull request is in the merge queue")


def test_a_second_master_send_to_the_operator_goes_to_the_front_door(inbox, monkeypatch):
    assert run_as(monkeypatch, SPARE, "send", "operator", "the phase is planned") == 0
    assert inbox.inbox("operator") == []
    assert [item.sender for item in inbox.open_mail(DOOR)] == [SPARE.name]


def test_the_front_door_never_writes_to_the_operator_first(inbox, monkeypatch, capsys):
    assert run_as(monkeypatch, BOSS, "send", "operator", "the phase is planned") == 1
    assert "first" in capsys.readouterr().err and inbox.inbox("operator") == []


def test_a_session_outside_any_swarm_never_writes_to_the_operator_first(inbox, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "alice")
    assert cli.main(["send", "operator", "the build is green"]) == 1
    assert "first" in capsys.readouterr().err and inbox.inbox("operator") == []


def test_an_unseated_master_answers_the_operator_through_the_front_door(inbox):
    asked = inbox.send("operator", "sw-master-3", "status")
    answer = inbox.reply(asked.id, "sw-master-3", "two tasks left")
    assert answer.address == DOOR and inbox.inbox("operator") == []


def test_a_lane_agent_reply_to_an_operator_item_goes_to_its_front_door_with_his_item(inbox, monkeypatch):
    asked = inbox.send("operator", ENG.name, "is the fix merged")
    assert run_as(monkeypatch, ENG, "reply", asked.id, "handed", "off", "to", "my", "seat") == 0
    assert inbox.inbox("operator") == []
    held = {item.id: item for item in inbox.open_mail(DOOR)}
    assert asked.id in held and held[asked.id].sender == "operator"
    [answer] = [item for item in held.values() if item.sender == ENG.name]
    assert (answer.text, answer.ref) == ("handed off to my seat", asked.id)


def test_a_lane_agent_channel_reply_to_an_operator_item_goes_to_its_front_door(inbox):
    asked = inbox.send("operator", ENG.name, "is the fix merged")
    assert channel.answer(inbox, ENG.name, asked.id, "merged").startswith(f"sent to {DOOR}")
    assert inbox.inbox("operator") == []


def test_the_front_door_reply_to_an_operator_item_still_posts(inbox, monkeypatch):
    asked = inbox.send("operator", BOSS.name, "status")
    assert run_as(monkeypatch, BOSS, "reply", asked.id, "two", "tasks", "left") == 0
    [answer] = inbox.inbox("operator")
    assert (answer.sender, answer.text, answer.ref) == (BOSS.name, "two tasks left", asked.id)
    assert inbox.get(asked.id).state == "done"


def test_a_close_notice_for_an_operator_item_goes_to_the_front_door(inbox):
    asked = inbox.send("operator", ENG.name, "is the fix merged")
    exits.settle(inbox, ENG.name, "", "finished its task and exited")
    assert inbox.get(asked.id).state == "cancelled"
    assert inbox.inbox("operator") == []
    assert any(asked.id in item.text and item.sender == exits.BY for item in inbox.open_mail(DOOR))
