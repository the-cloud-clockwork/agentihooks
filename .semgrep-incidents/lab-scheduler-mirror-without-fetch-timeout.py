from typing import Protocol


class Mirror:
    def __init__(self, timeout_s: float | None = None) -> None:
        self.timeout_s = timeout_s


class MirrorFactory(Protocol):
    def _mirror(self) -> Mirror: ...


class Unbounded(MirrorFactory):
    def _mirror(self) -> Mirror:
        # ruleid: lab-scheduler-mirror-without-fetch-timeout
        return Mirror()


class ExplicitlyUnbounded(MirrorFactory):
    def _mirror(self) -> Mirror:
        # ruleid: lab-scheduler-mirror-without-fetch-timeout
        return Mirror(timeout_s=None)


class Bounded(MirrorFactory):
    def _mirror(self) -> Mirror:
        # ok: lab-scheduler-mirror-without-fetch-timeout
        return Mirror(timeout_s=60.0)


def worker() -> Mirror:
    # ok: lab-scheduler-mirror-without-fetch-timeout
    return Mirror()
