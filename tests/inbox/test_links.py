import pytest

from scripts.inbox import links
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

LINKS = [
    {"from": "eng", "to": "ci", "kind": "delegates-to"},
    {"from": "ci-1", "to": "eng-1", "kind": "can-observe"},
]


@pytest.fixture
def inbox():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    swarm = RedisStore(redis)
    swarm.create(SwarmConfig("rig", "/repo", 2, 1, links=LINKS))
    swarm.create(SwarmConfig("open", "/repo", 2, 1))
    store = InboxStore(redis)
    for slug in ("rig", "open"):
        for seat in ("eng-1", "ci-1", "master"):
            store.seats.occupy(f"{seat}@{slug}", f"{slug}-{seat.split('-')[0]}-1", at=1)
    return store


def test_a_delegates_to_link_allows_the_send(inbox):
    links.check_send(inbox, "rig-eng-1", "ci-1@rig")
    links.check_send(inbox, "eng-1@rig", "rig-ci-1")


def test_a_can_observe_link_refuses_the_send_and_names_why(inbox):
    with pytest.raises(InboxError, match="ci-1@rig can only observe eng-1@rig"):
        links.check_send(inbox, "rig-ci-1", "eng-1@rig")


def test_a_seat_with_no_link_to_the_target_is_refused(inbox):
    with pytest.raises(InboxError, match="no delegates-to link from eng-1@rig to eng-2@rig"):
        links.check_send(inbox, "rig-eng-1", "eng-2@rig")


def test_a_swarm_without_links_is_unrestricted(inbox):
    links.check_send(inbox, "open-ci-1", "eng-1@open")
    links.check_observe(inbox, "open-ci-1", "eng-1@open")


@pytest.mark.parametrize("sender", ["operator", "rig-master-1", "master@rig", "someone-else"])
def test_the_operator_the_master_and_seatless_senders_are_never_blocked(inbox, sender):
    links.check_send(inbox, sender, "eng-1@rig")
    links.check_observe(inbox, sender, "eng-1@rig")


@pytest.mark.parametrize("target", ["operator", "master@rig", "rig-master-1", "eng-1@open"])
def test_sends_to_the_operator_the_master_and_other_swarms_stay_open(inbox, target):
    links.check_send(inbox, "rig-ci-1", target)


def test_can_observe_and_delegates_to_both_allow_reading_a_seats_items(inbox):
    links.check_observe(inbox, "rig-ci-1", "eng-1@rig")
    links.check_observe(inbox, "rig-eng-1", "ci-1@rig")
    with pytest.raises(InboxError, match="no link from eng-1@rig to eng-2@rig"):
        links.check_observe(inbox, "rig-eng-1", "eng-2@rig")


def test_msg_send_refuses_a_can_observe_link_and_sends_nothing(inbox, monkeypatch, capsys):
    from scripts.inbox import cli

    monkeypatch.setattr(cli, "connect", lambda: inbox)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "rig-ci-1")
    assert cli.main(["send", "eng-1@rig", "hello"]) == 1
    assert "can only observe" in capsys.readouterr().err
    assert inbox.inbox("eng-1@rig") == []


def test_msg_inbox_of_a_seat_reads_its_items_through_a_link(inbox, monkeypatch, capsys):
    from scripts.inbox import cli

    monkeypatch.setattr(cli, "connect", lambda: inbox)
    item = inbox.send("operator", "eng-1@rig", "take the flaky test")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "rig-ci-1")
    assert cli.main(["inbox", "--of", "eng-1@rig"]) == 0
    assert capsys.readouterr().out.startswith(item.id)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "rig-eng-1")
    assert cli.main(["inbox", "--of", "eng-2@rig"]) == 1


def test_swarm_say_refuses_before_sending_when_a_recipient_is_refused(inbox):
    from scripts.swarm import delivery
    from scripts.swarm.store import AgentRecord

    swarm = RedisStore(inbox.redis)
    for name, lane, seat in (("rig-eng-1", "eng", "eng-1@rig"), ("rig-ci-1", "ci", "ci-1@rig")):
        swarm.put_agent("rig", AgentRecord(name, lane, "t", seat=seat))
    with pytest.raises(InboxError, match="can only observe"):
        delivery.send(swarm, "rig", "hi", sender="rig-ci-1", to="")
    assert inbox.inbox("rig-eng-1") == []
    assert delivery.send(swarm, "rig", "hi", sender="rig-eng-1", to="ci") == ["rig-ci-1"]
