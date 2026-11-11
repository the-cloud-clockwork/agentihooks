import signal
from collections.abc import Callable
from typing import Any

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def terminate_only(stopped: Callable[..., Any], run: Callable[[], int]) -> int:
    # ruleid: controller-stop-signal-pair
    previous = signal.signal(signal.SIGTERM, stopped)
    try:
        return run()
    finally:
        # ruleid: controller-stop-signal-pair
        signal.signal(signal.SIGTERM, previous)


def both_signals(stopped: Callable[..., Any], run: Callable[[], int]) -> int:
    # ok: controller-stop-signal-pair
    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    return run()


def every_stop_signal(stopped: Callable[..., Any], run: Callable[[], int]) -> int:
    previous = {number: signal.signal(number, stopped) for number in STOP_SIGNALS}
    try:
        return run()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
