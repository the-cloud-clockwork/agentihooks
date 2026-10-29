import time


def steal(scheduler) -> None:
    successor = {"token": "successor", "expires": time.time() + 3600}
    scheduler.change(lambda document: document.__setitem__("lease", successor))


def free(scheduler) -> None:
    scheduler.change(lambda document: document.pop("lease", None))


def held(scheduler) -> dict | None:
    return scheduler.document().get("lease")


def due(scheduler) -> list[str]:
    return [entry["id"] for entry in scheduler.records() if entry["state"] == "active"]


def pending(scheduler) -> set[str]:
    return {entry["id"] for entry in scheduler.records() if entry["state"] == "pending"}
