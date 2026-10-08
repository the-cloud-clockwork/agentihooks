from dataclasses import replace

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.runtime.commands import Action, Commands, Principal, Role
from tests.test_swarm_v2_commands import fixture, request


def case_a():
    runs = []
    for _ in range(2):
        store, agent, remote, local, service = fixture.__wrapped__()
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
                "other_backend_untouched": not local.calls,
                "audit": service.audit("fixture"),
                "runtime_commands_by_outcome": service.runtime_commands_by_outcome("fixture"),
            }
        )
    return {"passed": all(run["passed"] for run in runs), "independent_fixtures": runs}


def case_b():
    store, agent, remote, local, service = fixture.__wrapped__()
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
    return {
        "passed": refused.status == "refused"
        and unchanged
        and no_effect
        and corrected.ok
        and not remote.running
        and remote.calls[0][3]["pod_uid"] == "new-pod"
        and not local.calls,
        "refusal": refused.detail,
        "protected_state_unchanged": unchanged,
        "refused_request_had_no_effect": no_effect,
        "new_valid_request_targets_replacement": corrected.ok,
        "runtime_commands_by_outcome": service.runtime_commands_by_outcome("fixture"),
    }


def case_c():
    import fakeredis

    store, agent, remote, local, service = fixture.__wrapped__()
    command = request(agent, Action.DETACH)
    accepted = service.execute("fixture", command, "")
    snapshot = store.export("fixture")
    restored = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    restored.restore("fixture", snapshot)
    reopened = Commands(restored, [remote, local], service.authenticate)
    controls = reopened.controls("fixture", agent.seat, "")
    no_replay_on_read = len(remote.calls) == 1
    replay = reopened.execute("fixture", command, "")
    no_second_effect = len(remote.calls) == 1
    audit_survived = reopened.audit("fixture")[0] == service.audit("fixture")[0]
    remote.commands = frozenset((Action.DRAIN, Action.CANCEL, Action.FORCE_STOP))
    unavailable_terminal = reopened.execute("fixture", request(agent, Action.ATTACH, "attach"), "")
    rollback = Commands(
        restored, [remote, local], lambda slug, credential: Principal("fixture-operator", Role.OPERATOR), enabled=False
    )
    rollback_controls = rollback.controls("fixture", agent.seat, "")
    return {
        "passed": accepted.ok
        and replay.ok
        and no_replay_on_read
        and no_second_effect
        and audit_survived
        and controls.execution_id == agent.execution_id
        and controls.generation == 1
        and unavailable_terminal.status == "unsupported"
        and remote.running
        and rollback_controls.state == "working"
        and not rollback_controls.actions
        and not local.calls,
        "ui_reopen_is_read_only": no_replay_on_read,
        "replayed_identity_has_no_duplicate_effect": no_second_effect,
        "audit_survived_snapshot": audit_survived,
        "disconnected_terminal": unavailable_terminal.status,
        "rollback_status_retained": rollback_controls.state,
        "rollback_controls_hidden": not rollback_controls.actions,
        "durability": "execution registry, command journal, audit and counts survive; terminal transport remains disconnected",
        "runtime_commands_by_outcome": reopened.runtime_commands_by_outcome("fixture"),
    }
