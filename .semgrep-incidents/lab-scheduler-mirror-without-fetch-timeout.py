class Mirror:
    def __init__(self, timeout_s: float | None = None) -> None:
        self.timeout_s = timeout_s


class Unbounded:
    def _mirror(self) -> Mirror:
        # ruleid: lab-scheduler-mirror-without-fetch-timeout
        return Mirror()


class ExplicitlyUnbounded:
    def _mirror(self) -> Mirror:
        # ruleid: lab-scheduler-mirror-without-fetch-timeout
        return Mirror(timeout_s=None)


class Bounded:
    def _mirror(self) -> Mirror:
        # ok: lab-scheduler-mirror-without-fetch-timeout
        return Mirror(timeout_s=60.0)


def worker() -> Mirror:
    # ok: lab-scheduler-mirror-without-fetch-timeout
    return Mirror()
