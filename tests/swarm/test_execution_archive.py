import json
from dataclasses import asdict, replace
from unittest.mock import MagicMock

import pytest

from scripts.events import state
from scripts.swarm import fencing
from scripts.swarm.execution import ExecutionRegistry
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    return store


def started(store, seat, previous=""):
    return store.start_execution(
        "fixture", AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat=seat), previous
    )


def history(store):
    first = started(store, "eng-1@fixture")
    store.drop_agent("fixture", first.name)
    second = started(store, "eng-1@fixture", first.execution_id)
    store.drop_agent("fixture", second.name)
    third = started(store, "eng-1@fixture", second.execution_id)
    other = started(store, "eng-2@fixture")
    store.drop_agent("fixture", other.name)
    return first, second, third, other


def section(store, name):
    return fencing.read(store, "fixture")[name]


def test_archive_moves_only_rows_no_live_reader_returns(store):
    first, second, third, other = history(store)
    registry = store.execution_registry
    agents, occupants = registry.agents("fixture"), registry.occupants("fixture")
    records = sorted(registry.records("fixture"), key=lambda a: a.execution_id)

    assert registry.archive("fixture") == 2

    assert set(section(store, "executions")) == {third.execution_id, other.execution_id}
    assert set(section(store, "archive")) == {first.execution_id, second.execution_id}
    assert registry.agents("fixture") == agents
    assert registry.occupants("fixture") == occupants
    assert sorted(registry.records("fixture"), key=lambda a: a.execution_id) == records
    assert store.execution("fixture", first.execution_id).name == first.name
    assert registry.managed("fixture", second.name)
    assert not registry.managed("fixture", "nobody@fixture")
    assert registry.archive("fixture") == 0


def test_managed_reads_the_archive_inside_redis(store, monkeypatch):
    first, second, third, other = history(store)
    registry = store.execution_registry
    registry.archive("fixture")
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    archive = store.key("fixture", ARCHIVE)
    hvals = store.redis.hvals

    def refusing(key):
        assert key != archive, "the archive was read into the client"
        return hvals(key)

    monkeypatch.setattr(store.redis, "hvals", refusing)
    assert registry.managed("fixture", first.name) is True
    assert registry.managed("fixture", third.name) is True
    assert registry.managed("fixture", first.name[:-1]) is False
    assert registry.managed("fixture", "nobody@fixture") is False


def test_a_hive_member_reads_the_archive_it_may_only_read(store, monkeypatch):
    first, second, third, other = history(store)
    registry = store.execution_registry
    registry.archive("fixture")
    for variable in ("AGENTIHOOKS_HIVE_REDIS_URL", "AGENTIHOOKS_SWARM_REDIS_URL"):
        monkeypatch.setenv(variable, "redis://hive.example:6380/0")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "m1")
    archive = store.key("fixture", ARCHIVE)
    script = store.redis.eval

    def refusing(source, keys, *args):
        assert args[0] != archive, "the archive key was declared to a script"
        return script(source, keys, *args)

    monkeypatch.setattr(store.redis, "eval", refusing)
    assert registry.managed("fixture", first.name) is True
    assert registry.managed("fixture", third.name) is True
    assert registry.managed("fixture", "nobody@fixture") is False


def timed_steps(capsys, write):
    from scripts.swarm import timing

    capsys.readouterr()
    with timing.tick("fixture"):
        write()
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    return {row["step"] for row in rows if row.get("event") == "swarm_tick_step" and row["phase"] == "finished"}


def test_a_legacy_agent_write_times_its_managed_check_and_commit(store, capsys):
    from scripts.swarm import timing

    legacy = AgentRecord("engineer@fixture-0009", "eng", "task", seat="eng-9@fixture")
    steps = timed_steps(capsys, lambda: store.put_agent("fixture", legacy))
    with store.redis.pipeline() as pipe:
        execute = timing.named(pipe.execute)
    parts = [timing.named(ExecutionRegistry._put), execute]
    assert [part for part in parts if part not in steps] == []


def test_an_identified_agent_write_times_its_update_reads_and_commit(store, capsys):
    from scripts.swarm import timing

    live = started(store, "eng-1@fixture")
    steps = timed_steps(capsys, lambda: store.put_agent("fixture", replace(live, pane_id="pane-7")))
    with store.redis.pipeline() as pipe:
        execute = timing.named(pipe.execute)
    parts = [
        timing.named(ExecutionRegistry._put),
        timing.named(ExecutionRegistry._update),
        execute,
    ]
    assert [part for part in parts if part not in steps] == []
    assert next(a for a in store.agents("fixture") if a.name == live.name).pane_id == "pane-7"


def test_set_pane_still_finds_an_archived_attempt(store):
    first, *_ = history(store)
    store.execution_registry.archive("fixture")

    store.execution_registry.set_pane("fixture", first.execution_id, "pane-9")

    assert store.execution("fixture", first.execution_id).pane_id == "pane-9"
    assert first.execution_id not in section(store, "archive")


@pytest.mark.parametrize("archived", [False, True])
def test_set_pane_on_a_replaced_run_keeps_its_dropped_agent_out_of_the_live_list(store, archived):
    first, second, third, other = history(store)
    if archived:
        store.execution_registry.archive("fixture")
    live = store.redis.hgetall(store.key("fixture", "agents"))

    store.execution_registry.set_pane("fixture", first.execution_id, "pane-9")

    assert store.execution("fixture", first.execution_id).pane_id == "pane-9"
    assert store.redis.hgetall(store.key("fixture", "agents")) == live
    assert [agent.name for agent in store.agents("fixture")] == [third.name]


def test_set_pane_never_shows_an_agent_dropped_during_the_write(store, monkeypatch):
    current = started(store, "eng-1@fixture")
    project = store.project_agent

    def dropped_first(slug, *args, **kwargs):
        store.redis.hdel(store.key(slug, "agents"), current.name)
        return project(slug, *args, **kwargs)

    monkeypatch.setattr(store, "project_agent", dropped_first)

    store.execution_registry.set_pane("fixture", current.execution_id, "pane-9")

    assert store.execution("fixture", current.execution_id).pane_id == "pane-9"
    assert not store.redis.hexists(store.key("fixture", "agents"), current.name)


def test_set_pane_retries_when_its_run_record_changes_during_the_write(store, monkeypatch):
    current = started(store, "eng-1@fixture")
    states = state.for_store(store, fencing.BUCKET)
    raced = []

    class Racing:
        def get(self, key):
            return states.get(key)

        def create(self, key, value):
            return states.create(key, value)

        def update(self, key, value, revision):
            if not raced:
                raced.append(True)
                entry = states.get(key)
                doc = json.loads(entry.value)
                row = doc["executions"][current.execution_id]
                doc["executions"][current.execution_id] = {**row, "pane_id": "pane-race"}
                states.update(key, json.dumps(doc).encode(), entry.revision)
            return states.update(key, value, revision)

    monkeypatch.setattr(fencing.state, "for_store", lambda *args, **kwargs: Racing())

    store.execution_registry.set_pane("fixture", current.execution_id, "pane-9")

    assert raced == [True]
    assert store.execution("fixture", current.execution_id).pane_id == "pane-9"


def test_set_pane_on_an_earlier_run_of_a_live_name_keeps_the_latest_projection(store):
    agent = AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat="eng-3@fixture")
    one = store.start_execution("fixture", agent)
    store.start_execution("fixture", replace(agent, state="working"), one.execution_id)
    live = store.redis.hgetall(store.key("fixture", "agents"))

    store.execution_registry.set_pane("fixture", one.execution_id, "pane-9")

    assert store.execution("fixture", one.execution_id).pane_id == "pane-9"
    assert store.redis.hgetall(store.key("fixture", "agents")) == live


def test_archive_keeps_every_row_of_a_live_name_and_seat_head_ties(store):
    agent = AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat="eng-3@fixture")
    one = store.start_execution("fixture", agent)
    two = store.start_execution("fixture", agent, one.execution_id)
    three = store.start_execution("fixture", agent, two.execution_id)
    tie = {**section(store, "executions")[three.execution_id], "name": "gone@fixture", "execution_id": "exe-tie"}
    fencing.write(store, "fixture", lambda doc: doc["executions"].update({"exe-tie": tie}))
    registry = store.execution_registry
    agents, occupants = registry.agents("fixture"), registry.occupants("fixture")
    assert [a.name for a in agents] == [agent.name]

    assert registry.archive("fixture") == 0

    assert set(section(store, "executions")) == {one.execution_id, two.execution_id, three.execution_id, "exe-tie"}
    assert registry.agents("fixture") == agents
    assert registry.occupants("fixture") == occupants


def test_archive_keeps_rows_without_a_seat_or_generation(store):
    history(store)
    rows = {"legacy": {"name": "old@fixture"}, "nameless": {"seat": "eng-1@fixture", "generation": 0}}
    fencing.write(store, "fixture", lambda doc: doc["executions"].update(rows))

    store.execution_registry.archive("fixture")

    assert {"legacy", "nameless"} <= set(section(store, "executions"))


def test_reconcile_archives_dead_rows(store):
    first, second, *_ = history(store)
    runtime = MagicMock()
    runtime.live_names.return_value = set()

    ExecutionRegistry(store).reconcile("fixture", runtime, 0)

    assert set(section(store, "archive")) == {first.execution_id, second.execution_id}


def test_a_first_read_imports_the_legacy_execution_hashes(store):
    agent = AgentRecord("old@fixture", "eng", "task", seat="eng-1@fixture", execution_id="exe-old", generation=1)
    store.redis.hset(store.key("other", "executions"), "exe-old", json.dumps(asdict(agent)))
    store.redis.hset(store.key("other", "executions-archive"), "exe-older", json.dumps(asdict(agent)))
    doc = fencing.read(store, "other")
    assert set(doc["executions"]) == {"exe-old"}
    assert set(doc["archive"]) == {"exe-older"}
