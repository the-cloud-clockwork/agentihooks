"""Record the repository files each test opens and whether it starts a process the tracer cannot follow.

Loaded with `-p tests.dep_inputs`; idle unless DEP_INPUTS_DIR names the folder for one JSON line per test.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ESCAPES = {"subprocess.Popen", "os.posix_spawn", "os.system", "os.exec", "os.spawn", "os.fork", "os.forkpty"}
SKIPPED = (".git/", ".venv/", ".playwright/", "node_modules/")


class DepInputs:
    def __init__(self, folder: Path, root: Path):
        self.folder = folder
        self.root = str(root) + os.sep
        self.shared = [set(), False]
        self.current = self.shared
        self.tests = {}
        self.fixtures = {}
        self.setting_up = []
        self.escaping = set()

    def audit(self, event: str, args: tuple) -> None:
        test = self.current
        if event == "open":
            path, mode = args[0], args[1]
            if isinstance(path, os.PathLike):
                path = os.fspath(path)
            if isinstance(path, (str, bytes)) and (mode is None or not set(str(mode)) & set("wax")):
                try:
                    test[0].add(os.path.abspath(os.fsdecode(path)))
                except OSError:
                    pass
        elif event in ESCAPES and test is not self.shared:
            test[1] = True
            self.escaping.update(self.setting_up)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        self.setting_up.append(fixturedef.argname)
        try:
            yield
        finally:
            self.setting_up.pop()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.current = self.tests.setdefault(item.nodeid, [set(), False])
        self.fixtures[item.nodeid] = set(getattr(item, "fixturenames", ()))
        try:
            yield
        finally:
            self.current = self.shared

    def relative(self, opened: set[str]) -> list[str]:
        kept = set()
        for full in opened:
            if not full.startswith(self.root):
                continue
            relative = full[len(self.root) :]
            if relative.endswith((".py", ".pyc")) or relative.startswith(SKIPPED) or "__pycache__" in relative:
                continue
            if os.path.isfile(full):
                kept.add(relative)
        return sorted(kept)

    def pytest_sessionfinish(self, session):
        if not self.tests:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        shared = self.shared[0]
        lines = [
            json.dumps(
                {
                    "test": test,
                    "files": self.relative(opened | shared),
                    "escapes": escapes or bool(self.fixtures[test] & self.escaping),
                }
            )
            for test, (opened, escapes) in self.tests.items()
        ]
        (self.folder / f"{uuid.uuid4().hex}.jsonl").write_text("".join(line + "\n" for line in lines))


def pytest_configure(config):
    folder = os.environ.get("DEP_INPUTS_DIR")
    controller = not hasattr(config, "workerinput") and config.getoption("dist", "no") != "no"
    if not folder or controller:
        return
    plugin = DepInputs(Path(folder), config.rootpath)
    sys.addaudithook(plugin.audit)
    config.pluginmanager.register(plugin, "dep-inputs")
