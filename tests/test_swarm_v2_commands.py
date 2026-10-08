from dataclasses import replace

import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.runtime.commands import Action, Commands, Principal, Request
from scripts.swarm_v2.runtime.operations import Observation, Phase


class Backend:
    backend = "kubernetes"
    commands = frozenset(Action)

    def __init__(self):
        self.effects = {}
        self.calls = []
        self.running = True
        self.profile = True
        self.stop_requested = False

    def observe_operation(self, operation):
        return self.effects.get(operation.operation_id, Observation(Phase.ABSENT))

    def apply_operation(self, operation, payload):
        action = Action(payload["command"])
        self.calls.append((action, operation.execution_id, operation.generation, operation.target))
        if action is Action.DETACH:
            self.profile = False
        if action is Action.CANCEL:
            self.stop_requested = True
        if action is Action.FORCE_STOP:
            self.running = False
        result = Observation(
            Phase.APPLIED,
            operation.execution_id,
            operation.generation,
            operation.backend,
            operation.payload_digest,
            {"command": action.value},
        )
        self.effects[operation.operation_id] = result
        return result


@pytest.fixture
def fixture():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    agent = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "eng"),
            "eng",
            "task",
            state="working",
            seat="eng-1@fixture",
            runtime_backend="kubernetes",
            runtime_target={"pod_namespace": "workers", "pod_name": "same-label", "pod_uid": "first-pod"},
        ),
    )
    remote, local = Backend(), Backend()
    local.backend = "local"
    return (
        store,
        agent,
        remote,
        local,
        Commands(store, [remote, local], lambda slug, credential: Principal("") if credential == "" else None),
    )


def request(agent, action, key="button-one", text=""):
    return Request(agent.execution_id, agent.generation, action, key, text)


def test_removing_terminal_profile_preserves_task_and_cancel_requests_safe_stop(fixture):
    store, agent, remote, local, service = fixture
    before = store.execution("fixture", agent.execution_id)
    result = service.execute("fixture", request(agent, Action.DETACH), "")
    assert result.status == "ok"
    assert remote.profile is False
    assert remote.running is True
    assert remote.stop_requested is False
    assert store.execution("fixture", agent.execution_id) == before
    result = service.execute("fixture", request(agent, Action.CANCEL, "cancel-one"), "")
    assert result.status == "ok"
    assert remote.stop_requested is True
    assert remote.running is True
    assert store.execution("fixture", agent.execution_id).state == "working"
    assert local.calls == []


def test_stale_browser_cannot_force_stop_replacement_pod(fixture):
    store, agent, remote, local, service = fixture
    replacement = store.start_execution(
        "fixture",
        replace(agent, execution_id="", generation=0, runtime_target={**agent.runtime_target, "pod_uid": "new-pod"}),
        agent.execution_id,
    )
    before = store.execution_registry.records("fixture")
    result = service.execute("fixture", request(agent, Action.FORCE_STOP), "")
    assert result.status == "refused"
    assert result.detail == "execution generation is stale"
    assert store.execution_registry.records("fixture") == before
    assert remote.calls == local.calls == []
    assert remote.running is True
    result = service.execute("fixture", request(replacement, Action.FORCE_STOP), "")
    assert result.status == "ok"
    assert remote.calls == [(Action.FORCE_STOP, replacement.execution_id, 2, replacement.runtime_target)]


def test_ui_reopen_reads_current_controls_without_replaying_old_commands(fixture):
    store, agent, remote, local, service = fixture
    old = request(agent, Action.DETACH)
    assert service.execute("fixture", old, "").status == "ok"
    restarted = Commands(store, [remote, local], lambda slug, credential: Principal("") if credential == "" else None)
    controls = restarted.controls("fixture", agent.seat, "")
    assert controls.execution_id == agent.execution_id
    assert controls.generation == agent.generation
    assert controls.actions == tuple(Action)
    assert remote.calls == [(Action.DETACH, agent.execution_id, 1, agent.runtime_target)]
    assert restarted.execute("fixture", old, "").status == "ok"
    assert len(remote.calls) == 1


def test_disconnected_terminal_is_unsupported_without_task_mutation(fixture):
    store, agent, remote, local, service = fixture
    remote.commands = frozenset((Action.DRAIN, Action.CANCEL, Action.FORCE_STOP))
    before = store.execution_registry.records("fixture")
    result = service.execute("fixture", request(agent, Action.ATTACH), "")
    assert result.status == "unsupported"
    assert result.detail == "command is unsupported by the selected backend"
    assert store.execution_registry.records("fixture") == before
    assert remote.calls == local.calls == []
    assert service.controls("fixture", agent.seat, "").actions == (Action.DRAIN, Action.CANCEL, Action.FORCE_STOP)


@pytest.mark.parametrize("principal", [None, "operator", "master@fake", "some-display-label"])
def test_display_label_cannot_grant_control(fixture, principal):
    store, agent, remote, local, service = fixture
    result = service.execute("fixture", request(agent, Action.FORCE_STOP), principal)
    assert result.status == "refused"
    assert result.detail == "authenticated operator or current master required"
    assert remote.calls == local.calls == []
    assert store.operation_journal.records("fixture") == []


def test_master_control_requires_the_current_admitted_incarnation(fixture):
    store, agent, remote, local, service = fixture
    master = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "master"),
            "master",
            "",
            state="working",
            seat="master@fixture",
            runtime_backend="kubernetes",
            runtime_target={"pod_namespace": "workers", "pod_name": "master"},
        ),
    )
    service.authenticate = lambda slug, credential: Principal(master.name, master.execution_id, master.generation)
    assert service.execute("fixture", request(agent, Action.CANCEL), "master-grant").status == "ok"
    replaced = store.start_execution(
        "fixture",
        replace(master, execution_id="", generation=0),
        master.execution_id,
    )
    before = list(remote.calls)
    result = service.execute("fixture", request(agent, Action.FORCE_STOP, "another-button"), "master-grant")
    assert result.status == "refused"
    assert result.detail == "authenticated operator or current master required"
    assert remote.calls == before
    assert replaced.generation == 2


@pytest.mark.parametrize(
    "action, canonical",
    [
        (Action.ATTACH, "recover"),
        (Action.DETACH, "command"),
        (Action.ANSWER, "command"),
        (Action.DRAIN, "drain"),
        (Action.CANCEL, "command"),
        (Action.FORCE_STOP, "terminate"),
    ],
)
def test_each_command_has_its_own_typed_payload_and_operation(action, canonical, fixture):
    store, agent, remote, local, service = fixture
    text = "continue" if action is Action.ANSWER else ""
    result = service.execute("fixture", request(agent, action, text=text), "")
    assert result.operation == action.value
    assert result.value.action == canonical
    assert result.value.result == {"command": action.value}
    assert remote.calls == [(action, agent.execution_id, 1, agent.runtime_target)]
    assert local.calls == []
    assert service.audit("fixture") == [
        {
            "actor": {"name": "", "execution_id": "", "generation": 0},
            "execution_id": agent.execution_id,
            "generation": 1,
            "command": action.value,
            "backend": "kubernetes",
            "outcome": "ok",
        }
    ]
    assert service.runtime_commands_by_outcome("fixture") == {f'["kubernetes", "{action.value}", "ok"]': 1}


def test_changed_action_with_same_key_is_refused_without_replay(fixture):
    store, agent, remote, local, service = fixture
    assert service.execute("fixture", request(agent, Action.DETACH), "").status == "ok"
    result = service.execute("fixture", request(agent, Action.CANCEL), "")
    assert result.status == "refused"
    assert result.detail == "runtime command conflicts with current authority"
    assert remote.stop_requested is False
    assert len(remote.calls) == 1


def test_missing_backend_never_falls_back_to_local(fixture):
    store, agent, remote, local, _ = fixture
    service = Commands(store, [local], lambda slug, credential: Principal(""))
    result = service.execute("fixture", request(agent, Action.FORCE_STOP), "")
    assert result.status == "unavailable"
    assert result.detail == "selected backend is unavailable"
    assert service.controls("fixture", agent.seat, "").actions == ()
    assert remote.calls == local.calls == []
    assert store.operation_journal.records("fixture") == []


def test_rollback_hides_new_controls_and_keeps_authoritative_status(fixture):
    store, agent, remote, local, _ = fixture
    service = Commands(store, [remote, local], lambda slug, credential: Principal(""), enabled=False)
    before = store.execution_registry.records("fixture")
    controls = service.controls("fixture", agent.seat, "")
    assert controls.actions == ()
    assert controls.state == "working"
    assert controls.execution_id == agent.execution_id
    result = service.execute("fixture", request(agent, Action.DRAIN), "")
    assert result.status == "unsupported"
    assert result.detail == "command is unsupported by the selected backend"
    assert store.execution_registry.records("fixture") == before
    assert store.operation_journal.records("fixture") == []
    assert remote.calls == local.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"generation": True},
        {"generation": 0},
        {"generation": -1},
        {"generation": "1"},
        {"execution_id": ""},
        {"execution_id": None},
        {"key": ""},
        {"key": None},
        {"action": "cancel"},
        {"text": None},
        {"text": "wrong"},
        {"action": Action.ANSWER, "text": ""},
    ],
)
def test_invalid_request_is_refused_before_any_effect(fixture, change):
    store, agent, remote, local, service = fixture
    result = service.execute("fixture", replace(request(agent, Action.CANCEL), **change), "")
    assert result.status == "refused"
    assert result.detail == "invalid runtime command"
    assert remote.calls == local.calls == []
    assert store.operation_journal.records("fixture") == []


def test_unauthenticated_or_absent_controls_are_hidden(fixture):
    store, agent, remote, local, service = fixture
    assert service.controls("fixture", agent.seat, "wrong-grant") is None
    assert service.controls("fixture", "eng-2@fixture", "") is None
    assert remote.calls == local.calls == []


def test_answer_and_credential_are_not_written_to_audit_or_journal(fixture):
    store, agent, remote, local, service = fixture
    service.authenticate = lambda slug, credential: Principal("") if credential == "fixture-grant" else None
    assert (
        service.execute("fixture", request(agent, Action.ANSWER, text="private fixture answer"), "fixture-grant").status
        == "ok"
    )
    rows = str(service.audit("fixture")) + str(store.operation_journal.records("fixture"))
    assert "private fixture answer" not in rows
    assert "fixture-grant" not in rows


@pytest.mark.parametrize("phase, status", [(Phase.UNKNOWN, "ambiguous"), (Phase.REFUSED, "refused")])
def test_observation_cannot_authorize_an_unproven_effect(fixture, monkeypatch, phase, status):
    store, agent, remote, local, service = fixture
    monkeypatch.setattr(remote, "observe_operation", lambda operation: Observation(phase))
    result = service.execute("fixture", request(agent, Action.CANCEL), "")
    assert result.status == status
    assert result.value.phase is phase
    assert remote.calls == local.calls == []


def test_lost_acknowledgement_reconciles_without_second_effect(fixture, monkeypatch):
    store, agent, remote, local, service = fixture
    apply = remote.apply_operation

    def lost(operation, payload):
        apply(operation, payload)
        raise TimeoutError("lost fixture acknowledgement")

    monkeypatch.setattr(remote, "apply_operation", lost)
    command = request(agent, Action.CANCEL)
    assert service.execute("fixture", command, "").status == "ambiguous"
    assert Commands(store, [remote, local], service.authenticate).execute("fixture", command, "").status == "ok"
    assert len(remote.calls) == 1
    assert remote.stop_requested is True


def test_current_controls_switch_to_replacement_without_runtime_effect(fixture):
    store, agent, remote, local, service = fixture
    replacement = store.start_execution(
        "fixture",
        replace(agent, execution_id="", generation=0, runtime_target={**agent.runtime_target, "pod_uid": "new-pod"}),
        agent.execution_id,
    )
    controls = service.controls("fixture", agent.seat, "")
    assert controls.execution_id == replacement.execution_id
    assert controls.generation == 2
    assert controls.backend == "kubernetes"
    assert remote.calls == local.calls == []


def test_master_replaced_during_observation_cannot_dispatch(fixture, monkeypatch):
    store, agent, remote, local, service = fixture
    master = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "master"),
            "master",
            "",
            state="working",
            seat="master@fixture",
            runtime_backend="kubernetes",
            runtime_target={"pod_namespace": "workers", "pod_name": "master"},
        ),
    )
    service.authenticate = lambda slug, credential: Principal(master.name, master.execution_id, master.generation)

    def observe(operation):
        store.start_execution("fixture", replace(master, execution_id="", generation=0), master.execution_id)
        return Observation(Phase.ABSENT)

    monkeypatch.setattr(remote, "observe_operation", observe)
    result = service.execute("fixture", request(agent, Action.FORCE_STOP), "master-grant")
    assert result.status == "refused"
    assert remote.calls == local.calls == []
    assert remote.running is True


def test_package_cases_pass_on_the_isolated_fixture():
    from tests.sv2_run05_cases import case_a, case_b, case_c

    assert case_a()["passed"]
    assert case_b()["passed"]
    assert case_c()["passed"]
