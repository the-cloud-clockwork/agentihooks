import argparse
import sys
from pathlib import Path

from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.events.subjects import subject
from scripts.hub import hive_loss, runner
from scripts.hub.registry import Reactor, Registry
from scripts.swarm import readiness
from tests.chaos.journal import Journal, now_ms

LANE = "eng"

REACTOR = "chaos-settle"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="chaos hub")
    for flag in ("--url", "--swarm", "--name", "--journal"):
        parser.add_argument(flag, required=True)
    args = parser.parse_args(argv)
    journal = Journal(Path(args.journal), args.name)

    def settle(event: Envelope) -> None:
        journal.write("settled", event=event.id, task=event.data["task"], claim=event.data["claim_key"])
        if event.data.get("reason") == hive_loss.REASON:
            reopen(event)

    def reopen(event: Envelope) -> None:
        generation = event.data["task_generation"] + 1
        task = event.data["task"]
        offer = Envelope(
            id=readiness.offer_id(args.swarm, {"id": task, "claim_generation": generation}),
            type="task.ready",
            swarm=args.swarm,
            entity="task",
            key=task,
            version=generation,
            time=now_ms(),
            author="chaos",
            data={"lane": LANE, "generation": generation},
        )
        bus.queue(readiness.queue_name(args.swarm, LANE), QueuePolicy()).offer(offer)
        journal.write("reopened", task=task, generation=generation)

    bus = open_bus(BusConfig(backend="nats", url=args.url))
    reactor = Reactor(REACTOR, subject(args.swarm, "execution", "exited"), settle, singleton=True)
    hub = runner.HubRunner(bus, Registry([hive_loss.HiveLoss(bus).reactor(), reactor]), args.name)
    journal.write("connected")
    try:
        return runner._run(hub, False)
    except BaseException as exc:
        journal.write("disconnected", error=type(exc).__name__)
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
