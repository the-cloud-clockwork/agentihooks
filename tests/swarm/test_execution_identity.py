import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scripts.swarm.execution import ExecutionRegistry
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    assert isinstance(store.execution_registry, ExecutionRegistry)
    return store


@pytest.fixture
def agent(store):
    return AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat="eng-1@fixture", started_at=123)


def target(uid=""):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/swarm_v2/execution-identity.json").read_text())
    return {**fixture["attempts"][0]["runtime_target"], "pod_uid": uid}


@pytest.mark.parametrize("repeat", range(2))
def test_three_attempts_preserve_logical_identity(store, agent, repeat):
    first = store.start_execution("fixture", replace(agent, runtime_backend="kubernetes", runtime_target=target()))
    assert first.execution_id
    assert first.generation == 1
    assert store.execution("fixture", first.execution_id) == first
    first = replace(first, runtime_target=target("pod-one"))
    store.put_agent("fixture", first)
    store.memory.learn(agent.seat, agent.name, "Preserve seat evidence because replacements lose process memory", 123)
    memory = store.memory.learned(agent.seat)
    second = store.start_execution(
        "fixture", replace(agent, runtime_backend="kubernetes", runtime_target=target("pod-two")), first.execution_id
    )
    third = store.start_execution("fixture", agent, second.execution_id)
    assert len({first.execution_id, second.execution_id, third.execution_id}) == 3
    assert [a.generation for a in store.executions("fixture", agent.seat)] == [1, 2, 3]
    assert [a.name for a in store.executions("fixture", agent.seat)] == [agent.name] * 3
    assert [a.task for a in store.executions("fixture", agent.seat)] == [agent.task] * 3
    assert store.agents("fixture") == [third]
    assert store.memory.learned(agent.seat) == memory
    assert store.execution_identity_conflicts_total("fixture") == 0


def protected(store):
    return {key: store.redis.dump(key) for key in store.redis.scan_iter() if not key.endswith("identity-conflicts")}


@pytest.mark.parametrize(
    "fault", ["uid", "label", "stale", "generation", "runtime_backend", "task", "seat", "empty", "name"]
)
def test_conflicts_refuse_without_changing_identity(store, agent, fault):
    first = store.start_execution(
        "fixture", replace(agent, runtime_backend="kubernetes", runtime_target=target("pod-one"))
    )
    second = store.start_execution(
        "fixture", replace(agent, runtime_backend="kubernetes", runtime_target=target("pod-two")), first.execution_id
    )
    before = protected(store)
    with pytest.raises(SwarmError) as error:
        if fault == "uid":
            store.start_execution(
                "fixture",
                replace(agent, runtime_backend="kubernetes", runtime_target=target("pod-one")),
                second.execution_id,
            )
        elif fault == "label":
            store.execution("fixture", "shared-label")
        elif fault == "stale":
            store.put_agent("fixture", first)
        elif fault == "empty":
            store.put_agent("fixture", agent)
        elif fault == "name":
            store.start_execution("fixture", replace(agent, name="shared-label"), second.execution_id)
        else:
            value = {"generation": 99, "runtime_backend": "local", "task": "other", "seat": "eng-2@fixture"}[fault]
            store.put_agent("fixture", replace(second, **{fault: value}))
    assert protected(store) == before
    assert store.execution_identity_conflicts_total("fixture") == 1
    assert store.agents("fixture") == [second]
    messages = {
        "uid": "Pod UID already belongs to another execution",
        "label": "unknown execution identity; display labels cannot identify attempts",
        "name": "execution requires a registered canonical agent name",
        "runtime_backend": "unsupported runtime target identity field",
    }
    assert str(error.value) == messages.get(fault, "stale or changed execution identity")


@pytest.mark.parametrize("repeat", range(2))
def test_restart_replay_snapshot_and_rollback(store, agent, repeat):
    first = store.start_execution("fixture", agent)
    second = store.start_execution("fixture", agent, first.execution_id)
    third = store.start_execution("fixture", agent, second.execution_id)
    restarted = RedisStore(store.redis)
    restarted.redis.delete(restarted.key("fixture", "agents"))
    assert restarted.execution_occupants("fixture") == {agent.seat: third}
    restarted.put_agent("fixture", third)
    restarted.put_agent("fixture", third)
    assert restarted.executions("fixture", agent.seat) == [first, second, third]
    state = restarted.export("fixture")
    import fakeredis

    restored = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    restored.restore("fixture", state)
    assert restored.execution_occupants("fixture") == {agent.seat: third}
    assert restored.executions("fixture", agent.seat) == [first, second, third]
    preceding_fields = set(asdict(agent)) - {"execution_id", "generation", "runtime_backend", "runtime_target"}
    assert {k: v for k, v in asdict(third).items() if k in preceding_fields} == {
        k: v for k, v in asdict(agent).items() if k in preceding_fields
    }
    restored.drop_agent("fixture", third.name)
    assert restored.executions("fixture", agent.seat) == [first, second, third]


def test_legacy_local_records_remain_readable(store, agent):
    row = asdict(agent)
    for key in ("execution_id", "generation", "runtime_backend", "runtime_target"):
        row.pop(key)
    store.redis.hset(store.key("fixture", "agents"), agent.name, json.dumps(row))
    assert store.agents("fixture") == [agent]
    store.put_agent("fixture", replace(agent, idle_ticks=2))
    assert store.agents("fixture")[0].idle_ticks == 2


def test_execution_metadata_schema(store, agent):
    from jsonschema import Draft202012Validator

    record = store.start_execution("fixture", agent)
    schema = json.loads((Path(__file__).parents[2] / "docs/swarm-v2/schemas/execution.json").read_text())
    Draft202012Validator(schema).validate(asdict(record))


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"runtime_backend": "unsupported"}, "unsupported runtime backend"),
        ({"seat": "eng-1@other"}, "execution seat does not belong to the swarm"),
        ({"seat": "label"}, "execution seat does not belong to the swarm"),
        ({"runtime_target": "label"}, "runtime target must be a structured identity"),
        ({"runtime_target": {"unexpected": "value"}}, "unsupported runtime target identity field"),
        ({"runtime_backend": "kubernetes", "runtime_target": {}}, "Kubernetes target requires namespace and Pod name"),
        (
            {"runtime_backend": "kubernetes", "runtime_target": {"pod_namespace": "", "pod_name": "pod"}},
            "Kubernetes target requires namespace and Pod name",
        ),
        (
            {"runtime_backend": "kubernetes", "runtime_target": {"pod_namespace": "workers", "pod_name": 1}},
            "Kubernetes target requires namespace and Pod name",
        ),
        ({"runtime_target": {"server_id": 1}}, "runtime target identity must be a string"),
        ({"runtime_target": {"pid": 0}}, "runtime PID must be a positive integer"),
        ({"runtime_target": {"pid": True}}, "runtime PID must be a positive integer"),
        ({"runtime_target": {"pid": 7, "pid_start": 0}}, "runtime PID start time must be a positive integer"),
        ({"runtime_target": {"pid": 7, "pid_start": "777"}}, "runtime PID start time must be a positive integer"),
        ({"runtime_target": {"pid": 7, "pid_start": True}}, "runtime PID start time must be a positive integer"),
        ({"execution_id": "exe-forged"}, "new execution identity and generation must be allocated by the store"),
        ({"generation": 9}, "new execution identity and generation must be allocated by the store"),
        ({"name": "engineer@ffffff-9999"}, "execution requires a registered canonical agent name"),
    ],
)
def test_invalid_admission_is_contained(store, agent, changes, message):
    before = protected(store)
    with pytest.raises(SwarmError) as error:
        store.start_execution("fixture", replace(agent, **changes))
    assert str(error.value) == message
    assert protected(store) == before
    assert store.execution_identity_conflicts_total("fixture") == 1


def test_a_local_target_names_its_process_by_namespace_pid_and_start_time(store, agent):
    target = {"process_namespace": "boot/pid:[1]", "pid": 7, "pid_start": 1}
    assert store.start_execution("fixture", replace(agent, runtime_target=target)).runtime_target == target


@pytest.mark.parametrize(
    "changes",
    [
        {"runtime_target": target("changed")},
        {"runtime_target": target()},
        {"runtime_target": {**target("pod-one"), "pod_namespace": "other"}},
        {"runtime_target": {**target("pod-one"), "pod_name": "other"}},
        {"execution_id": "exe-forged"},
        {"name": "other"},
        {"lane": "ci"},
        {"started_at": 999},
    ],
)
def test_bound_attempt_cannot_change_its_identity(store, agent, changes):
    current = store.start_execution(
        "fixture", replace(agent, runtime_backend="kubernetes", runtime_target=target("pod-one"))
    )
    before = protected(store)
    with pytest.raises(SwarmError) as error:
        store.put_agent("fixture", replace(current, **changes))
    assert str(error.value) == (
        "runtime target identity is immutable after binding"
        if "runtime_target" in changes
        else "stale or changed execution identity"
    )
    assert protected(store) == before
    assert store.execution("fixture", current.execution_id) == current


def test_new_attempt_requires_exact_predecessor(store, agent):
    current = store.start_execution("fixture", agent)
    for predecessor in ("", "label", "exe-forged"):
        before = protected(store)
        with pytest.raises(SwarmError) as error:
            store.start_execution("fixture", agent, predecessor)
        assert str(error.value) == "replacement does not match the current execution"
        assert protected(store) == before
    assert store.execution_occupants("fixture") == {agent.seat: current}


def test_alias_uses_the_naming_registry(store, agent):
    store.names.alias("old-alias", agent.name)
    current = store.start_execution("fixture", replace(agent, name="old-alias"))
    assert current.name == agent.name
    assert store.executions("fixture", agent.seat) == [current]


def test_replacement_name_keeps_seat_history_and_removes_old_projection(store, agent):
    first = store.start_execution("fixture", agent)
    next_name = store.next_name("fixture", "eng")
    second = store.start_execution("fixture", replace(agent, name=next_name), first.execution_id)
    assert store.agents("fixture") == [second]
    assert store.executions("fixture", agent.seat) == [first, second]
    assert store.execution_occupants("fixture") == {agent.seat: second}


def test_one_canonical_agent_cannot_occupy_two_execution_seats(store, agent):
    store.start_execution("fixture", agent)
    before = protected(store)
    with pytest.raises(SwarmError) as error:
        store.start_execution("fixture", replace(agent, seat="eng-2@fixture"))
    assert str(error.value) == "canonical agent already belongs to another execution seat"
    assert protected(store) == before


def test_binding_cannot_steal_another_attempts_uid(store, agent):
    first = store.start_execution(
        "fixture", replace(agent, runtime_backend="kubernetes", runtime_target=target("pod-one"))
    )
    next_name = store.next_name("fixture", "eng")
    second = store.start_execution(
        "fixture",
        replace(agent, name=next_name, seat="eng-2@fixture", runtime_backend="kubernetes", runtime_target=target()),
    )
    before = protected(store)
    with pytest.raises(SwarmError, match="Pod UID"):
        store.put_agent("fixture", replace(second, runtime_target=target("pod-one")))
    assert protected(store) == before
    store.put_agent("fixture", replace(second, runtime_target=target("pod-two")))
    assert store.execution("fixture", first.execution_id) == first


def test_runtime_metadata_requires_execution_admission(store, agent):
    before = protected(store)
    with pytest.raises(SwarmError):
        store.put_agent("fixture", replace(agent, runtime_backend="kubernetes", runtime_target=target()))
    assert protected(store) == before


def test_uuid_collision_preserves_all_attempts(store, agent, monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm import execution

    monkeypatch.setattr(execution, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    first = store.start_execution("fixture", agent)
    before = protected(store)
    with pytest.raises(SwarmError) as error:
        store.start_execution("fixture", agent, first.execution_id)
    assert str(error.value) == "execution identity already exists"
    assert protected(store) == before


def test_updates_preserve_status_timestamp_until_status_changes(store, agent, monkeypatch):
    from scripts.swarm import execution

    monkeypatch.setattr(execution.time, "time", lambda: 10)
    current = store.start_execution("fixture", agent)
    key = store.key("fixture", "state-since")
    assert store.redis.hget(key, agent.name) == "10000"
    monkeypatch.setattr(execution.time, "time", lambda: 20)
    store.put_agent("fixture", current)
    assert store.redis.hget(key, agent.name) == "10000"
    changed = replace(current, state="finished")
    store.put_agent("fixture", changed)
    assert store.redis.hget(key, agent.name) == "20000"
    assert store.execution("fixture", current.execution_id) == changed


@pytest.mark.parametrize("operation", ["start", "update", "legacy"])
@pytest.mark.parametrize("failures", [1, 5])
def test_transaction_contention_is_bounded_and_never_partially_commits(store, agent, monkeypatch, operation, failures):
    from redis.exceptions import WatchError

    current = store.start_execution("fixture", agent)
    legacy = replace(agent, name=store.next_name("fixture", "eng"))
    before = protected(store)
    original = store.redis.pipeline
    attempts = []

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def contested():
            attempts.append(pipe)
            if len(attempts) <= failures:
                raise WatchError("synthetic competing controller")
            return execute()

        monkeypatch.setattr(pipe, "execute", contested)
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    if failures == 5:
        with pytest.raises(SwarmError) as error:
            if operation == "start":
                store.start_execution("fixture", agent, current.execution_id)
            elif operation == "update":
                store.put_agent("fixture", replace(current, idle_ticks=2))
            else:
                store.put_agent("fixture", legacy)
        messages = {
            "start": "execution history kept changing; admission was not committed",
            "update": "execution history kept changing; update was not committed",
            "legacy": "agent registry kept changing; update was not committed",
        }
        assert str(error.value) == messages[operation]
        assert protected(store) == before
        assert len(attempts) == 5
    else:
        if operation == "start":
            result = store.start_execution("fixture", agent, current.execution_id)
            assert result.generation == 2
            assert len(store.executions("fixture", agent.seat)) == 2
        elif operation == "update":
            store.put_agent("fixture", replace(current, idle_ticks=2))
            assert store.execution("fixture", current.execution_id).idle_ticks == 2
        else:
            store.put_agent("fixture", legacy)
            assert store.redis.hexists(store.key("fixture", "agents"), legacy.name)
        assert len(attempts) == 2


def test_explicit_local_runtime_identity_is_preserved(store, agent):
    runtime_target = {"server_id": "local-herdr", "process_namespace": "host-one", "pid": 100}
    current = store.start_execution("fixture", replace(agent, runtime_target=runtime_target))
    assert current.runtime_target == runtime_target
    with pytest.raises(SwarmError, match="immutable"):
        store.put_agent("fixture", replace(current, runtime_target={**runtime_target, "pid": 200}))


def test_legacy_writer_cannot_overwrite_concurrent_execution_admission(store, agent, monkeypatch):
    registry = store.execution_registry
    managed = registry.managed
    admitted = []

    def concurrent_admission(slug, name, reader=None):
        result = managed(slug, name) if reader is None else managed(slug, name, reader)
        if not admitted:
            admitted.append(store.start_execution(slug, agent))
        return result

    monkeypatch.setattr(registry, "managed", concurrent_admission)
    with pytest.raises(SwarmError):
        store.put_agent("fixture", agent)
    assert store.agents("fixture") == admitted
    assert store.execution("fixture", admitted[0].execution_id) == admitted[0]


def test_agent_projection_is_compatible_with_the_preceding_strict_reader(store, agent):
    from dataclasses import make_dataclass

    preceding_fields = [
        "name",
        "lane",
        "task",
        "pane_id",
        "harness",
        "account",
        "started_at",
        "state",
        "idle_ticks",
        "model",
        "effort",
        "seat",
        "conversation_id",
        "placement",
        "profile",
        "model_source",
        "model_confidence",
        "profile_decision",
        "input_prompt",
        "input_ticks",
        "choice",
        "launched_at",
        "overlays",
        "launch_timings",
    ]
    preceding_record = make_dataclass("PrecedingAgentRecord", preceding_fields)
    current = store.start_execution("fixture", agent)
    raw = json.loads(store.redis.hget(store.key("fixture", "agents"), agent.name))
    prior = preceding_record(**raw)
    assert prior.name == current.name
    assert prior.task == current.task
    assert prior.seat == current.seat
    assert prior.state == current.state
    assert store.agents("fixture") == [current]


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_id": "exe-unadmitted"},
        {"generation": 1},
        {"runtime_target": {"pid": 1}},
        {"runtime_backend": "unsupported"},
    ],
)
def test_unadmitted_identity_fields_cannot_use_legacy_writer(store, agent, changes):
    before = protected(store)
    with pytest.raises(SwarmError):
        store.put_agent("fixture", replace(agent, **changes))
    assert protected(store) == before


def test_legacy_status_timestamp_changes_only_with_observed_status(store, agent, monkeypatch):
    from scripts.swarm import execution

    monkeypatch.setattr(execution.time, "time", lambda: 1)
    store.put_agent("fixture", agent)
    key = store.key("fixture", "state-since")
    assert store.redis.hget(key, agent.name) == "1000"
    monkeypatch.setattr(execution.time, "time", lambda: 2)
    store.put_agent("fixture", replace(agent, model="new-model"))
    assert store.redis.hget(key, agent.name) == "1000"
    store.put_agent("fixture", replace(agent, idle_ticks=1))
    assert store.redis.hget(key, agent.name) == "2000"


@pytest.mark.parametrize("uid", [None, 1, False])
def test_pod_uid_requires_a_string(store, agent, uid):
    before = protected(store)
    with pytest.raises(SwarmError) as error:
        store.start_execution("fixture", replace(agent, runtime_backend="kubernetes", runtime_target=target(uid)))
    assert str(error.value) == "runtime target identity must be a string"
    assert protected(store) == before


def test_local_pid_one_and_scope_fields_survive_admission(store, agent):
    target = {"server_id": "server", "process_namespace": "namespace", "pid": 1}
    current = store.start_execution("fixture", replace(agent, runtime_target=target))
    assert store.execution("fixture", current.execution_id).runtime_target == target


def test_runtime_binding_refuses_removing_a_local_scope_field(store, agent):
    current = store.start_execution("fixture", replace(agent, runtime_target={"server_id": "server"}))
    before = protected(store)
    with pytest.raises(SwarmError) as error:
        store.put_agent("fixture", replace(current, runtime_target={}))
    assert str(error.value) == "runtime target identity is immutable after binding"
    assert protected(store) == before


def test_separate_swarm_history_never_fills_another_projection(store, agent):
    current = store.start_execution("fixture", agent)
    store.create(SwarmConfig("another", "agentihooks", 1, 0))
    assert store.execution_occupants("another") == {}
    assert store.executions("another", agent.seat) == []
    with pytest.raises(SwarmError):
        store.execution("another", current.execution_id)
    assert store.execution_identity_conflicts_total("fixture") == 0
    assert store.execution_identity_conflicts_total("another") == 1


def test_registered_agent_from_another_swarm_is_refused(store, agent):
    store.create(SwarmConfig("another", "agentihooks", 1, 0))
    foreign = store.next_name("another", "eng")
    before = protected(store)
    with pytest.raises(SwarmError) as error:
        store.start_execution("fixture", replace(agent, name=foreign))
    assert str(error.value) == "execution requires a registered canonical agent name"
    assert protected(store) == before


@pytest.mark.parametrize("field", ["pod_name", "pod_namespace"])
def test_unbound_uid_never_hides_a_changed_runtime_target(store, agent, field):
    current = store.start_execution("fixture", replace(agent, runtime_backend="kubernetes", runtime_target=target()))
    before = protected(store)
    with pytest.raises(SwarmError) as error:
        store.put_agent("fixture", replace(current, runtime_target={**target("pod-one"), field: "other"}))
    assert str(error.value) == "runtime target identity is immutable after binding"
    assert protected(store) == before


@pytest.mark.parametrize("changed_key", ["executions", "agents"])
def test_legacy_write_retries_when_its_observed_state_changes(store, agent, monkeypatch, changed_key):
    store.put_agent("fixture", agent)
    key = store.key("fixture", changed_key)
    watched = []
    original = store.redis.pipeline

    def pipeline(*args, **kwargs):
        pipe = original(*args, **kwargs)
        execute = pipe.execute

        def competing_commit():
            watched.append(True)
            if len(watched) == 1:
                if changed_key == "executions":
                    admitted = replace(agent, execution_id="exe-" + "a" * 32, generation=1)
                    store.redis.hset(key, admitted.execution_id, json.dumps(asdict(admitted)))
                else:
                    store.redis.hset(key, agent.name, json.dumps(asdict(replace(agent, idle_ticks=1))))
            return execute()

        monkeypatch.setattr(pipe, "execute", competing_commit)
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    if changed_key == "executions":
        with pytest.raises(SwarmError) as error:
            store.put_agent("fixture", replace(agent, model="new"))
        assert str(error.value) == "stale or changed execution identity"
        assert json.loads(store.redis.hget(store.key("fixture", "agents"), agent.name))["model"] == ""
    else:
        from scripts.swarm import execution

        monkeypatch.setattr(execution.time, "time", lambda: 20)
        store.put_agent("fixture", agent)
        assert len(watched) == 2
        assert store.redis.hget(store.key("fixture", "state-since"), agent.name) == "20000"


def test_stale_update_cannot_overwrite_a_concurrent_replacement(store, agent, monkeypatch):
    current = store.start_execution("fixture", agent)
    check_uid = store.execution_registry.check_uid
    replacements = []

    def replace_after_read(slug, record, reader):
        check_uid(slug, record, reader)
        if not replacements:
            replacements.append(True)
            replacements[0] = store.start_execution(slug, agent, current.execution_id)

    monkeypatch.setattr(store.execution_registry, "check_uid", replace_after_read)
    with pytest.raises(SwarmError) as error:
        store.put_agent("fixture", replace(current, idle_ticks=1))
    assert str(error.value) == "stale or changed execution identity"
    assert store.agents("fixture") == replacements
    assert store.execution("fixture", current.execution_id) == current
    assert store.executions("fixture", agent.seat) == [current, replacements[0]]
