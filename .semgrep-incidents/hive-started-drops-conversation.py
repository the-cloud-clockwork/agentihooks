from scripts.events.envelope import Envelope


def without_either(placed, key: str) -> Envelope:
    # ruleid: hive-started-drops-conversation
    return Envelope(
        id="started",
        type="execution.started",
        swarm="rig",
        entity="execution",
        key="execution",
        version=1,
        time=0,
        author="hive",
        data={"claim_key": key, "pane_id": placed.pane_id, "account": placed.account},
    )


def without_overlays(placed, key: str) -> Envelope:
    # ruleid: hive-started-drops-conversation
    return Envelope(
        id="started",
        type="execution.started",
        swarm="rig",
        entity="execution",
        key="execution",
        version=1,
        time=0,
        author="hive",
        data={"claim_key": key, "pane_id": placed.pane_id, "conversation_id": placed.conversation_id},
    )


def carried(placed, key: str) -> Envelope:
    # ok: hive-started-drops-conversation
    return Envelope(
        id="started",
        type="execution.started",
        swarm="rig",
        entity="execution",
        key="execution",
        version=1,
        time=0,
        author="hive",
        data={
            "claim_key": key,
            "pane_id": placed.pane_id,
            "conversation_id": placed.conversation_id,
            "overlays": list(placed.overlays),
        },
    )


def unclaimed(record: dict, hive: str) -> Envelope:
    # ok: hive-started-drops-conversation
    return Envelope(
        id="started",
        type="execution.started",
        swarm="rig",
        entity="execution",
        key="execution",
        version=1,
        time=0,
        author=hive,
        data={"name": record["name"], "hive": hive},
    )
