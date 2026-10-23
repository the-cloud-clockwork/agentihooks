import argparse
import sys
from pathlib import Path

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.events.subjects import subject
from scripts.hub import runner
from scripts.hub.registry import Reactor, Registry
from tests.chaos.journal import Journal

REACTOR = "chaos-settle"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="chaos hub")
    for flag in ("--url", "--swarm", "--name", "--journal"):
        parser.add_argument(flag, required=True)
    args = parser.parse_args(argv)
    journal = Journal(Path(args.journal), args.name)

    def settle(event: Envelope) -> None:
        journal.write("settled", event=event.id, task=event.data["task"], claim=event.data["claim_key"])

    bus = open_bus(BusConfig(backend="nats", url=args.url))
    reactor = Reactor(REACTOR, subject(args.swarm, "execution", "exited"), settle, singleton=True)
    hub = runner.HubRunner(bus, Registry([reactor]), args.name)
    journal.write("connected")
    try:
        return runner._run(hub, False)
    except BaseException as exc:
        journal.write("disconnected", error=type(exc).__name__)
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
