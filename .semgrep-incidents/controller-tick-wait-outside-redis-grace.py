from time import monotonic, sleep

from scripts.scheduler.process_timer import wait as scheduled_wait


def cadence_wait_on_the_timer(left: float) -> None:
    # ruleid: controller-tick-wait-outside-redis-grace
    scheduled_wait(left)


def _tick_wait(seconds: float) -> None:
    until = monotonic() + seconds
    try:
        # ok: controller-tick-wait-outside-redis-grace
        scheduled_wait(seconds)
    except ConnectionError:
        sleep(max(0.0, until - monotonic()))
