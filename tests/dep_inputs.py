"""Record the repository files each test opens and whether it starts a process the tracer cannot follow.

Loaded with `-p tests.dep_inputs`; idle unless DEP_INPUTS_DIR names the folder for one JSON line per test.
"""

import hashlib
import json
import os
import sys
import sysconfig
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import FrameType

import pytest

ESCAPES = {"subprocess.Popen", "os.posix_spawn", "os.system", "os.exec", "os.spawn", "os.fork", "os.forkpty"}
SKIPPED = (".git/", ".venv/", ".playwright/", "node_modules/")
HELPERS = (f"{os.sep}pathlib.py", f"{os.sep}tokenize.py")
LOADERS = (
    "<frozen importlib",
    f"{os.sep}importlib{os.sep}",
    f"{os.sep}_pytest{os.sep}assertion{os.sep}",
    "linecache.py",
)


def loading(frame: FrameType | None) -> bool:
    """Whether the code that opened a file, past pathlib and tokenize helpers, is an import, assertion rewriting or
    traceback source lookup rather than test code."""
    while frame is not None and frame.f_code.co_filename.endswith(HELPERS):
        frame = frame.f_back
    return frame is not None and any(loader in frame.f_code.co_filename for loader in LOADERS)


class DepInputs:
    def __init__(self, folder: Path, root: Path) -> None:
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
        self.finished = folder / "finished" / f"{os.getpid()}.jsonl"
        self.own = str(folder) + os.sep
        self.shared_key = None
        self.shared_files = {}
        self.collecting = None
        self.loose = set()
        self.parsed = {}
        self.closures = {}

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

    def importing(self, frame: FrameType | None) -> str | None:
        """The repository module, other than the one being collected, whose top level code runs innermost."""
        while frame is not None:
            name = frame.f_code.co_filename
            if frame.f_code.co_name == "<module>" and name.startswith(self.root):
                if name == self.collecting:
                    return None
                if not name[len(self.root) :].startswith(SKIPPED):
                    return name[len(self.root) :]
            frame = frame.f_back
        return None

    def audit(self, event: str, args: tuple) -> None:
        test = self.current
        if event == "open":
            path, mode = args[0], args[1]
            if isinstance(path, os.PathLike):
                path = os.fspath(path)
            if isinstance(path, (str, bytes)) and (mode is None or not set(str(mode)) & set("wax")):
                try:
                    opened = os.path.abspath(os.fsdecode(path))
                except OSError:
                    return
                if opened.startswith(self.own):
                    return
                if opened.endswith(".py") and (test is self.shared or loading(sys._getframe(1))):
                    return
                owner = None
                if self.collecting and opened.startswith(self.root) and not opened.endswith((".py", ".pyc")):
                    owner = self.importing(sys._getframe(1))
                if owner is not None:
                    self.collections.setdefault(owner, [set(), False])[0].add(opened)
                    return
                test[0].add(opened)
                if self.collecting:
                    self.loose.add(opened)
        elif event in ESCAPES and test is not self.shared:
            test[1] = True
            self.escaping.update(self.setting_up)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_make_collect_report(self, collector: pytest.Collector) -> Iterator[None]:
        if not isinstance(collector, pytest.Module):
            yield
            return
        previous = self.current
        collected = self.collections.setdefault(collector.nodeid, [set(), False])
        imported = set(sys.modules)
        self.current = collected
        self.collecting, self.loose = str(collector.path), set()
        try:
            yield
        finally:
            previous[0].update(
                path
                for path in self.loose
                if not path.startswith(self.root + ".github/workflows/") and not path.endswith(".py")
            )
            self.collecting = None
            self.current = previous
            for name in set(sys.modules) - imported:
                filename = getattr(sys.modules[name], "__file__", "") or ""
                if filename.startswith(self.root) and filename.endswith(".py"):
                    found = self.collections.setdefault(filename[len(self.root) :], [set(), False])
                    found[1] = found[1] or collected[1]

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        imports = {}
        for path in self.collections:
            imports[path], dynamic = self.module_imports(path)
            self.collections[path][1] = self.collections[path][1] or dynamic
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

    def module_imports(self, path: str) -> tuple[set[str], bool]:
        """Repository files a module's imports and parent packages load now, and whether it imports by name."""
        if path not in self.parsed:
            from scripts.ci_mutation.scope import imported_names

            text = (Path(self.root) / path).read_text()
            names = imported_names(text, Path(path).parent.parts)
            dynamic = "__import__" in text or any(
                name == "importlib" or name.startswith("importlib.") for name in names
            )
            parts = [name.split(".") for name in names]
            self.parsed[path] = {".".join(part[:end]) for part in parts for end in range(1, len(part) + 1)}, dynamic
        names, dynamic = self.parsed[path]
        filenames = {getattr(sys.modules.get(name), "__file__", "") or "" for name in names}
        found = {name[len(self.root) :] for name in filenames if name.startswith(self.root)}
        return {name for name in found if not name.startswith(SKIPPED)}, dynamic

    def closure(self, path: str) -> tuple[set[str], bool]:
        """The collection reads of a module and of every repository module it reaches through the modules loaded now."""
        loaded = len(sys.modules), next(reversed(sys.modules), None)
        cached = self.closures.get(path)
        if cached and cached[0] == loaded:
            return cached[1]
        reads, escapes, pending, seen = set(), False, [path], {path}
        while pending:
            current = pending.pop()
            found = self.collections.get(current, [set(), False])
            reads.update(found[0])
            names, dynamic = self.module_imports(current) if (Path(self.root) / current).is_file() else (set(), False)
            escapes = escapes or found[1] or dynamic
            pending.extend(names - seen)
            seen.update(names)
        self.closures[path] = loaded, (reads, escapes)
        return reads, escapes

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        self.setting_up.append(fixturedef.argname)
        try:
            yield
        finally:
            self.setting_up.pop()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
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
            test, self.current = self.current, self.shared
            reads, escapes = self.closure(item.nodeid.split("::", 1)[0])
            test[0].update(reads)
            test[1] = test[1] or escapes
            self.finish(item.nodeid)

    def kept(self, opened: set[str]) -> dict[str, str]:
        kept = {}
        for full in opened:
            if not full.startswith(self.root):
                continue
            relative = full[len(self.root) :]
            if relative.endswith(".pyc") or relative.startswith(SKIPPED) or "__pycache__" in relative:
                continue
            if not Path(full).is_relative_to(self.folder):
                kept[full] = relative
        return kept

    def relative(self, opened: set[str]) -> list[str]:
        return sorted({relative for full, relative in self.kept(opened).items() if os.path.isfile(full)})

    def line(self, test: str, shared: set[str]) -> str:
        opened, escapes = self.tests[test]
        escapes = escapes or bool(self.fixtures[test] & self.escaping)
        return json.dumps({"test": test, "files": sorted(shared.union(self.relative(opened))), "escapes": escapes})

    def shared_relative(self) -> set[str]:
        log = self.children / f"{os.getpid()}-shared"
        try:
            size = log.stat().st_size
        except FileNotFoundError:
            size = 0
        if (len(self.shared[0]), size) != self.shared_key:
            self.shared_key = (len(self.shared[0]), size)
            self.shared_files = self.kept(self.shared[0] | self.child_reads(log))
        # A shared path read before its file existed counts once the file appears, as it does at session end.
        return {relative for full, relative in self.shared_files.items() if os.path.isfile(full)}

    def finish(self, test: str) -> None:
        self.tests[test][0].update(self.child_reads(self.logs[test]))
        self.finished.parent.mkdir(parents=True, exist_ok=True)
        with self.finished.open("a") as finished:
            finished.write(self.line(test, self.shared_relative()) + "\n")

    def pytest_sessionfinish(self, session):
        if not self.tests:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        shared = set(self.relative(self.shared[0] | self.child_reads(self.children / f"{os.getpid()}-shared")))
        for test, log in self.logs.items():
            self.tests[test][0].update(self.child_reads(log))
        lines = [self.line(test, shared) for test in self.tests]
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
