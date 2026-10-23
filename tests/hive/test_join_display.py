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


def local(store, seat, **fields):
    from scripts.swarm.store import AgentRecord

    agent = AgentRecord(store.next_name("sw", "eng"), "eng", seat, seat=f"{seat}@sw", **fields)
    agent = store.start_execution("sw", agent)
    store.put_agent("sw", agent)
    return agent


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


class Runtime:
    def live_names(self):
        return set()


def test_hive_list_agents_equal_the_swarm_agents_on_every_other_hive(store, capsys, monkeypatch):
    from scripts.hive import cli, join_report
    from scripts.swarm import commands, status

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")

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
    swarm = [agent.name for agent in status.live_agents(store.agents("sw"), Runtime()) if agent.state != "finished"]
    assert listed == swarm == [kept.name]
    assert finished.name in [agent.name for agent in store.agents("sw")]


def test_hive_list_leaves_out_a_report_from_an_earlier_generation(store, capsys):
    from scripts.hive import cli, join_report

    agent = running(store, "eng-1", "worker")
    join_report.start(store.redis, "sw", replace(agent, generation=agent.generation - 1), "old", checks(), {}, 900)
    cli._list()
    assert rows(capsys) == []


class Herdr:
    def __init__(self, names):
        self.names = names

    def live_names(self):
        return self.names


def listed_by_hive(out):
    hive, found = "", {}
    for line in out.splitlines():
        if line.startswith("  "):
            found.setdefault(hive, []).append(line.split("\t")[0].strip())
        else:
            hive = line.split("\t")[0]
    return found


def test_hive_list_shows_the_local_agents_swarm_status_places_on_this_hive(store, capsys, monkeypatch):
    from scripts.hive import cli
    from scripts.swarm import commands, runtime, status

    monkeypatch.setattr(commands, "hive_id", lambda: "worker")
    claude = [local(store, f"eng-{n}") for n in range(2)]
    codex = local(store, "eng-2", hive="worker", runtime_target={"pid": 1})
    local(store, "eng-3")
    live = Herdr({agent.name for agent in (*claude, codex)})
    monkeypatch.setattr(runtime, "HerdrRuntime", lambda: live)
    cli._list()
    listed = listed_by_hive(capsys.readouterr().out)
    placed = {a["name"]: a["hive"] for a in status.status_report(store, "sw", {"tasks": []}, runtime=live)["agents"]}
    names = sorted(agent.name for agent in (*claude, codex))
    assert placed == dict.fromkeys(names, "worker")
    assert listed == {"worker": names}


def test_hive_list_lists_each_live_local_agent_once_and_only_on_its_own_hive(store, capsys, monkeypatch):
    from scripts.hive import cli, join_report
    from scripts.swarm import commands, runtime, status

    monkeypatch.setattr(commands, "hive_id", lambda: "worker")
    plain = local(store, "eng-0")
    finished = local(store, "eng-1", state="finished")
    remote = local(store, "eng-2", runtime_target={"host_id": "old"})
    pod = local(store, "eng-3", runtime_backend="kubernetes")
    reported = running(store, "eng-9", "worker")
    join_report.start(store.redis, "sw", reported, "session", checks(), {}, 1000)
    live = Herdr({agent.name for agent in (plain, finished, remote, pod, reported)})
    monkeypatch.setattr(runtime, "HerdrRuntime", lambda: live)
    cli._list()
    assert listed_by_hive(capsys.readouterr().out) == {"worker": [reported.name, plain.name]}
    assert [status.agent_hive(agent) for agent in (plain, remote, pod)] == ["worker", "old", ""]
