import gc

from tests.ledger_load import hierarchy_probe


class Repository:
    def __init__(self, events: list) -> None:
        self.events = events
        self.calls = []

    def import_document(self, slug: str, state: dict, token: str | None = None, replace: bool = False) -> None:
        self.calls.append((slug, state, token, replace))
        self.events.append("imported")


def test_a_replacement_collects_the_garbage_it_left_before_the_next_read() -> None:
    events = []

    def record(phase: str, info: dict) -> None:
        if phase == "stop":
            events.append(("collected", info["generation"]))

    repo, state = Repository(events), {"phases": []}
    gc.callbacks.append(record)
    try:
        hierarchy_probe.store(repo, state, replace=True)
    finally:
        gc.callbacks.remove(record)
    assert repo.calls == [(hierarchy_probe.SLUG, state, "hierarchy-proof", True)]
    assert ("collected", 2) in events[events.index("imported") + 1 :]


def test_a_first_import_stores_without_replacing() -> None:
    repo, state = Repository([]), {"phases": []}
    hierarchy_probe.store(repo, state)
    assert repo.calls == [(hierarchy_probe.SLUG, state, "hierarchy-proof", False)]
