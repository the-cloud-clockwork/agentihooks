"""A worker hive roll: the replacement pod joins the same hive and takes agents while the old pod drains its own."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.hive import lanes, placement, registry, remove
from scripts.swarm.store import AgentRecord, RedisStore
from tests.hive.test_controller_launch import setup  # noqa: F401

pytestmark = pytest.mark.unit

NOW = 1_793_166_000_000
HIVE = "06db8fa06259f56a"
OLD, NEW = "worker-hive-old", "worker-hive-new"


@pytest.fixture
def client(monkeypatch):
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", OLD)
    monkeypatch.setattr(registry, "wall_ms", lambda: NOW)
    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    registry.beat(redis, HIVE, {"heartbeat_at": NOW - 1_000})
    return redis


def _shown(client: Any) -> dict[str, Any]:
    record = registry.show(client, HIVE)
    assert record is not None
    return record


@pytest.mark.feature("hive-roll-member-drain")
def test_a_replacement_pod_joins_as_a_second_live_member_and_a_quiet_member_is_forgotten(client):
    registry.join(client, HIVE, OLD, NOW - 5_000)
    registry.join(client, HIVE, NEW, NOW)

    assert _shown(client)["members"] == {
        OLD: {"heartbeat_at": NOW - 5_000, "draining": "no"},
        NEW: {"heartbeat_at": NOW, "draining": "no"},
    }

    registry.join(client, HIVE, NEW, NOW + registry.LIVE_MS - 5_000)
    assert set(_shown(client)["members"]) == {OLD, NEW}
    registry.join(client, HIVE, NEW, NOW + registry.LIVE_MS - 4_999)
    assert _shown(client)["members"] == {NEW: {"heartbeat_at": NOW + registry.LIVE_MS - 4_999, "draining": "no"}}


@pytest.mark.feature("hive-roll-member-drain")
def test_the_old_pod_drain_leaves_the_hive_open_while_the_replacement_takes_agents(client):
    registry.join(client, HIVE, OLD, NOW - 5_000)
    registry.join(client, HIVE, NEW, NOW - 1_000)

    assert registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping", NOW + 600_000)) is True

    record = _shown(client)
    assert record.get("draining") != "yes"
    assert "drains" not in record
    assert record["members"] == {
        OLD: {"heartbeat_at": NOW - 5_000, "draining": "yes"},
        NEW: {"heartbeat_at": NOW - 1_000, "draining": "no"},
    }
    assert (registry.member_draining(client, HIVE, OLD), registry.member_draining(client, HIVE, NEW)) == (True, False)
    assert placement.hosts("eng", record)


@pytest.mark.feature("hive-roll-member-drain")
def test_a_sole_or_stale_replacement_drains_the_whole_hive_as_before(client):
    registry.join(client, HIVE, OLD, NOW - 5_000)
    registry.join(client, HIVE, NEW, NOW - registry.LIVE_MS - 1)

    assert registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping", NOW + 600_000)) is False

    record = _shown(client)
    assert record["draining"] == "yes"
    assert record["members"][OLD] == {"heartbeat_at": NOW - 5_000, "draining": "yes"}
    [entry] = record["drains"]
    assert (entry["draining"], entry["owner"], entry["reason"], entry["deadline_at"]) == (
        "yes",
        OLD,
        "the hive is stopping",
        NOW + 600_000,
    )
    assert not placement.hosts("eng", record)


@pytest.mark.feature("hive-roll-member-drain")
def test_a_replacement_last_heard_exactly_one_live_window_ago_still_keeps_the_hive_open(client):
    registry.join(client, HIVE, NEW, NOW - registry.LIVE_MS)

    assert registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping")) is True

    record = _shown(client)
    assert record.get("draining") != "yes"
    assert record["members"][OLD] == {"heartbeat_at": NOW, "draining": "yes"}


@pytest.mark.feature("hive-roll-member-drain")
def test_a_draining_member_whose_beats_lapsed_keeps_its_drain_on_its_next_beat(client):
    registry.join(client, HIVE, OLD, NOW - registry.LIVE_MS - 5_000)
    registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping"))

    registry.join(client, HIVE, OLD, NOW + registry.LIVE_MS + 2)

    assert _shown(client)["members"][OLD] == {"heartbeat_at": NOW + registry.LIVE_MS + 2, "draining": "yes"}


@pytest.mark.feature("hive-roll-member-drain")
def test_a_draining_replacement_takes_no_agents_so_the_hive_drains(client):
    registry.join(client, HIVE, OLD, NOW - 5_000)
    registry.join(client, HIVE, NEW, NOW - 1_000)
    registry.leave(client, HIVE, NEW, registry.Drain(NEW, "the hive is stopping"))

    assert registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping")) is False
    assert _shown(client)["draining"] == "yes"


@pytest.mark.feature("hive-roll-member-drain")
def test_a_member_keeps_its_drain_across_beats_and_clears_it_only_when_it_starts_again(client):
    registry.join(client, HIVE, OLD, NOW - 5_000)
    registry.join(client, HIVE, NEW, NOW - 1_000)
    registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping"))

    registry.join(client, HIVE, OLD, NOW)
    assert _shown(client)["members"][OLD] == {"heartbeat_at": NOW, "draining": "yes"}

    registry.join(client, HIVE, OLD, NOW + 1, started=True)
    assert _shown(client)["members"][OLD] == {"heartbeat_at": NOW + 1, "draining": "no"}


@pytest.mark.feature("hive-roll-member-drain")
def test_a_join_or_drain_check_of_an_unregistered_hive_writes_nothing(client):
    registry.join(client, "unregistered", OLD, NOW)

    assert registry.show(client, "unregistered") is None
    assert registry.member_draining(client, "unregistered", OLD) is False
    assert registry.member_draining(client, HIVE, "never-joined") is False


def _agent(name: str, namespace: str, task: str = "t1") -> AgentRecord:
    target = {"process_namespace": namespace} if namespace else {}
    return AgentRecord(name, "eng", task, seat=f"{name}-seat", hive=HIVE, state="working", runtime_target=target)


@pytest.mark.feature("hive-roll-member-drain")
def test_the_old_pod_drain_orders_only_its_own_agents_to_hand_off(client, monkeypatch):
    registry.join(client, HIVE, OLD, NOW - 5_000)
    registry.join(client, HIVE, NEW, NOW - 1_000)
    agents = [_agent("old@1", "boot/old"), _agent("new@1", "boot/new"), _agent("launching@1", "")]
    store = cast(
        RedisStore,
        SimpleNamespace(
            redis=client,
            slugs=lambda: ["fixture"],
            agents=lambda slug: agents,
            handoff_envelope=lambda slug, task: None,
        ),
    )
    ordered: list[list[str]] = []
    monkeypatch.setattr(remove, "_order", lambda inbox, sent, remaining, text, hive: ordered.append(_names(remaining)))
    monkeypatch.setattr(remove, "_recover", lambda store, hive, wait, remaining, host: f"recovered {_names(remaining)}")
    host = remove.Host(wall=lambda: NOW / 1000, clock=lambda: 0.0, sleep=lambda s: None, namespace=lambda: "boot/old")

    assert remove.settle(store, HIVE, 0, host, OLD) == (
        f"recovered ['old@1']; only member {OLD} drained, the hive stays open for its other members"
    )
    assert ordered == [["old@1"]]
    assert _shown(client).get("draining") != "yes"


@pytest.mark.feature("hive-roll-member-drain")
def test_a_drain_with_no_member_named_still_waits_on_every_agent_of_the_hive(client, monkeypatch):
    agents = [_agent("old@1", "boot/old"), _agent("new@1", "boot/new")]
    store = cast(
        RedisStore,
        SimpleNamespace(
            redis=client,
            slugs=lambda: ["fixture"],
            agents=lambda slug: agents,
            handoff_envelope=lambda slug, task: None,
        ),
    )
    monkeypatch.setattr(remove, "_order", lambda inbox, sent, remaining, text, hive: None)
    monkeypatch.setattr(remove, "_recover", lambda store, hive, wait, remaining, host: f"recovered {_names(remaining)}")
    host = remove.Host(wall=lambda: NOW / 1000, clock=lambda: 0.0, sleep=lambda s: None, namespace=lambda: "boot/old")

    assert remove.settle(store, HIVE, 0, host) == "recovered ['old@1', 'new@1']"
    record = _shown(client)
    assert record["draining"] == "yes"
    assert "members" not in record


@pytest.mark.feature("hive-roll-member-drain")
def test_a_member_drain_whose_pod_namespace_is_unreadable_drains_the_whole_hive(client, monkeypatch):
    registry.join(client, HIVE, OLD, NOW - 5_000)
    registry.join(client, HIVE, NEW, NOW - 1_000)
    agents = [_agent("old@1", "boot/old"), _agent("new@1", "boot/new")]
    store = cast(
        RedisStore,
        SimpleNamespace(
            redis=client,
            slugs=lambda: ["fixture"],
            agents=lambda slug: agents,
            handoff_envelope=lambda slug, task: None,
        ),
    )
    monkeypatch.setattr(remove, "_order", lambda inbox, sent, remaining, text, hive: None)
    monkeypatch.setattr(remove, "_recover", lambda store, hive, wait, remaining, host: f"recovered {_names(remaining)}")
    host = remove.Host(wall=lambda: NOW / 1000, clock=lambda: 0.0, sleep=lambda s: None, namespace=lambda: "")

    assert remove.settle(store, HIVE, 0, host, OLD) == "recovered ['old@1', 'new@1']"
    record = _shown(client)
    assert record["draining"] == "yes"
    assert record["members"][OLD]["draining"] == "no"


@pytest.mark.feature("hive-roll-member-drain")
def test_a_sole_member_drain_waits_on_every_agent_of_the_hive(client, monkeypatch):
    registry.join(client, HIVE, OLD, NOW - 5_000)
    agents = [_agent("old@1", "boot/old"), _agent("new@1", "boot/new")]
    store = cast(
        RedisStore,
        SimpleNamespace(
            redis=client,
            slugs=lambda: ["fixture"],
            agents=lambda slug: agents,
            handoff_envelope=lambda slug, task: None,
        ),
    )
    monkeypatch.setattr(remove, "_order", lambda inbox, sent, remaining, text, hive: None)
    monkeypatch.setattr(remove, "_recover", lambda store, hive, wait, remaining, host: f"recovered {_names(remaining)}")
    host = remove.Host(wall=lambda: NOW / 1000, clock=lambda: 0.0, sleep=lambda s: None, namespace=lambda: "boot/old")

    assert remove.settle(store, HIVE, 0, host, OLD) == "recovered ['old@1', 'new@1']"
    record = _shown(client)
    assert record["draining"] == "yes"
    assert record["members"][OLD]["draining"] == "yes"
    [entry] = record["drains"]
    assert (entry["owner"], entry["reason"], entry["deadline_at"]) == (OLD, "the hive is stopping", NOW)


@pytest.mark.feature("hive-roll-member-drain")
def test_a_member_leaving_a_hive_that_never_recorded_members_drains_the_whole_hive(client):
    assert registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping")) is False

    record = _shown(client)
    assert record["draining"] == "yes"
    assert record["members"] == {OLD: {"heartbeat_at": NOW, "draining": "yes"}}


def _names(remaining: list[tuple[str, AgentRecord]]) -> list[str]:
    return [agent.name for _, agent in remaining]


@pytest.mark.feature("hive-roll-member-drain")
def test_the_pod_prestop_drain_names_this_pod_as_the_leaving_member(monkeypatch):
    from scripts.hive import cli

    seen: list[tuple[str, int, str]] = []
    monkeypatch.setattr(cli, "redis_client", lambda: "redis")
    monkeypatch.setattr("scripts.swarm.store.RedisStore", lambda redis: redis)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: HIVE)
    monkeypatch.setattr(registry, "member_name", lambda: OLD)
    monkeypatch.setattr(remove, "settle", lambda store, hive, wait, host, member: seen.append((hive, wait, member)))

    cli._drain(42)

    assert seen == [(HIVE, 42, OLD)]


@pytest.mark.feature("hive-roll-member-drain")
def test_the_member_name_is_this_host_name(monkeypatch):
    monkeypatch.setattr(registry.socket, "gethostname", lambda: "agentihooks-worker-hive-7c9f")

    assert registry.member_name() == "agentihooks-worker-hive-7c9f"


@pytest.mark.feature("hive-roll-member-drain")
def test_a_draining_member_polls_no_lane_spoke_beats_presence_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    polled: list[tuple] = []
    waits: list[float] = []
    asked: list[tuple] = []
    beaten: list[tuple] = []
    state = iter([True, False])
    monkeypatch.setattr(lanes, "run_once", lambda *args: polled.append(args) or ["started"])
    monkeypatch.setattr(lanes, "draining", lambda store, hive: asked.append((store, hive)) or next(state))
    monkeypatch.setattr(lanes, "beat", lambda *args: beaten.append(args))
    monkeypatch.setattr(lanes, "stop_once", lambda *args: [])

    lanes.run("store", "bus", "runtime", "hive", ("eng",), waits.append, passes=range(2))

    assert len(polled) == 1
    assert beaten == [("store", "bus", "hive")]
    assert waits == [lanes.IDLE_S]
    assert asked == [("store", "hive"), ("store", "hive")]


@pytest.mark.feature("hive-roll-member-drain")
def test_the_lane_drain_gate_reads_this_pods_member_and_a_store_without_redis_is_never_draining(client, monkeypatch):
    registry.join(client, HIVE, OLD, NOW - 5_000)
    registry.join(client, HIVE, NEW, NOW - 1_000)
    registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping"))
    store = SimpleNamespace(redis=client)

    monkeypatch.setattr(registry, "member_name", lambda: OLD)
    assert lanes.draining(store, HIVE) is True
    monkeypatch.setattr(registry, "member_name", lambda: NEW)
    assert lanes.draining(store, HIVE) is False
    monkeypatch.setattr(registry, "member_name", lambda: OLD)
    assert lanes.draining("store", HIVE) is False


@pytest.mark.feature("hive-roll-member-drain")
def test_the_daemon_first_beat_clears_its_drain_and_a_draining_member_polls_no_launch_order(client, monkeypatch):
    from scripts.hive import daemon

    report = {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    monkeypatch.setattr(daemon, "observe", lambda **kwargs: report)
    monkeypatch.setattr(daemon.time, "time", lambda: NOW / 1000)
    monkeypatch.setattr(daemon, "JOINED", set())
    monkeypatch.setattr(registry, "member_name", lambda: OLD)
    polled: list[str] = []
    monkeypatch.setattr(daemon.dispatch, "poll", lambda consumer: polled.append(consumer.hive))
    registry.join(client, HIVE, NEW, NOW)
    registry.join(client, HIVE, OLD, NOW)
    registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping"))

    daemon.beat(client, HIVE)

    assert polled == [HIVE]
    assert _shown(client)["members"][OLD] == {"heartbeat_at": NOW, "draining": "no"}
    assert daemon.JOINED == {HIVE}

    registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping"))
    daemon.beat(client, HIVE)

    assert polled == [HIVE]
    assert _shown(client)["members"][OLD] == {"heartbeat_at": NOW, "draining": "yes"}


@pytest.mark.feature("hive-roll-member-drain")
def test_a_fenced_member_that_is_not_draining_still_polls_no_launch_order(client, monkeypatch):
    from scripts.hive import daemon

    report = {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    monkeypatch.setattr(daemon, "observe", lambda **kwargs: report)
    monkeypatch.setattr(daemon.time, "time", lambda: NOW / 1000)
    monkeypatch.setattr(daemon, "fence", lambda redis, hive, runtime: True)
    monkeypatch.setattr(registry, "member_name", lambda: NEW)
    polled: list[str] = []
    monkeypatch.setattr(daemon.dispatch, "poll", lambda consumer: polled.append(consumer.hive))

    daemon.beat(client, HIVE)

    assert polled == []
    assert _shown(client)["members"][NEW]["draining"] == "no"


@pytest.mark.feature("hive-roll-member-drain")
def test_hive_list_names_each_member_and_whether_it_takes_agents(client, monkeypatch, capsys):
    from scripts.hive import cli

    monkeypatch.setattr(cli, "redis_client", lambda: client)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    registry.update(client, HIVE, ["ui=no"])
    registry.join(client, HIVE, OLD, NOW - 5_000)
    registry.join(client, HIVE, NEW, NOW - 1_000)
    registry.leave(client, HIVE, OLD, registry.Drain(OLD, "the hive is stopping"))

    assert cli.main(["list"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[1:3] == [
        f"  member {NEW}\ttaking agents\theartbeat 1000 ms ago",
        f"  member {OLD}\tdraining\theartbeat 5000 ms ago",
    ]


@pytest.mark.feature("hive-roll-member-drain")
def test_admission_counts_the_agents_of_both_members_against_one_hive_cap(setup, monkeypatch):  # noqa: F811
    from scripts.hive import launch

    hive = setup.hive["id"]
    agents = [
        AgentRecord(f"eng@{n}", "eng", f"t{n}", hive=hive, state="working", runtime_target={"process_namespace": ns})
        for n, ns in enumerate(("boot/old", "boot/old", "boot/new"))
    ]
    monkeypatch.setattr(setup.store, "agents", lambda slug: agents)

    listed = next(found for found in launch.loaded_hives(setup.store) if found["id"] == hive)

    assert (listed["agents"], listed["room"]) == (3, 0)
    assert listed["refusal"] == "max agents: 3 of 3 agents on the hive, fits 0 more"
