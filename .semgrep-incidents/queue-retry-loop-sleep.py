import time


def hold_the_runner(queued: bool) -> bool:
    # ruleid: queue-retry-loop-sleep
    for _ in range(60):
        if not queued:
            return False
        time.sleep(30)
    return queued


def release_the_runner(queued: bool) -> dict[str, bool]:
    # ok: queue-retry-loop-sleep
    return {"requeue": False, "waiting": queued}
