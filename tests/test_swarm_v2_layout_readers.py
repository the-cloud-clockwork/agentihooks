import ast
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_v2 import filesystem, image_probe, supervision, supervision_runtime, worker_health, worker_home
from scripts.swarm_v2.supervision_protocol import write

READERS = (supervision, supervision_runtime, worker_health, image_probe, worker_home)
MOVED = {
    "layout_version": 1,
    "roots": {
        "home": "h",
        "runtime": "r",
        "checkout": "c",
        "worktree": "w",
        "spool": "q",
        "scratch": "s",
        "seed": "p",
    },
    "immutable": ["seed"],
}
AUTHORITY = {
    "execution_id": "exe-" + "a" * 32,
    "generation": 1,
    "task_id": "fixture",
    "seat_id": "eng-1",
    "swarm_id": "fixture",
    "grant_id": "lgr-" + "b" * 32,
}


ACCESSORS = {"get", "pop", "setdefault"}
PATH_CALLS = {
    "Path",
    "PurePath",
    "join",
    "joinpath",
    "copytree",
    "copy",
    "copy2",
    "copyfile",
    "move",
    "rmtree",
    "mkdir",
    "makedirs",
    "mkdtemp",
    "mkstemp",
    "glob",
    "rglob",
    "iglob",
    "open",
    "chdir",
    "listdir",
    "scandir",
    "walk",
    "exists",
    "isdir",
    "is_dir",
    "relative_to",
}
PATH_KEYWORDS = {"dir", "cwd", "path", "root"}
WRAPPERS = {"sorted", "list", "tuple", "set", "reversed", "enumerate"}


def named_folders(source: str, folders: set[str]) -> list[str]:
    tree = ast.parse(source)
    bound = folder_bindings(tree, folders)
    found = set()
    for node in ast.walk(tree):
        for part, kind in path_parts(node):
            if isinstance(part, ast.Constant):
                literal = isinstance(node, (ast.JoinedStr, ast.Constant))
                if is_folder(part, folders) and ("/" in part.value or not literal):
                    found.add(f"{part.lineno}: {part.value}")
            elif is_bound(part, kind, bound):
                found.add(f"{part.lineno}: {ast.unparse(part)}")
    return sorted(found)


def path_parts(node: ast.AST) -> list[tuple[ast.AST, str]]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [(node.left, "name"), (node.right, "name")]
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        return [(head.value if isinstance(head, ast.FormattedValue) else head, "name")]
    if isinstance(node, ast.Tuple) and isinstance(node.ctx, ast.Load):
        return [(element, "name") for element in node.elts]
    if isinstance(node, (ast.For, ast.comprehension)):
        if isinstance(node.iter, (ast.List, ast.Set)):
            return [(element, "name") for element in node.iter.elts]
        return [(unwrapped(node.iter), "sequence")]
    if isinstance(node, ast.Call):
        return call_parts(node)
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and "/" in node.value:
        return [(node, "name")]
    return []


def call_parts(node: ast.Call) -> list[tuple[ast.AST, str]]:
    callee = ast.unparse(node.func).rpartition(".")[2]
    args = node.args[1:] if callee in ACCESSORS else node.args if callee in PATH_CALLS else []
    args = args + [keyword.value for keyword in node.keywords if keyword.arg in PATH_KEYWORDS]
    return [(unwrapped(arg.value), "sequence") if isinstance(arg, ast.Starred) else (arg, "name") for arg in args]


def unwrapped(node: ast.AST) -> ast.AST:
    while isinstance(node, ast.Call) and ast.unparse(node.func) in WRAPPERS and node.args:
        node = node.args[0]
    return node


def folder_bindings(tree: ast.AST, folders: set[str]) -> dict[str, set[str]]:
    pairs = list(bindings(tree))
    bound = {"name": set(), "sequence": set(), "mapping": set()}
    while True:
        before = sum(map(len, bound.values()))
        for target, value in pairs:
            kind = folder_kind(value, folders, bound)
            if kind:
                bound[kind].add(target)
        if sum(map(len, bound.values())) == before:
            return bound


def bindings(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield from unpacked(target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value:
            yield ast.unparse(node.target), node.value
        elif isinstance(node, ast.arguments):
            positional = node.posonlyargs + node.args
            defaults = zip(positional[len(positional) - len(node.defaults) :], node.defaults)
            for arg, default in [*defaults, *zip(node.kwonlyargs, node.kw_defaults)]:
                if default:
                    yield arg.arg, default


def unpacked(target: ast.AST, value: ast.AST):
    sequences = (ast.Tuple, ast.List)
    if isinstance(target, sequences) and isinstance(value, sequences) and len(target.elts) == len(value.elts):
        for element, item in zip(target.elts, value.elts):
            yield from unpacked(element, item)
    else:
        yield ast.unparse(target), value


def folder_kind(value: ast.AST, folders: set[str], bound: dict[str, set[str]]) -> str | None:
    if isinstance(value, ast.IfExp):
        return folder_kind(value.body, folders, bound) or folder_kind(value.orelse, folders, bound)
    if isinstance(value, ast.BoolOp):
        return next(filter(None, (folder_kind(item, folders, bound) for item in value.values)), None)
    if is_folder(value, folders):
        return "name"
    if isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)):
        return next((kind for kind in bound if is_bound(value, kind, bound)), None)
    items = value.elts if isinstance(value, (ast.List, ast.Tuple, ast.Set)) else []
    if any(folder_kind(item, folders, bound) == "name" for item in items):
        return "sequence"
    entries = value.values if isinstance(value, ast.Dict) else []
    if any(folder_kind(entry, folders, bound) == "name" for entry in entries):
        return "mapping"
    return None


def is_bound(part: ast.AST, kind: str, bound: dict[str, set[str]]) -> bool:
    if isinstance(part, ast.Subscript) and kind == "name" and ast.unparse(part.value) in bound["mapping"]:
        return True
    return isinstance(part, (ast.Name, ast.Attribute, ast.Subscript)) and ast.unparse(part) in bound[kind]


def is_folder(node: ast.AST | None, folders: set[str]) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.partition("/")[0] in folders


def test_the_guard_catches_a_reader_naming_a_layout_folder():
    folders = {"homes", "run", "tmp"}
    source = (
        'a / "homes" / t\n'
        'b = f"run/{x}"\n'
        'c = a / "tmp/x"\n'
        'd = ["pane", "run"]\n'
        'e = a / "work"\n'
        'f = f"homes{x}"\n'
        'for name in ("run", "tmp"): pass\n'
        'h = Path(a, "tmp")\n'
        'i = os.path.join(a, "homes/x")\n'
        'j = record.get("homes")\n'
        'k = a.joinpath("run", "x")\n'
        'for n in ["run", "tmp"]: pass\n'
        'm = r.glob("homes/*")\n'
    )
    assert named_folders(source, folders) == [
        "11: run",
        "12: run",
        "12: tmp",
        "13: homes/*",
        "1: homes",
        "2: run/",
        "3: tmp/x",
        "7: run",
        "7: tmp",
        "8: tmp",
        "9: homes/x",
    ]


def test_the_guard_catches_a_folder_name_passed_through_a_variable():
    folders = {"homes", "run", "tmp"}
    source = (
        'name = "tmp"\n'
        "a = root / name\n"
        'FOLDERS = ["run", "tmp"]\n'
        "for folder in FOLDERS: pass\n"
        'copytree(src, dst, "homes")\n'
        'def f(base="run"): return Path(base)\n'
        'self.where = "homes"\n'
        "g = os.path.join(root, self.where)\n"
        'h = f"{name}/x"\n'
        "i = [d for d in FOLDERS]\n"
        'cmd = ["pane", "run"]\n'
        "herdr(cmd)\n"
        'key = "homes"\n'
        "record.get(key)\n"
        "record[key]\n"
        'sub = "run"\n'
        'herdr(["pane", sub])\n'
        'mkdtemp(dir="tmp")\n'
    )
    assert named_folders(source, folders) == [
        "10: FOLDERS",
        "18: tmp",
        "2: name",
        "4: FOLDERS",
        "5: homes",
        "6: base",
        "8: self.where",
        "9: name",
    ]


def test_the_guard_follows_aliases_wrappers_and_fallbacks_but_not_keys_or_subcommands():
    folders = {"homes", "run", "tmp"}
    source = (
        'x = "tmp"\n'
        "y = x\n"
        "a = root / y\n"
        'p, q = ["homes", "run"]\n'
        "b = root / p\n"
        'DEFAULT = "tmp"\n'
        "def f(root, sub=DEFAULT): return root / sub\n"
        'FOLDERS = ["run", "tmp"]\n'
        "for d in sorted(FOLDERS): pass\n"
        "c = os.path.join(root, *FOLDERS)\n"
        'e = root / os.environ.get("X", "tmp")\n'
        'g = "homes" if flag else "work"\n'
        "h = root / g\n"
        'i = os.environ.get("X") or "run"\n'
        "j = Path(i)\n"
        'ROOTS = {"runtime": "run"}\n'
        'k = root / ROOTS["runtime"]\n'
        'cfg["f"] = "tmp"\n'
        'm = root / cfg["f"]\n'
        'key = "homes"\n'
        "load(record, key)\n"
        'verb = "run"\n'
        "herdr(verb)\n"
        'herdr("run")\n'
        'n = record.get("homes", {})\n'
    )
    assert named_folders(source, folders) == [
        "10: FOLDERS",
        "11: tmp",
        "13: g",
        "15: i",
        "17: ROOTS['runtime']",
        "19: cfg['f']",
        "3: y",
        "5: p",
        "7: sub",
        "9: FOLDERS",
    ]


@pytest.mark.parametrize("module", READERS, ids=lambda module: module.__name__)
def test_no_reader_names_a_layout_folder(module):
    folders = set(filesystem.load().roots.values())
    assert named_folders(Path(module.__file__).read_text(), folders) == []


def test_recorded_layout_wins_over_the_layout_file(tmp_path):
    execution = filesystem.recorded(tmp_path, {"layout": MOVED})
    assert execution.root == tmp_path
    assert execution.path("home") == tmp_path / "h"
    assert filesystem.recorded(tmp_path, {}).layout == filesystem.load()


def moved_attempt(tmp_path: Path, harness: str = "codex") -> Path:
    attempt = tmp_path / "attempt"
    for folder in ("r", "s"):
        (attempt / folder).mkdir(parents=True)
    (attempt / "h" / harness / f".{harness}").mkdir(parents=True)
    record = {"attempt": AUTHORITY["execution_id"], "homes": {harness: f"h/{harness}"}, "layout": MOVED}
    (attempt / "execution.json").write_text(json.dumps(record))
    return attempt


def launch_spec(tmp_path: Path, attempt: Path) -> Path:
    (attempt / "registration.json").write_text(json.dumps(AUTHORITY))
    spec = {"schema_version": 1, "authority": AUTHORITY, "harness": "codex", "agent": ["true"], "exporter": ["true"]}
    path = tmp_path / "launch.json"
    path.write_text(json.dumps(spec))
    return path


def test_supervisor_takes_its_home_runtime_and_environment_from_the_layout(tmp_path):
    attempt = moved_attempt(tmp_path)
    launch = supervision.Launch.load(attempt, launch_spec(tmp_path, attempt))
    assert launch.attempt == attempt.resolve()
    assert launch.home == attempt.resolve() / "h" / "codex"
    owner = supervision_runtime.Supervisor(launch)
    assert owner.root.parent == attempt.resolve() / "r" / "supervision"
    expected = filesystem.environment(filesystem.Execution(attempt.resolve(), filesystem.parse(MOVED)), "codex")
    assert {key: owner.environment[key] for key in expected} == expected
    assert owner.environment["HERDR_CONFIG_PATH"] == str(owner.root / "herdr.toml")
    assert owner.environment["SWARM_SUPERVISION_DIR"] == str(owner.root)


def test_supervisor_refuses_a_home_outside_the_layout_home_for_its_harness(tmp_path):
    attempt = moved_attempt(tmp_path)
    (attempt / "h" / "other").mkdir()
    record = json.loads((attempt / "execution.json").read_text())
    (attempt / "execution.json").write_text(json.dumps(record | {"homes": {"codex": "h/other"}}))
    with pytest.raises(supervision.LaunchRefused, match="invalid private home"):
        supervision.Launch.load(attempt, launch_spec(tmp_path, attempt))


def test_health_probe_reads_the_home_runtime_and_scratch_the_layout_names(tmp_path, monkeypatch):
    attempt = moved_attempt(tmp_path)
    execution = filesystem.recorded(attempt, json.loads((attempt / "execution.json").read_text()))
    assert worker_health.private_home(execution, "codex") == (attempt / "h" / "codex").resolve()
    assert worker_health.runtime_failure(execution) is None
    (attempt / "s").chmod(0o500)
    try:
        assert worker_health.runtime_failure(execution) == "runtime_path_unwritable:s"
    finally:
        (attempt / "s").chmod(0o700)
    root = attempt / "r" / "supervision" / "incarnation"
    root.mkdir(parents=True)
    scope = {"supervisor_pid": os.getpid(), "process_namespace": os.readlink("/proc/self/ns/pid")}
    write(root / "context.json", scope)
    assert worker_health.incarnation(execution) == (root, scope)
    seen = {}

    def run(command, env, **_):
        seen.update(env)
        return SimpleNamespace(stdout=b"{}")

    monkeypatch.setattr(worker_health.subprocess, "run", run)
    probe = worker_health.Probe(attempt, "codex", {"PATH": "/bin"})
    assert worker_health.herdr_failure(probe, execution, attempt / "h" / "codex", root) is None
    assert seen["XDG_RUNTIME_DIR"] == str(attempt / "r")
    assert seen["TMPDIR"] == str(attempt / "s")
    assert seen["HOME"] == str(attempt / "h" / "codex")
    assert seen["HERDR_CONFIG_PATH"] == str(root / "herdr.toml")


def test_health_probe_report_follows_the_recorded_layout(tmp_path):
    attempt = moved_attempt(tmp_path)
    checks = worker_health.evaluate(worker_health.Probe(attempt, "codex", {"PATH": ""}), "startup")["checks"]
    assert checks["home"] is None
    assert checks["runtime_paths"] is None


def test_health_probe_resolves_a_linked_attempt_like_the_supervisor(tmp_path, monkeypatch):
    attempt = moved_attempt(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(attempt)
    seen = []
    monkeypatch.setattr(
        worker_health, "herdr_failure", lambda probe, execution, home, root: seen.append(execution.root)
    )
    worker_health.evaluate(worker_health.Probe(linked, "codex", {"PATH": ""}), "startup")
    assert seen == [attempt.resolve()]


def test_image_probe_launches_each_harness_in_the_layout_home(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        image_probe,
        "run",
        lambda command, environ, timeout, cwd=None: calls.append(environ) or SimpleNamespace(stdout=""),
    )
    execution = filesystem.Execution(tmp_path, filesystem.parse(MOVED))
    image_probe.harness("claude", execution, {"PATH": "/bin"})
    assert calls[1] == {"PATH": "/bin"} | filesystem.environment(execution, "claude")
    assert calls[1]["HOME"] == str(tmp_path / "h" / "claude")
    assert calls[1]["XDG_RUNTIME_DIR"] == str(tmp_path / "r")


def test_image_probe_runs_herdr_in_a_layout_execution(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(image_probe, "server_status", lambda environ: {})
    monkeypatch.setattr(image_probe, "run", lambda *args, **kwargs: SimpleNamespace(stdout=""))

    class Server:
        def __init__(self, command, env, **_):
            started.append(env)

        def terminate(self):
            pass

        def wait(self, timeout):
            pass

    monkeypatch.setattr(image_probe.subprocess, "Popen", Server)
    execution = filesystem.allocate(tmp_path, "herdr", filesystem.parse(MOVED))
    image_probe.herdr(execution, {"PATH": "/bin"})
    root = tmp_path / "herdr"
    assert started == [
        {"PATH": "/bin"} | filesystem.environment(execution, "herdr") | {"HERDR_CONFIG_PATH": str(root / "herdr.toml")}
    ]
    assert (root / "h" / "herdr").is_dir()


def test_image_probe_observes_the_recorded_attempt(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(image_probe, "bootstrap", lambda request: {"layout": MOVED})
    monkeypatch.setattr(image_probe, "herdr", lambda execution, environ: seen.append(execution.root) or {})
    monkeypatch.setattr(image_probe, "harness", lambda name, execution, environ: seen.append(execution.path("home")))
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(image_probe, "MANIFEST", manifest)
    root = tmp_path / "attempts"
    image_probe.probe(tmp_path, root, {})
    assert seen == [root / "herdr", root / "image-probe" / "h", root / "image-probe" / "h"]


def test_worker_home_render_runs_in_the_layout_home_and_logs_to_its_runtime(tmp_path, monkeypatch):
    execution = filesystem.allocate(tmp_path, "attempt", filesystem.parse(MOVED))
    (execution.path("home") / "claude").mkdir()
    (execution.root / worker_home.PENDING).write_text(json.dumps({"request": {"interpreter": "/bin/python"}}))
    seen = {}

    def run(command, cwd, env, **_):
        seen.update(cwd=cwd, home=env["HOME"])
        return subprocess.CompletedProcess(command, 0, stdout="rendered\n")

    monkeypatch.setattr(worker_home.subprocess, "run", run)
    worker_home.render(execution, "claude")
    assert seen == {"cwd": tmp_path / "attempt" / "h" / "claude", "home": str(tmp_path / "attempt" / "h" / "claude")}
    assert (tmp_path / "attempt" / "r" / "render-claude.log").read_text() == "rendered\n"


def test_worker_home_record_names_homes_under_the_layout_home():
    request = SimpleNamespace(
        attempt="a1",
        profiles={"claude": "x", "codex": "y"},
        accounts={},
        endpoints={},
        interpreter=Path("/bin/python"),
    )
    record = worker_home._record(request, "d", {}, 1.0, filesystem.parse(MOVED))
    assert record["homes"] == {"claude": "h/claude", "codex": "h/codex"}
    assert record["layout"] == filesystem.mapping(filesystem.parse(MOVED))
