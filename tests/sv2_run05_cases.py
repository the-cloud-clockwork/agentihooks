from dataclasses import replace

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.runtime.commands import Action, Commands
from tests.test_swarm_v2_commands import fixture, request


def case_a():
    runs = []
    for _ in range(2):
        store, agent, remote, local, service, clock = _fixture()
        before = store.execution_registry.records("fixture")
        detached = service.execute("fixture", request(agent, Action.DETACH), "")
        cancelled = service.execute("fixture", request(agent, Action.CANCEL, "cancel"), "")
        unchanged = store.execution_registry.records("fixture") == before
        runs.append(
            {
                "passed": detached.ok
                and cancelled.ok
                and unchanged
                and remote.running
                and remote.stop_requested
                and not remote.profile
                and not local.calls,
                "task_running": remote.running,
                "saved_profile_removed": not remote.profile,
                "cooperative_stop_requested": remote.stop_requested,
                "authority_unchanged": unchanged,
                "fake_clock_ms": clock["now"],
                "other_backend_untouched": not local.calls,
                "audit": service.audit("fixture"),
                "runtime_commands_by_outcome": service.runtime_commands_by_outcome("fixture"),
            }
        )
    return {"passed": all(run["passed"] for run in runs), "independent_fixtures": runs}


def case_b():
    store, agent, remote, local, service, clock = _fixture()
    current = store.start_execution(
        "fixture",
        replace(agent, execution_id="", generation=0, runtime_target={**agent.runtime_target, "pod_uid": "new-pod"}),
        agent.execution_id,
    )
    before = store.execution_registry.records("fixture")
    refused = service.execute("fixture", request(agent, Action.FORCE_STOP), "")
    unchanged = store.execution_registry.records("fixture") == before
    no_effect = remote.calls == local.calls == [] and remote.running
    corrected = service.execute("fixture", request(current, Action.FORCE_STOP), "")
    protected = (
        store.execution_registry.records("fixture"),
        store.operation_journal.records("fixture"),
        list(remote.calls),
    )
    clock["now"] = 100
    expired = service.execute("fixture", request(current, Action.CANCEL, "expired"), "")
    expiry_safe = protected == (
        store.execution_registry.records("fixture"),
        store.operation_journal.records("fixture"),
        remote.calls,
    )
    return {
        "passed": refused.status == "refused"
        and unchanged
        and no_effect
        and corrected.ok
        and not remote.running
        and remote.calls[0][3]["pod_uid"] == "new-pod"
        and not local.calls
        and expired.status == "refused"
        and expiry_safe,
        "refusal": refused.detail,
        "protected_state_unchanged": unchanged,
        "refused_request_had_no_effect": no_effect,
        "new_valid_request_targets_replacement": corrected.ok,
        "expired_authority_refused": expired.status == "refused",
        "expiry_preserves_protected_state": expiry_safe,
        "fake_clock_ms": clock["now"],
        "lease_scope": "Command service owns no lease; scoped authenticator supplies current authority",
        "runtime_commands_by_outcome": service.runtime_commands_by_outcome("fixture"),
    }


def case_c():
    import fakeredis

    store, agent, remote, local, service, clock = _fixture()
    command = request(agent, Action.DETACH)
    apply = remote.apply_operation

    def interrupted(operation, payload):
        apply(operation, payload)
        raise TimeoutError("fixture acknowledgement lost")

    remote.apply_operation = interrupted
    accepted = service.execute("fixture", command, "")
    intermediate = store.operation_journal.records("fixture")[0].phase.value
    snapshot = store.export("fixture")
    restored = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    restored.restore("fixture", snapshot)
    clock["now"] = 1
    remote.apply_operation = apply
    reopened = Commands(restored, [remote, local], service.authenticate)
    controls = reopened.controls("fixture", agent.seat, "")
    no_replay_on_read = len(remote.calls) == 1
    replay = reopened.execute("fixture", command, "")
    recovered = restored.operation_journal.records("fixture")[0].phase.value
    no_second_effect = len(remote.calls) == 1
    audit_survived = reopened.audit("fixture")[0] == service.audit("fixture")[0]
    current = restored.start_execution(
        "fixture",
        replace(agent, execution_id="", generation=0, runtime_target={**agent.runtime_target, "pod_uid": "new-pod"}),
        agent.execution_id,
    )
    newer = reopened.execute("fixture", request(current, Action.DRAIN, "newer"), "")
    protected = (
        restored.execution_registry.records("fixture"),
        restored.operation_journal.records("fixture"),
        list(remote.calls),
    )
    stale = reopened.execute("fixture", command, "")
    stale_safe = protected == (
        restored.execution_registry.records("fixture"),
        restored.operation_journal.records("fixture"),
        remote.calls,
    )
    current_controls = reopened.controls("fixture", agent.seat, "")
    remote.commands = frozenset((Action.DRAIN, Action.CANCEL, Action.FORCE_STOP))
    unavailable_terminal = reopened.execute("fixture", request(current, Action.ATTACH, "attach"), "")
    rollback = Commands(restored, [remote, local], service.authenticate, enabled=False)
    rollback_controls = rollback.controls("fixture", agent.seat, "")
    return {
        "passed": accepted.status == "ambiguous"
        and intermediate == "unknown"
        and replay.ok
        and recovered == "applied"
        and no_replay_on_read
        and no_second_effect
        and audit_survived
        and controls.execution_id == agent.execution_id
        and controls.generation == 1
        and newer.ok
        and stale.status == "refused"
        and stale_safe
        and current_controls.execution_id == current.execution_id
        and unavailable_terminal.status == "unsupported"
        and remote.running
        and rollback_controls.state == "working"
        and not rollback_controls.actions
        and not local.calls,
        "intermediate_phase": intermediate,
        "recovered_phase": recovered,
        "ui_reopen_is_read_only": no_replay_on_read,
        "replayed_identity_has_no_duplicate_effect": no_second_effect,
        "stale_request_keeps_newer_result": stale_safe,
        "audit_survived_snapshot": audit_survived,
        "disconnected_terminal": unavailable_terminal.status,
        "rollback_status_retained": rollback_controls.state,
        "rollback_controls_hidden": not rollback_controls.actions,
        "fake_clock_ms": clock["now"],
        "durability": "Unknown operation, registry, audit and counts survive; acknowledgement is lost and terminal remains disconnected",
        "runtime_commands_by_outcome": reopened.runtime_commands_by_outcome("fixture"),
    }


def _fixture():
    store, agent, remote, local, service = fixture.__wrapped__()
    clock = {"now": 0}
    authenticate = service.authenticate
    service.authenticate = lambda slug, credential: authenticate(slug, credential) if clock["now"] < 100 else None
    return store, agent, remote, local, service, clock
