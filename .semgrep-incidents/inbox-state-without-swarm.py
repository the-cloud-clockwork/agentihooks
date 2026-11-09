from scripts.events.bus import Bus


def unselected() -> type:
    class SeatInbox:
        def __init__(self, bus: Bus, swarm: str) -> None:
            self.bus = bus
            # ruleid: inbox-state-without-swarm
            self.state = bus.state("inbox")

    return SeatInbox


def selected() -> type:
    class SeatInbox:
        def __init__(self, bus: Bus, swarm: str) -> None:
            self.bus = bus.for_swarm(swarm)
            # ok: inbox-state-without-swarm
            self.state = self.bus.state("inbox")

    return SeatInbox
