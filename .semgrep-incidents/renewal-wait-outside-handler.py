import sys
import traceback
from time import sleep

from scripts.scheduler import process_timer
from scripts.scheduler.process_timer import wait as scheduled_wait

KEEP_S = 100.0


def keep(credential: str) -> None:
    del credential


def renew_with_the_wait_before_the_handler(credential: str) -> None:
    while True:
        # ruleid: renewal-wait-outside-handler
        scheduled_wait(KEEP_S)
        try:
            keep(credential)
        except Exception:
            print("renewal failed", file=sys.stderr)
            traceback.print_exc()


def renew_on_the_module_timer_with_no_handler(credential: str) -> None:
    while True:
        # ruleid: renewal-wait-outside-handler
        process_timer.wait(KEEP_S)
        keep(credential)


def renew_past_a_failed_wait(credential: str) -> None:
    while True:
        try:
            # ok: renewal-wait-outside-handler
            scheduled_wait(KEEP_S)
        except Exception:
            print("renewal wait failed", file=sys.stderr)
            traceback.print_exc()
            sleep(KEEP_S)
        try:
            keep(credential)
        except Exception:
            print("renewal failed", file=sys.stderr)
            traceback.print_exc()
