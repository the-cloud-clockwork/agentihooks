import subprocess
import sys

import pytest


class Selection:
    def __init__(self) -> None:
        self.count = 0
        self.maximum = 1

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.count = len(session.items)
        self.maximum = session.config.hook.pytest_xdist_auto_num_workers(config=session.config)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    selection = Selection()
    status = pytest.main([*args, "--collect-only", "-q", "-n", "0"], plugins=[selection])
    if status:
        return int(status)
    workers = max(1, min(selection.maximum, selection.count))
    print(f"Touched tests: {selection.count}; workers: {workers}", flush=True)
    return subprocess.run([sys.executable, "-m", "pytest", *args, "-n", str(workers)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
