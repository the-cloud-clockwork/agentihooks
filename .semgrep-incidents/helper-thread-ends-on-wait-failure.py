import sys
import threading

HEADROOM_SECONDS = 10


def scheduled_wait(delay: float, state: object = None, timeout: float | None = None) -> None:
    del delay, state, timeout


def publish_headroom(stop: threading.Event) -> None:
    while not stop.is_set():
        # ruleid: helper-thread-ends-on-wait-failure
        try:
            scheduled_wait(HEADROOM_SECONDS, timeout=HEADROOM_SECONDS)
        except (ConnectionError, TimeoutError) as exc:
            print(f"hive headroom wait failed ({exc})", file=sys.stderr)
            return


def publish_headroom_kept(stop: threading.Event) -> None:
    while not stop.is_set():
        # ok: helper-thread-ends-on-wait-failure
        try:
            scheduled_wait(HEADROOM_SECONDS, timeout=HEADROOM_SECONDS)
        except (ConnectionError, TimeoutError) as exc:
            print(f"hive headroom wait failed ({exc})", file=sys.stderr)
            stop.wait(HEADROOM_SECONDS)
            continue
