import argparse
import json
import re
from dataclasses import asdict, dataclass, replace

from nats.js.errors import APIError

from .config import BusConfig
from .nats import DEFAULT_URL, QUEUE_SUBJECT, NatsBus, queue_stream


@dataclass
class ReadyQueue:
    swarm: str
    lane: str
    stream: str
    retained: int
    last_sequence: int
    remaining: int
    kept: int = 0


async def _inventory(bus: NatsBus, swarms: list[str]) -> list[ReadyQueue]:
    offset, queues = 0, []
    pattern = re.compile(rf"{re.escape(QUEUE_SUBJECT)}\.swarm\.([^.]+)\.ready\.(eng|ci|plan)")
    while True:
        page = await bus._js.streams_info_iterator(offset=offset)
        for info in page:
            subjects = info.config.subjects or []
            if len(subjects) != 1 or not (match := pattern.fullmatch(subjects[0])):
                continue
            swarm, lane = match.groups()
            if swarms and swarm not in swarms:
                continue
            if info.config.name != queue_stream(f"swarm.{swarm}.ready.{lane}"):
                raise ValueError("legacy ready queue ownership differs from its stream name")
            queues.append(
                ReadyQueue(swarm, lane, info.config.name, info.state.messages, info.state.last_seq, info.state.messages)
            )
        offset += len(page.streams)
        if offset >= page.total:
            return sorted(queues, key=lambda queue: (queue.swarm, queue.lane))
        if not page.streams:
            raise RuntimeError("NATS stream inventory did not advance")


def _move(bus: NatsBus, queue: ReadyQueue) -> None:
    before = bus._loop.run(bus._js.stream_info(queue.stream))
    if (before.state.messages, before.state.last_seq) != (queue.retained, queue.last_sequence):
        raise RuntimeError("legacy ready queue changed after inventory")
    bus.migrate_queue(queue.swarm, f"swarm.{queue.swarm}.ready.{queue.lane}", bus._config.queue)
    after = bus._loop.run(bus._js.stream_info(queue.stream))
    queue.remaining = after.state.messages
    if after.state.messages or after.state.last_seq != queue.last_sequence:
        raise RuntimeError("legacy ready queue changed during migration")
    queue.kept = queue.retained


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory or migrate retained legacy ready offers to scoped queues")
    parser.add_argument("--swarm", action="append", default=[], help="Limit the move to this swarm; repeat for more")
    parser.add_argument("--apply", action="store_true", help="Move retained offers; default is read only inventory")
    parser.add_argument("--quiescent", action="store_true", help="Confirm legacy producers and consumers are quiet")
    args = parser.parse_args(argv)
    if args.apply and (not args.quiescent or not args.swarm):
        parser.error("--apply requires explicit --swarm selection and --quiescent")
    config = BusConfig.from_environ()
    if config.backend != "nats" or args.apply and not config.provision:
        parser.error("migration requires the NATS backend and privileged provisioning for --apply")
    bus = NatsBus(replace(config, scoped=True, provision=args.apply), config.url or DEFAULT_URL)
    try:
        queues = bus._loop.run(_inventory(bus, args.swarm))
        failures = []
        if args.apply:
            for queue in queues:
                try:
                    _move(bus, queue)
                except (RuntimeError, ValueError, PermissionError, APIError) as error:
                    failures.append({"swarm": queue.swarm, "lane": queue.lane, "error": str(error)})
                    break
        print(
            json.dumps(
                {
                    "applied": args.apply,
                    "retained": sum(queue.retained for queue in queues),
                    "kept": sum(queue.kept for queue in queues),
                    "remaining": sum(queue.remaining for queue in queues),
                    "queues": [asdict(queue) for queue in queues],
                    "failures": failures,
                },
                sort_keys=True,
            )
        )
        return 1 if failures else 0
    finally:
        bus.close()


if __name__ == "__main__":
    raise SystemExit(main())
