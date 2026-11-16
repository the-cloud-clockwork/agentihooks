from typing import Any


def claim_without_runtime_target(name: str, lane: str, task_id: str, seat: str, now: int, hive: str) -> Any:
    from scripts.swarm.store import AgentRecord

    # ruleid: admission-claim-omits-runtime-target
    record = AgentRecord(name, lane, task_id, seat=seat, state="starting", started_at=now, hive=hive)
    return record


def claim_with_runtime_target(name: str, lane: str, task_id: str, seat: str, now: int, hive: str) -> Any:
    from scripts.swarm.store import AgentRecord

    # ok: admission-claim-omits-runtime-target
    record = AgentRecord(
        name,
        lane,
        task_id,
        seat=seat,
        state="starting",
        started_at=now,
        hive=hive,
        runtime_target={"host_id": hive},
    )
    return record
