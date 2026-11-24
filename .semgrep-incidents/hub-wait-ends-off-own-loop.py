from typing import Any


class Registry:
    def __init__(self, reactors: list[Any], own_loop: list[Any] | None = None) -> None:
        self.reactors = [*reactors, *(own_loop or [])]


class WaitEnds:
    def __init__(self, clock: Any) -> None:
        self.clock = clock

    def reactors(self) -> list[Any]:
        return [self.clock]


def configured(pulls: Any, clock: Any) -> Registry:
    # ruleid: hub-wait-ends-off-own-loop
    return Registry([pulls, *WaitEnds(clock=clock).reactors()])


class Looped:
    @staticmethod
    def configured(pulls: Any, clock: Any) -> Registry:
        # ok: hub-wait-ends-off-own-loop
        return Registry([pulls], own_loop=WaitEnds(clock=clock).reactors())
