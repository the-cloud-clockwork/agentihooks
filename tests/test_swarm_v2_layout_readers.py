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


def named_folders(source: str, folders: set[str]) -> list[str]:
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = [node.right]
        elif isinstance(node, ast.JoinedStr) and node.values:
            parts = [node.values[0]]
        elif isinstance(node, ast.Tuple):
            parts = node.elts
        elif isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.iter, (ast.List, ast.Set)):
            parts = node.iter.elts
        elif isinstance(node, ast.Call) and ast.unparse(node.func).rpartition(".")[2] in ("Path", "join", "joinpath"):
            parts = node.args
        elif isinstance(node, ast.Constant):
            parts = [node] if isinstance(node.value, str) and "/" in node.value else []
        else:
            continue
        for part in parts:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                head, slash, _ = part.value.partition("/")
                if head in folders and (slash or not isinstance(node, (ast.JoinedStr, ast.Constant))):
                    found.add(f"{part.lineno}: {part.value}")
    return sorted(found)


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
