import hashlib
import json
import time
import os
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest


class KillMemory:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.mutant: str | None = None
        self.rank: dict[str, int] = {}

    def _path(self, mutant: str) -> Path:
        # Upload refuses some characters mangled names may hold, so the file is named by a digest.
        return self.folder / hashlib.sha256(mutant.partition("__mutmut_")[0].encode()).hexdigest()

    def killer(self, mutant: str) -> str | None:
        path = self._path(mutant)
        return path.read_text() if path.is_file() else None

    def cost(self, mutant: str, tests: Iterable[str]) -> float | None:
        path = self.folder / (hashlib.sha256(mutant.encode()).hexdigest() + ".cost")
        if not path.is_file():
            return None
        record = json.loads(path.read_text())
        return record["seconds"] if record["tests"] == sorted(tests) else None

    def order(self, mutant: str, tests: Iterable[str]) -> list[str]:
        killer = self.killer(mutant)
        return sorted(tests, key=lambda test: test != killer)

    def prepare(self, mutant: str | None, tests: Iterable[str]) -> Iterable[str]:
        self.mutant = mutant
        if mutant is None:
            return tests
        ordered = self.order(mutant, tests)
        self.rank = {test: index for index, test in enumerate(ordered)}
        return ordered

    def wrap(self, execute_pytest: Callable[..., int]) -> Callable[..., int]:
        def execute_remembering_kills(runner, params, **kwargs):
            mutant = self.mutant
            if mutant is not None:
                kwargs["plugins"] = [*kwargs.get("plugins", []), self]
            started = time.monotonic()
            status = execute_pytest(runner, params, **kwargs)
            if mutant is not None and status in (0, 1):
                seconds = time.monotonic() - started
                self.folder.mkdir(parents=True, exist_ok=True)
                path = self.folder / (hashlib.sha256(mutant.encode()).hexdigest() + ".cost")
                partial = path.with_name(f"{path.name}.{os.getpid()}")
                partial.write_text(json.dumps({"tests": sorted(self.rank), "seconds": seconds}))
                partial.replace(path)
            return status

        return execute_remembering_kills

    # The test conftest sorts paths by folder so package conftests load; the run order is set back here.
    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        items.sort(key=lambda item: self.rank.get(item.nodeid, len(self.rank)))

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if not report.failed or self.mutant is None:
            return
        path = self._path(self.mutant)
        self.mutant = None
        self.folder.mkdir(parents=True, exist_ok=True)
        # Children of one run write the same function concurrently; a rename never shows a partial name.
        partial = path.with_name(f"{path.name}.{os.getpid()}")
        partial.write_text(report.nodeid)
        partial.replace(path)
