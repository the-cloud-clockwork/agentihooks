from typing import Any


def waiting(expert_launch: Any) -> dict[str, Any]:
    # ruleid: expert-launch-ack-later
    return {"requires_harness": expert_launch.HARNESS, "id": "expert-1"}


def disabled(expert_launch: Any, tick: Any) -> dict[str, Any]:
    # ruleid: expert-launch-ack-later
    return {"requires_harness": expert_launch.HARNESS, tick.ACK_LATER: False}


def deferred(expert_launch: Any, tick: Any) -> dict[str, Any]:
    # ok: expert-launch-ack-later
    return {"requires_harness": expert_launch.HARNESS, tick.ACK_LATER: True}
