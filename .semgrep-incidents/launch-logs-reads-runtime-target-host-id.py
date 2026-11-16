from typing import Any


def receipt_hive_from_runtime_target(agent: Any) -> str:
    # ruleid: launch-logs-reads-runtime-target-host-id
    return agent.runtime_target["host_id"]


def receipt_hive_from_agent(agent: Any) -> str:
    # ok: launch-logs-reads-runtime-target-host-id
    return agent.hive
