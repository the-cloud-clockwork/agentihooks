from dataclasses import replace

import pytest


@pytest.fixture
def store(monkeypatch):
    from fakeredis import FakeRedis

    from scripts.hive import cli, registry
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "repo", 8, 0))
    monkeypatch.setattr(cli, "redis_client", lambda: store.redis)
    monkeypatch.setattr(
        registry,
        "hives",
        lambda client: [
            {"id": hive, "heartbeat_at": 1000, "excluded": [], "ui": False, "max_agents": 1}
            for hive in ("worker", "old")
        ],
    )
    monkeypatch.setattr(registry, "live", lambda *args: True)
    return store


def running(store, seat, hive):
    from scripts.swarm.store import AgentRecord

    agent = AgentRecord(store.next_name("sw", "eng"), "eng", "task", runtime_target={"host_id": hive})
    agent = store.start_execution("sw", replace(agent, seat=f"{seat}@sw"))
    store.put_agent("sw", agent)
    return replace(agent, hive=hive)


def checks(**misses):
    from scripts.hive import join_report

    return {**dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True), **misses}


def rows(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("  ")]


def test_hive_list_shows_each_agent_grade_and_every_miss(store, capsys):
    from scripts.hive import cli, join_report

    agent = running(store, "eng-1", "worker")
    join_report.start(store.redis, "sw", agent, "session", checks(brain=False, github=False), {}, 1000)
    cli._list()
    assert rows(capsys) == [f"  {agent.name}\tfailed\tmisses=github (required), brain (optional)\theartbeat=1000"]


def test_hive_list_leaves_out_ended_reports(store, capsys):
    from scripts.hive import cli, join_report

    kept, gone = running(store, "eng-1", "worker"), running(store, "eng-2", "worker")
    for agent in (kept, gone):
        join_report.start(store.redis, "sw", agent, "session", checks(), {}, 1000)
    join_report.end(store.redis, "worker", gone.execution_id, "session", "agent gone from the swarm", "ended", 2000)
    cli._list()
    assert rows(capsys) == [f"  {kept.name}\tcomplete\tmisses=none\theartbeat=1000"]


def test_hive_list_agents_equal_the_swarm_agents_of_every_hive(store, capsys):
    from scripts.hive import cli, join_report

    kept = running(store, "eng-1", "worker")
    finished = running(store, "eng-2", "worker")
    rolled = running(store, "eng-3", "old")
    for agent in (kept, finished, rolled):
        join_report.start(store.redis, "sw", agent, "session", checks(pane=False), {}, 1000)
    record = next(agent for agent in store.agents("sw") if agent.name == finished.name)
    store.put_agent("sw", replace(record, state="finished"))
    store.drop_agent("sw", rolled.name)
    cli._list()
    listed = [row.split("\t")[0].strip() for row in rows(capsys)]
    swarm = [agent.name for agent in store.agents("sw") if agent.state not in ("finished", "ended")]
    assert listed == swarm == [kept.name]
    assert finished.name in [agent.name for agent in store.agents("sw")]


def test_hive_list_leaves_out_a_report_from_an_earlier_generation(store, capsys):
    from scripts.hive import cli, join_report

    agent = running(store, "eng-1", "worker")
    join_report.start(store.redis, "sw", replace(agent, generation=agent.generation - 1), "old", checks(), {}, 900)
    cli._list()
    assert rows(capsys) == []
