"""Record the repository files each test opens and whether it starts a process the tracer cannot follow.

Loaded with `-p tests.dep_inputs`; idle unless DEP_INPUTS_DIR names the folder for one JSON line per test.
"""

import hashlib
import json
import os
import sys
import sysconfig
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
        self.children = folder / "children"
        self.logs = {}

    def site_file(self) -> Path:
        name = hashlib.sha256(str(self.children).encode()).hexdigest()[:16]
        return Path(sysconfig.get_path("purelib")) / f"agentihooks-dep-inputs-{name}.pth"

    def register(self, log: Path) -> None:
        registration = self.children / str(os.getpid())
        staged = registration.with_suffix(".new")
        staged.write_text(str(log))
        staged.replace(registration)

    def follow_children(self) -> None:
        self.children.mkdir(parents=True, exist_ok=True)
        self.register(self.children / f"{os.getpid()}-shared")
        hook, children = str(Path(__file__).resolve().with_name("dep_child.py")), str(self.children)
        site = self.site_file()
        staged = site.with_suffix(f".{os.getpid()}")
        staged.write_text(
            f"import os, pathlib; os.path.isdir({children!r}) and exec(pathlib.Path({hook!r}).read_text(),"
            f" {{'__name__': 'dep_child', 'CHILDREN': {children!r}, 'ROOT': {self.root!r}}})\n"
        )
        staged.replace(site)

    def pytest_unconfigure(self, config):
        (self.children / str(os.getpid())).unlink(missing_ok=True)
        if not any(path.name.isdigit() for path in self.children.iterdir()):
            self.site_file().unlink(missing_ok=True)

    def child_reads(self, log: Path) -> set[str]:
        return set(log.read_text().splitlines()) if log.is_file() else set()

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
        log = self.logs.setdefault(item.nodeid, self.children / f"{os.getpid()}-{len(self.logs)}")
        self.register(log)
        try:
            yield
        finally:
            self.register(self.children / f"{os.getpid()}-shared")
            self.current = self.shared

    def relative(self, opened: set[str]) -> list[str]:
        kept = set()
        for full in opened:
            if not full.startswith(self.root):
                continue
            relative = full[len(self.root) :]
            if relative.endswith((".py", ".pyc")) or relative.startswith(SKIPPED) or "__pycache__" in relative:
                continue
            if Path(full).is_relative_to(self.folder):
                continue
            if os.path.isfile(full):
                kept.add(relative)
        return sorted(kept)

    def pytest_sessionfinish(self, session):
        if not self.tests:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        shared = self.shared[0] | self.child_reads(self.children / f"{os.getpid()}-shared")
        for test, log in self.logs.items():
            self.tests[test][0].update(self.child_reads(log))
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
    plugin = DepInputs(Path(os.path.abspath(folder)), config.rootpath)
    sys.addaudithook(plugin.audit)
    plugin.follow_children()
    config.pluginmanager.register(plugin, "dep-inputs")
