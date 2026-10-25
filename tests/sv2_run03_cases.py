from dataclasses import replace

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.runtime.operations import OperationConflict, Operations, Phase
from tests.test_swarm_v2_operations import fixture, request


def case_a():
    runs = []
    for _ in range(2):
        store, agent, transport, operations = fixture.__wrapped__()
        first = operations.execute("fixture", request(agent))
        before = operations.runtime_ambiguous_operations("fixture")
        retried = operations.execute("fixture", request(agent, key="new retry label"))
        stored = store.operation_journal.get("fixture", first.operation_id)
        runs.append(
            {
                "passed": first.phase is Phase.UNKNOWN
                and retried.phase is Phase.APPLIED
                and first.operation_id == retried.operation_id
                and stored == retried
                and transport.creations == 1
                and len(transport.effects) == 1,
                "effects": transport.creations,
                "runtime_ambiguous_operations": {
                    "before": before,
                    "after": operations.runtime_ambiguous_operations("fixture"),
                },
                "observed": "one fixture runtime object",
                "accepted": stored.phase.value,
                "selected_backend": agent.runtime_backend,
            }
        )
    return {"passed": all(run["passed"] for run in runs), "independent_fixtures": runs}


def case_b():
    runs = []
    for _ in range(2):
        store, agent, transport, operations = fixture.__wrapped__()
        first = operations.execute("fixture", request(agent, "command", {"prompt": "original"}, "command"))
        before = store.export("fixture")
        refused = False
        try:
            operations.execute("fixture", request(agent, "command", {"prompt": "changed"}, "command"))
        except OperationConflict:
            refused = True
        unchanged = before == store.export("fixture")
        corrected = operations.execute("fixture", request(agent, "command", {"prompt": "original"}, "command"))
        runs.append(
            {
                "passed": refused
                and unchanged
                and corrected.phase is Phase.APPLIED
                and corrected.operation_id == first.operation_id
                and transport.creations == 1,
                "conflict": refused,
                "protected_state_unchanged": unchanged,
                "effects": transport.creations,
                "runtime_ambiguous_operations": operations.runtime_ambiguous_operations("fixture"),
            }
        )
    return {"passed": all(run["passed"] for run in runs), "independent_fixtures": runs}


def case_c():
    import fakeredis

    runs = []
    for _ in range(2):
        store, agent, transport, operations = fixture.__wrapped__()
        first = operations.execute("fixture", request(agent))
        snapshot = store.export("fixture")
        restored = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        restored.restore("fixture", snapshot)
        restarted = Operations(restored, [transport], dispatch_enabled=False)
        recovered = restarted.recover("fixture")
        replayed = restarted.execute("fixture", request(agent))
        rollback = restarted.execute("fixture", request(agent, "command", key="rollback"))
        rollback_safe = rollback.phase is Phase.ACCEPTED and transport.creations == 1
        preserved = restored.operation_journal.records("fixture")
        restored.start_execution("fixture", replace(agent, execution_id="", generation=0), agent.execution_id)
        stale = False
        try:
            restarted.execute("fixture", request(agent))
        except OperationConflict:
            stale = True
        runs.append(
            {
                "passed": len(recovered) == 1
                and recovered[0] == replayed
                and replayed.phase is Phase.APPLIED
                and replayed.operation_id == first.operation_id
                and rollback_safe
                and stale
                and preserved == restored.operation_journal.records("fixture"),
                "snapshot_journal_retained": True,
                "observed_effects": transport.creations,
                "stale_replay_refused": stale,
                "rollback_dispatch_disabled": rollback_safe,
                "runtime_ambiguous_operations": restarted.runtime_ambiguous_operations("fixture"),
            }
        )
    return {"passed": all(run["passed"] for run in runs), "independent_fixtures": runs}
