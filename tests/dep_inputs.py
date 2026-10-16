"""Record the repository files each test opens and whether it starts a process the tracer cannot follow.

Loaded with `-p tests.dep_inputs`; idle unless DEP_INPUTS_DIR names the folder for one JSON line per test.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ESCAPES = {"subprocess.Popen", "os.posix_spawn", "os.system", "os.exec", "os.fork", "os.forkpty"}
SKIPPED = (".git/", ".venv/", ".playwright/", "node_modules/")


class DepInputs:
    def __init__(self, folder: Path, root: Path):
        self.folder = folder
        self.root = str(root) + os.sep
        self.current = None
        self.tests = {}

    def audit(self, event, args):
        test = self.current
        if test is None:
            return
        if event == "open":
            path, mode = args[0], args[1]
            if isinstance(path, (str, bytes)) and (mode is None or not set(str(mode)) & set("wax")):
                test[0].add(os.fsdecode(path))
        elif event in ESCAPES:
            test[1] = True

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.current = self.tests.setdefault(item.nodeid, [set(), False])
        try:
            yield
        finally:
            self.current = None

    def relative(self, opened):
        kept = set()
        for path in opened:
            full = os.path.abspath(path)
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
        lines = [
            json.dumps({"test": test, "files": self.relative(opened), "escapes": escapes})
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
