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
        self.collections = {}
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
    def pytest_make_collect_report(self, collector):
        if not isinstance(collector, pytest.Module):
            yield
            return
        previous = self.current
        collected = self.collections.setdefault(collector.nodeid, [set(), False])
        imported = set(sys.modules)
        self.current = collected
        try:
            yield
        finally:
            previous[0].update(path for path in collected[0] if not path.startswith(self.root + ".github/workflows/"))
            self.current = previous
            for name in set(sys.modules) - imported:
                filename = getattr(sys.modules[name], "__file__", "") or ""
                if filename.startswith(self.root) and filename.endswith(".py"):
                    found = self.collections.setdefault(filename[len(self.root) :], [set(), False])
                    found[0].update(collected[0])
                    found[1] = found[1] or collected[1]

    def pytest_collection_finish(self, session):
        # -p loads this plugin before pytest-cov starts; a module level import leaves scripts import lines unmeasured.
        from scripts.ci_mutation.scope import imported_names

        imports = {}
        for path in self.collections:
            source = Path(self.root) / path
            text = source.read_text()
            names = imported_names(text, Path(path).parent.parts)
            if "__import__" in text or any(name == "importlib" or name.startswith("importlib.") for name in names):
                self.collections[path][1] = True
            filenames = {getattr(sys.modules.get(name), "__file__", "") or "" for name in names}
            imports[path] = {name[len(self.root) :] for name in filenames if name.startswith(self.root)}
        for _ in self.collections:
            before = sum(len(found[0]) + found[1] for found in self.collections.values())
            for path, names in imports.items():
                found = self.collections[path]
                for name in names & self.collections.keys():
                    dependency = self.collections[name]
                    found[0].update(dependency[0])
                    found[1] = found[1] or dependency[1]
            if before == sum(len(found[0]) + found[1] for found in self.collections.values()):
                break

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        self.setting_up.append(fixturedef.argname)
        try:
            yield
        finally:
            self.setting_up.pop()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        collected = self.collections.get(item.nodeid.split("::", 1)[0], [set(), False])
        self.current = self.tests.setdefault(item.nodeid, [set(), False])
        self.current[0].update(collected[0])
        self.current[1] = self.current[1] or collected[1]
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
