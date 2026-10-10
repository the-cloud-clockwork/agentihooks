import json

import pytest

from scripts.inbox.seats import SeatRegistry
from scripts.swarm_v2 import masters, masters_channel
from scripts.swarm_v2.masters_channel import MastersChannel, Post

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "sw"
LEAD, SECOND, ENG = f"master@{SLUG}", f"master-2@{SLUG}", f"eng-1@{SLUG}"


@pytest.fixture
def channel():
    import fakeredis

    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    masters.MasterSeats(redis).set_count(SLUG, 2)
    seats = SeatRegistry(redis)
    seats.occupy(LEAD, "sw-master-1", 1)
    seats.occupy(SECOND, "sw-master-2", 1)
    seats.occupy(ENG, "sw-eng-1", 1)
    return MastersChannel(redis)


def test_two_masters_post_and_read_each_others_posts(channel):
    channel.post(SLUG, "sw-master-1", "I take the deploy phase", 10)
    channel.post(SLUG, "sw-master-2", "agreed", 11)

    expected = [
        Post(1, LEAD, "sw-master-1", "I take the deploy phase", 10),
        Post(2, SECOND, "sw-master-2", "agreed", 11),
    ]
    assert channel.read(SLUG, "sw-master-1") == expected
    assert channel.read(SLUG, "sw-master-2") == expected


@pytest.mark.parametrize("name", ["sw-eng-1", "stranger"])
def test_an_engineer_or_unseated_session_can_neither_post_nor_read(channel, name):
    channel.post(SLUG, "sw-master-1", "private", 10)
    held = ENG if name == "sw-eng-1" else "no seat"
    refusal = f"only master seats of {SLUG} read and write its masters channel; {name} holds {held}"

    for act in (lambda: channel.post(SLUG, name, "let me in", 11), lambda: channel.read(SLUG, name)):
        with pytest.raises(masters.MasterError) as refused:
            act()
        assert str(refused.value) == refusal
    with pytest.raises(masters.MasterError):
        channel.unread(SLUG, name)
    assert [p.text for p in channel.read(SLUG, "sw-master-1")] == ["private"]


def test_a_seat_beyond_the_master_count_is_refused(channel):
    masters.MasterSeats(channel.redis).set_count(SLUG, 1)

    with pytest.raises(masters.MasterError):
        channel.post(SLUG, "sw-master-2", "still here", 10)
    assert channel.post(SLUG, "sw-master-1", "alone now", 11).number == 1


def test_a_master_of_another_swarm_is_refused(channel):
    masters.MasterSeats(channel.redis).set_count("other", 2)
    SeatRegistry(channel.redis).occupy("master@other", "other-master-1", 1)

    with pytest.raises(masters.MasterError):
        channel.post(SLUG, "other-master-1", "hello", 10)
    with pytest.raises(masters.MasterError):
        channel.post("other", "sw-master-1", "hello", 10)


def test_posts_are_durable_with_no_expiry(channel):
    channel.post(SLUG, "sw-master-1", "kept", 10)

    assert channel.redis.ttl(channel.key(SLUG)) == -1
    assert json.loads(channel.redis.lrange(channel.key(SLUG), 0, -1)[0])["text"] == "kept"
    assert MastersChannel(channel.redis).read(SLUG, "sw-master-2")[0].text == "kept"


def test_unread_moves_each_seats_cursor_and_a_successor_continues(channel):
    channel.post(SLUG, "sw-master-1", "one", 10)
    assert [p.text for p in channel.unread(SLUG, "sw-master-2")] == ["one"]
    assert channel.unread(SLUG, "sw-master-2") == []
    channel.post(SLUG, "sw-master-1", "two", 11)

    SeatRegistry(channel.redis).occupy(SECOND, "sw-master-3", 2)

    assert [p.text for p in channel.unread(SLUG, "sw-master-3")] == ["two"]
    assert [p.text for p in channel.unread(SLUG, "sw-master-1")] == ["one", "two"]
    with pytest.raises(masters.MasterError):
        channel.read(SLUG, "sw-master-2")


def test_an_empty_post_is_refused(channel):
    with pytest.raises(masters.MasterError) as refused:
        channel.post(SLUG, "sw-master-1", "  ", 10)
    assert str(refused.value) == "a masters channel post needs text"
    assert channel.read(SLUG, "sw-master-1") == []


def test_masters_channel_names_are_reserved():
    assert masters_channel.channel_name(SLUG) == "masters.sw"
    assert masters_channel.reserved("masters.sw")
    assert not masters_channel.reserved("brain")
    assert not masters_channel.reserved("ops-masters.sw")


def test_channel_publish_refuses_a_masters_channel(monkeypatch):
    from hooks.mcp import channels

    tools = {}

    class Mcp:
        def tool(self):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    published = []
    monkeypatch.setattr("hooks.context.broadcast.create_broadcast", lambda **kw: published.append(kw) or "id")
    channels.register(Mcp())

    answer = json.loads(tools["channel_publish"]("masters.sw", "secret"))

    assert answer == {
        "success": False,
        "error": "masters.sw is a masters channel: master seats post with agentihooks swarm sw masters-channel say",
    }
    assert published == []
    assert json.loads(tools["channel_publish"]("brain", "hello"))["success"] is True


def test_swarm_masters_channel_command_lets_masters_talk_and_refuses_the_engineer(monkeypatch, capsys):
    import fakeredis

    from scripts.gates import Who
    from scripts.swarm import cli
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 1, 1))
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    for seat, name in ((LEAD, "sw-master-1"), (SECOND, "sw-master-2"), (ENG, "sw-eng-1")):
        store.seats.occupy(seat, name, 1)
    monkeypatch.setattr(cli, "connect", lambda: store)

    def act(name, *argv):
        monkeypatch.setattr(Who, "from_env", classmethod(lambda cls, environ=None: cls(name=name)))
        code = cli.main([SLUG, "masters-channel", *argv])
        return code, capsys.readouterr()

    code, said = act("sw-master-1", "say", "split the phases")
    assert code == 0
    assert json.loads(said.out)["seat"] == LEAD
    code, read = act("sw-master-2", "read")
    assert (code, [p["text"] for p in json.loads(read.out)]) == (0, ["split the phases"])
    assert json.loads(act("sw-master-2", "read")[1].out) == []
    assert [p["by"] for p in json.loads(act("sw-master-2", "read", "--all")[1].out)] == ["sw-master-1"]

    for argv in (("say", "me too"), ("read", "--all")):
        code, refused = act("sw-eng-1", *argv)
        assert code != 0
        assert refused.out == ""
        assert refused.err.splitlines()[-1] == (
            f"swarm: only master seats of {SLUG} read and write its masters channel; sw-eng-1 holds {ENG}"
        )
