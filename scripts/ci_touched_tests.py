import pytest


class Selection:
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.count = len(session.items)
        self.maximum = session.config.hook.pytest_xdist_auto_num_workers(config=session.config)


def worker_count(args: list[str]) -> int:
    selection = Selection()
    status = pytest.main([*args, "--collect-only", "-q", "-n", "0"], plugins=[selection])
    if status:
        raise SystemExit(status)
    return max(1, min(selection.maximum, selection.count))
