from types import SimpleNamespace

import pytest

from scripts.doctor import detect, traces_read
from scripts.swarm import idle, waits


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.mark.parametrize("kind", [None, "checks", "reply", "task"])
def test_wait_history_survives_expiry_and_explicit_end(redis, kind):
    on = {"kind": kind, "target": "proof"} if kind else None
    idle.declare_wait(redis, "sw", "engineer", 3_660_000, "proof", 60_000, on=on)
    expected = {"at": 60_000, "until": 3_660_000, "reason": "proof", **({"on": on} if on else {})}
    assert idle.wait_history(redis, "sw", "engineer") == [expected]
    idle.end_wait(redis, "sw", "engineer", 3_000_000)
    assert idle.wait_history(redis, "sw", "engineer") == [{**expected, "until": 3_000_000}]
    idle.declare_wait(redis, "sw", "engineer", 4_000_000, "another proof", 3_100_000)
    redis.delete(idle.key("sw", "wait", "engineer"))
    assert idle.wait_history(redis, "sw", "engineer") == [
        {**expected, "until": 3_000_000},
        {"at": 3_100_000, "until": 4_000_000, "reason": "another proof"},
    ]
    assert redis.ttl(idle.key("sw", "wait-history", "engineer")) == -1
    assert idle.wait_history(redis, "other", "engineer") == []
    assert idle.wait_history(redis, "sw", "someone-else") == []


def test_redeclaring_a_wait_records_the_end_of_the_previous_one(redis):
    idle.declare_wait(redis, "sw", "engineer", 4_000_000, "first", 60_000)
    idle.declare_wait(redis, "sw", "engineer", 5_000_000, "second", 120_000)
    assert idle.wait_history(redis, "sw", "engineer") == [
        {"at": 60_000, "until": 120_000, "reason": "first"},
        {"at": 120_000, "until": 5_000_000, "reason": "second"},
    ]


def test_renamed_agents_keep_ended_history_under_both_names(redis):
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(redis)
    store.create(SwarmConfig("sw", "/home/x/agentihooks", 2, 1))
    idle.declare_wait(redis, "sw", "old-engineer", 3_660_000, "old proof", 60_000)
    idle.end_wait(redis, "sw", "old-engineer", 3_660_000)
    new = store.next_name("sw", "eng")
    store.names.alias("old-engineer", new)
    idle.declare_wait(redis, "sw", new, 5_000_000, "new proof", 4_000_000)
    expected = [
        {"at": 60_000, "until": 3_660_000, "reason": "old proof"},
        {"at": 4_000_000, "until": 5_000_000, "reason": "new proof"},
    ]
    assert idle.wait_history(redis, "sw", new) == expected
    assert idle.wait_history(redis, "sw", "old-engineer") == expected


def test_tick_resolution_records_the_actual_end_of_the_wait(redis):
    agent = SimpleNamespace(name="engineer", state="working", task="own", seat="")
    store = SimpleNamespace(redis=redis, agents=lambda slug: [agent])
    inbox = SimpleNamespace(send=lambda *args: None)
    idle.declare_wait(redis, "sw", "engineer", 5_000_000, "proof", 60_000, on=waits.on("task", "proof"))
    assert waits.end_pass(store, "sw", {"proof": {"state": "done"}}, inbox, None, 3_660_000, None)
    assert idle.wait(redis, "sw", "engineer") is None
    assert idle.wait_history(redis, "sw", "engineer") == [
        {
            "at": 60_000,
            "until": 3_660_000,
            "reason": "proof",
            "on": {"kind": "task", "target": "proof"},
        }
    ]


@pytest.mark.parametrize("until, expected", [(3_660_000, []), (1_800_000, ["turn-gap/session"])])
def test_doctor_reads_ended_wait_history_for_historical_agents(redis, monkeypatch, until, expected):
    record = {
        "traces": [
            {
                "name": "engineer",
                "tags": ["agent:engineer"],
                "session_id": "session",
                "observations": [
                    {"type": "SPAN", "turn": 1, "start": 0, "end": 60_000},
                    {"type": "SPAN", "turn": 2, "start": 3_660_000, "end": 3_720_000},
                ],
            }
        ],
        "sessions": [],
        "merged": [],
    }
    get = object()

    def client(env):
        assert env == {}
        return get

    def read(slug, tasks, agents, now_ms, reader):
        assert (slug, tasks, agents, now_ms, reader) == ("sw", [], [], 4_000_000, get)
        return record

    monkeypatch.setattr(traces_read, "client", client)
    monkeypatch.setattr(traces_read, "record", read)

    def rows(slug):
        assert slug == "sw"
        return []

    store = SimpleNamespace(redis=redis, agents=rows)
    ledger = SimpleNamespace(tasks=rows)
    idle.declare_wait(redis, "sw", "engineer", until, "proof", 60_000)
    idle.end_wait(redis, "sw", "engineer", until)
    found = detect.readers(store, ledger, "sw", 4_000_000, environ={})["trace"]()
    assert [f.id for f in found if f.kind == "turn gap"] == expected


def test_doctor_reads_wait_history_only_for_agents_with_a_long_turn_gap(redis, monkeypatch):
    def trace(name, second_start):
        return {
            "name": name,
            "tags": [f"agent:{name}"],
            "session_id": f"{name}-session",
            "observations": [
                {"type": "SPAN", "turn": 1, "start": 0, "end": 60_000},
                {"type": "SPAN", "turn": 2, "start": second_start, "end": second_start + 60_000},
            ],
        }

    record = {"traces": [trace("engineer", 3_660_000), trace("ci", 120_000)], "sessions": [], "merged": []}
    monkeypatch.setattr(traces_read, "client", lambda env: None)
    monkeypatch.setattr(traces_read, "record", lambda *args: record)
    asked = []
    monkeypatch.setattr(idle, "wait_history", lambda r, slug, name: asked.append((slug, name)) or [])
    store = SimpleNamespace(redis=redis, agents=lambda slug: [])
    found = detect.readers(store, SimpleNamespace(tasks=lambda slug: []), "sw", 4_000_000, environ={})["trace"]()
    assert asked == [("sw", "engineer")]
    assert [f.id for f in found if f.kind == "turn gap"] == ["turn-gap/engineer-session"]
    assert record["waits_unread"] == []
