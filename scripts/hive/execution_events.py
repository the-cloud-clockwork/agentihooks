import hashlib
import json

from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.events.subjects import subject

LIVE = frozenset({"working", "running", "idle", "waiting", "blocked"})
STATE = "hive-executions"


def publish(bus: Bus, hive: str, report: dict) -> None:
    states = bus.state(STATE)
    for execution, record in report["executions"].items():
        observed = record["pane"]["state"]
        if observed not in LIVE and observed != "exited":
            continue
        phase = "exited" if observed == "exited" else "started"
        key = hashlib.sha256(json.dumps([hive, execution, record["generation"]]).encode()).hexdigest()
        previous = states.get(key)
        if previous and previous.value in (b"exited", phase.encode()):
            continue
        event = Envelope(
            id=f"{key}:{phase}",
            type=f"execution.{phase}",
            swarm=record["slug"],
            entity="execution",
            key=execution,
            version=record["generation"],
            time=report["observed_at"],
            author=hive,
            data={"name": record["name"], "hive": hive, "generation": record["generation"]},
        )
        bus.events.publish(subject(event.swarm, "execution", phase), event)
        if previous:
            states.update(key, phase.encode(), previous.revision)
        else:
            states.create(key, phase.encode())


def observe(hive: str, report: dict) -> None:
    config = BusConfig.from_environ()
    if config.backend == "memory" or not report["executions"]:
        return
    bus = open_bus(config)
    try:
        publish(bus, hive, report)
    finally:
        bus.close()
