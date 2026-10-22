import hashlib
import json
import logging
from dataclasses import replace

from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.events.subjects import subject

LIVE = frozenset({"working", "running", "idle", "waiting", "blocked"})
STATE = "hive-executions"
STARTS = "execution-starts"
ENDED = frozenset({"exited", "dead"})
_LOG = logging.getLogger(__name__)


def identity(swarm: str, name: str, generation: int) -> str:
    return hashlib.sha256(json.dumps([swarm, name, generation]).encode()).hexdigest()


def emit(bus: Bus, event: Envelope) -> None:
    states = bus.state(STATE)
    key = hashlib.sha256(event.id.encode()).hexdigest()
    if states.get(key) is not None:
        return
    bus.events.publish(subject(event.swarm, "execution", event.type.split(".")[-1]), event)
    states.create(key, b"sent")


def prepare(bus: Bus, swarm: str, name: str, generation: int) -> None:
    starts = bus.state(STARTS)
    key = identity(swarm, name, generation)
    if starts.get(key) is None:
        starts.create(key, b"null")


def started(bus: Bus, event: Envelope) -> None:
    starts = bus.state(STARTS)
    key = identity(event.swarm, event.data["name"], event.version)
    previous = starts.get(key)
    value = json.dumps(event.to_dict()).encode()
    if previous is None:
        starts.create(key, value)
    elif previous.value != value:
        starts.update(key, value, previous.revision)
    emit(bus, event)


def initial(bus: Bus, hive: str, execution: str, record: dict, now: int) -> Envelope | None:
    stored = bus.state(STARTS).get(identity(record["slug"], record["name"], record["generation"]))
    if stored is not None:
        value = json.loads(stored.value)
        return Envelope.from_dict(value) if value else None
    key = identity(hive, execution, record["generation"])
    return Envelope(
        id=f"started:{key}:{record['generation']}",
        type="execution.started",
        swarm=record["slug"],
        entity="execution",
        key=execution,
        version=record["generation"],
        time=now,
        author=hive,
        data={"name": record["name"], "hive": hive, "generation": record["generation"]},
    )


def publish(bus: Bus, hive: str, report: dict) -> None:
    for execution, record in report["executions"].items():
        observed = record["pane"]["state"]
        if observed not in LIVE and observed not in ENDED:
            continue
        event = initial(bus, hive, execution, record, report["observed_at"])
        if event is None:
            continue
        exited = replace(
            event, id=f"exited:{event.id}", type="execution.exited", time=report["observed_at"], cause=event.id
        )
        if observed in ENDED:
            emit(bus, exited)
        elif bus.state(STATE).get(hashlib.sha256(exited.id.encode()).hexdigest()) is None:
            emit(bus, event)


def observe(hive: str, report: dict) -> None:
    config = BusConfig.from_environ()
    if config.backend == "memory" or not report["executions"]:
        return
    try:
        bus = open_bus(config)
        try:
            publish(bus, hive, report)
        finally:
            bus.close()
    except Exception as exc:
        _LOG.warning("Execution events deferred: %s", type(exc).__name__)
