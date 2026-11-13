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
    agents = registry.agents("fixture")
    records = sorted(registry.records("fixture"), key=lambda a: a.execution_id)

    assert registry.archive("fixture") == 3

    assert set(section(store, "executions")) == {third.execution_id}
    assert set(section(store, "archive")) == {first.execution_id, second.execution_id, other.execution_id}
    assert registry.agents("fixture") == agents
    assert registry.occupants("fixture") == {"eng-1@fixture": store.execution("fixture", third.execution_id)}
    assert sorted(registry.records("fixture"), key=lambda a: a.execution_id) == records
    assert store.execution("fixture", first.execution_id).name == first.name
    assert registry.managed("fixture", second.name)
    assert not registry.managed("fixture", "nobody@fixture")
    assert registry.archive("fixture") == 0


@pytest.mark.feature("swarm-archive-terminal-head")
def test_archive_moves_a_terminal_head_with_no_newer_generation(store):
    agent = started(store, "eng-9@fixture")
    store.drop_agent("fixture", agent.name)
    registry = store.execution_registry

    assert registry.archive("fixture") == 1

    assert agent.execution_id not in section(store, "executions")
    assert agent.execution_id in section(store, "archive")
    assert store.execution("fixture", agent.execution_id).state == "ended"
    assert registry.occupants("fixture") == {}


@pytest.mark.feature("swarm-archive-stale-generations-of-a-listed-name")
def test_archive_moves_older_generations_of_a_name_that_stays_listed(store):
    name = store.next_name("fixture", "eng")
    agent = AgentRecord(name, "eng", "task", seat="eng-1@fixture")
    one = store.start_execution("fixture", agent)
    store.drop_agent("fixture", name)
    two = store.start_execution("fixture", agent, one.execution_id)
    store.drop_agent("fixture", name)
    three = store.start_execution("fixture", agent, two.execution_id)
    registry = store.execution_registry

    assert registry.archive("fixture") == 2

    assert set(section(store, "executions")) == {three.execution_id}
    assert set(section(store, "archive")) == {one.execution_id, two.execution_id}
    assert registry.occupants("fixture") == {"eng-1@fixture": store.execution("fixture", three.execution_id)}


@pytest.mark.feature("swarm-archive-keeps-listed-head")
def test_archive_keeps_a_listed_head_that_is_ended(store):
    name = store.next_name("fixture", "eng")
    agent = AgentRecord(name, "eng", "task", seat="eng-9@fixture")
    older = store.start_execution("fixture", agent)
    store.drop_agent("fixture", name)
    head = store.start_execution("fixture", agent, older.execution_id)
    store.put_agent("fixture", replace(head, state="ended"))
    registry = store.execution_registry

    assert registry.archive("fixture") == 1

    assert set(section(store, "executions")) == {head.execution_id}
    assert set(section(store, "archive")) == {older.execution_id}
    assert registry.occupants("fixture") == {"eng-9@fixture": store.execution("fixture", head.execution_id)}


@pytest.mark.feature("swarm-archive-prunes-intents")
def test_archive_prunes_intents_for_the_rows_it_archives(store):
    from scripts.swarm import lease

    held = lease.acquire(store, "fixture", "home")
    assert held is not None
    with lease.fencing(held.epoch):
        first, second, third, other = history(store)
        assert first.execution_id in section(store, "intents")
        assert second.execution_id in section(store, "intents")

    assert store.execution_registry.archive("fixture") == 3

    intents = section(store, "intents")
    assert first.execution_id not in intents
    assert second.execution_id not in intents
    assert third.execution_id in intents
    assert other.execution_id not in intents


@pytest.mark.parametrize("external", [False, True])
def test_fenced_archive_and_rows_written_in_the_old_layout_reach_old_readers(store: RedisStore, external: bool) -> None:
    from scripts.swarm import lease

    first, second, third, other = history(store)
    if external:
        fencing.migrate_archive(store, "fixture", writers_upgraded=True)
    held = lease.acquire(store, "fixture", "home")
    assert held is not None
    with lease.fencing(held.epoch):
        assert store.execution_registry.archive("fixture") == 3
        store.put_agent("fixture", replace(third, state="working"))
    port = fencing.records(store, "fixture")
    assert port.layout("fixture") == fencing.DOCUMENT
    doc = fencing.read(store, "fixture")
    executions = fencing.read_executions(store, "fixture")
    assert set(doc["archive"]) == {first.execution_id, second.execution_id, other.execution_id}
    assert executions == doc["executions"]
    assert executions[third.execution_id]["state"] == "working"
    for execution_id, row in doc["archive"].items():
        found = port.record("fixture", "archive", execution_id)
        assert found is not None
        assert found.value == row
    for execution_id, row in executions.items():
        found = port.record("fixture", "executions", execution_id)
        assert found is not None
        assert found.value == row


def test_managed_reads_the_archive_inside_redis(store, monkeypatch):
    first, second, third, other = history(store)
    registry = store.execution_registry
    registry.archive("fixture")
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    archive = store.key("fixture", "executions-archive")
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
    archive = store.key("fixture", "executions-archive")
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


DECISION = {"harness": "codex", "profile": "engineer", "account": "work", "model": "gpt-5", "effort": "high"}


@pytest.mark.feature("events-launch-decision-record")
def test_set_launch_writes_the_decision_onto_the_live_head_and_its_projection(store):
    current = started(store, "eng-1@fixture")

    assert store.execution_registry.set_launch("fixture", current.execution_id, DECISION) is True

    record = store.execution("fixture", current.execution_id)
    [agent] = store.agents("fixture")
    assert {field: getattr(record, field) for field in DECISION} == DECISION
    assert (record.name, record.seat, record.state) == (current.name, current.seat, current.state)
    assert {field: getattr(agent, field) for field in DECISION} == DECISION
    assert (agent.name, agent.seat, agent.execution_id) == (current.name, "eng-1@fixture", current.execution_id)


@pytest.mark.feature("events-launch-decision-record")
@pytest.mark.parametrize("which", ["replaced", "dropped", "unknown", "earlier", "unlisted"])
def test_set_launch_changes_nothing_for_a_run_that_is_not_a_listed_seat_head(store, which):
    first, _, _, other = history(store)
    agent = AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat="eng-3@fixture")
    earlier = store.start_execution("fixture", agent)
    store.start_execution("fixture", replace(agent, state="working"), earlier.execution_id)
    unlisted = started(store, "eng-4@fixture")
    store.redis.hdel(store.key("fixture", "agents"), unlisted.name)
    execution_id = {
        "replaced": first.execution_id,
        "dropped": other.execution_id,
        "unknown": "missing",
        "earlier": earlier.execution_id,
        "unlisted": unlisted.execution_id,
    }[which]
    executions = section(store, "executions")
    live = store.redis.hgetall(store.key("fixture", "agents"))

    assert store.execution_registry.set_launch("fixture", execution_id, DECISION) is False

    assert section(store, "executions") == executions
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


@pytest.mark.feature("swarm-archive-prunes-intents")
def test_reconcile_archives_dead_rows(store):
    from scripts.swarm import lease

    held = lease.acquire(store, "fixture", "home")
    assert held is not None
    with lease.fencing(held.epoch):
        first, second, third, other = history(store)
    runtime = MagicMock()
    runtime.live_names.return_value = set()

    ExecutionRegistry(store).reconcile("fixture", runtime, 0)

    assert set(section(store, "archive")) == {first.execution_id, second.execution_id, other.execution_id}
    assert first.execution_id not in section(store, "intents")
    assert second.execution_id not in section(store, "intents")


def test_a_first_read_imports_the_legacy_execution_hashes(store):
    agent = AgentRecord("old@fixture", "eng", "task", seat="eng-1@fixture", execution_id="exe-old", generation=1)
    store.redis.hset(store.key("other", "executions"), "exe-old", json.dumps(asdict(agent)))
    store.redis.hset(store.key("other", "executions-archive"), "exe-older", json.dumps(asdict(agent)))
    doc = fencing.read(store, "other")
    assert set(doc["executions"]) == {"exe-old"}
    assert set(doc["archive"]) == {"exe-older"}


VALIDATION = {"state": "validated", "pid": 7, "revisions": {"/repo": "abc"}, "source_blobs": [{"path": "CLAUDE.md"}]}


def validated(store, seat, previous=""):
    agent = AgentRecord(
        store.next_name("fixture", "eng"), "eng", "task", seat=seat, profile_decision={"validation": dict(VALIDATION)}
    )
    return store.start_execution("fixture", agent, previous)


def test_archive_drops_binding_source_blobs_and_keeps_the_rest_of_the_validation(store):
    first = validated(store, "eng-1@fixture")
    store.drop_agent("fixture", first.name)
    second = validated(store, "eng-1@fixture", first.execution_id)

    assert store.execution_registry.archive("fixture") == 1

    archived = section(store, "archive")[first.execution_id]["profile_decision"]["validation"]
    live = section(store, "executions")[second.execution_id]["profile_decision"]["validation"]
    assert archived == {key: value for key, value in VALIDATION.items() if key != "source_blobs"}
    assert live == VALIDATION
    assert store.execution("fixture", first.execution_id).profile_decision["validation"]["state"] == "validated"


def test_archive_compacts_rows_archived_before_it_dropped_source_blobs(store):
    first = validated(store, "eng-1@fixture")
    store.drop_agent("fixture", first.name)
    validated(store, "eng-1@fixture", first.execution_id)
    bare = {"name": "old@fixture", "seat": "eng-9@fixture", "generation": 1, "execution_id": "exe-bare"}

    def legacy(doc):
        doc["archive"][first.execution_id] = doc["executions"].pop(first.execution_id)
        doc["archive"]["exe-bare"] = dict(bare)

    fencing.write(store, "fixture", legacy)
    assert "source_blobs" in section(store, "archive")[first.execution_id]["profile_decision"]["validation"]

    assert store.execution_registry.archive("fixture") == 0
    assert "source_blobs" not in section(store, "archive")[first.execution_id]["profile_decision"]["validation"]
    assert section(store, "archive")["exe-bare"] == bare
    compacted = fencing._states(store).get("fixture").revision
    assert store.execution_registry.archive("fixture") == 0
    assert fencing._states(store).get("fixture").revision == compacted


def test_the_tick_reconcile_archives_without_source_blobs(store):
    first = validated(store, "eng-1@fixture")
    store.drop_agent("fixture", first.name)
    validated(store, "eng-1@fixture", first.execution_id)
    runtime = MagicMock()
    runtime.live_names.return_value = set()

    ExecutionRegistry(store).reconcile("fixture", runtime, 0)

    assert "source_blobs" not in section(store, "archive")[first.execution_id]["profile_decision"]["validation"]
