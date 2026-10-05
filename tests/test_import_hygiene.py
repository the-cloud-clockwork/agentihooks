"""Stdout discipline test (P0.3).

Importing any hooks/* module must NOT write to stdout. Claude Code parses
hook-process stdout as JSON; any module-level print/import-time warning
silently corrupts the parse and drops `additionalContext`.

Per upstream protocol (https://code.claude.com/docs/en/hooks.md):
- exit 0 + valid JSON → fields applied
- exit 0 + invalid JSON → harness logs error, fields NOT applied
- exit 1/non-2 → non-blocking, stderr in transcript

A noisy import means our broadcast injection silently disappears. This
test catches it at CI time before it ships.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"

# Modules that legitimately print on import are quarantined here. Empty by
# design — every entry is a future bug. Add ONLY with a justifying comment.
_ALLOWED_NOISY: frozenset[str] = frozenset()

# One interpreter loads the third-party dependencies once, then forks a child
# per module. Every child starts with none of the package loaded, so each
# module's import side effects are observed alone, as in a fresh process.
# The dependencies are read from the import statements that run at import
# time, so no first-party module executes outside its own child.
_PROBE_RUNNER = r"""
import ast, importlib, importlib.machinery, io, json, os, sys, tempfile, traceback

def import_quietly(name):
    real = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        importlib.import_module(name)
        failure = ""
    except BaseException:
        failure = traceback.format_exc()
    out, err = sys.stdout.getvalue(), sys.stderr.getvalue()
    sys.stdout, sys.stderr = real
    return out, err + failure, bool(failure)

def first_party(name, package):
    return name == package or name.startswith(package + ".")

def find_source(name):
    path, spec = None, None
    parts = name.split(".")
    for end in range(1, len(parts) + 1):
        spec = importlib.machinery.PathFinder.find_spec(".".join(parts[:end]), path)
        if spec is None or (end < len(parts) and spec.submodule_search_locations is None):
            return None, False
        path = spec.submodule_search_locations
    return spec.origin, path is not None

def imports_run_on_import(tree):
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        pending.extend(ast.iter_child_nodes(node))

def imported_names(node, module, is_package):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        anchor = module.split(".")[: None if is_package else -1]
        anchor = anchor[: len(anchor) - node.level + 1]
        base = ".".join([*anchor, *filter(None, [node.module])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")]

def dependencies(modules, package):
    seen, found, pending = set(), [], list(modules)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        parts = name.split(".")
        pending.extend(".".join(parts[:end]) for end in range(1, len(parts)))
        origin, is_package = find_source(name)
        if not origin or not origin.endswith(".py"):
            continue
        try:
            with open(origin, "rb") as source:
                tree = ast.parse(source.read())
        except (OSError, SyntaxError, ValueError):
            continue
        for node in imports_run_on_import(tree):
            for target in imported_names(node, name, is_package):
                (pending if first_party(target, package) else found).append(target)
    return list(dict.fromkeys(found))

def run_child(name):
    with tempfile.TemporaryFile() as raw:
        os.dup2(raw.fileno(), 1)
        out, err, failed = import_quietly(name)
        raw.seek(0)
        fd_out = raw.read().decode(errors="replace")
    return {"stdout": fd_out + out, "stderr": err, "failed": failed, "pid": os.getpid(), "ppid": os.getppid()}

def fork_child(name):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        with os.fdopen(write_fd, "wb") as pipe:
            pipe.write(json.dumps(run_child(name)).encode())
        os._exit(0)
    os.close(write_fd)
    return pid, read_fd

def collect_child(pid, read_fd):
    with os.fdopen(read_fd, "rb") as pipe:
        payload = pipe.read()
    _, status = os.waitpid(pid, 0)
    if not payload:
        return {"stdout": "", "stderr": f"probe child died (wait status {status})", "failed": True, "pid": pid, "ppid": os.getpid()}
    return json.loads(payload)

output, package, *modules = sys.argv[1:]
shared_out = []
for name in dependencies(modules, package):
    if name not in sys.modules:
        out, _, _ = import_quietly(name)
        shared_out.append(out)
children = {name: fork_child(name) for name in modules}
report = {"shared_stdout": "".join(shared_out), "modules": {name: collect_child(*child) for name, child in children.items()}}
with open(output, "w") as handle:
    json.dump(report, handle)
"""


def _probe_modules(package: str, modules: list[str], env: dict[str, str]) -> dict:
    """Import each module in its own forked child of one interpreter.

    Returns ``shared_stdout`` (stdout written while the dependencies loaded),
    ``process_stdout`` (anything that escaped every capture) and ``modules``,
    each with ``stdout``, ``stderr``, ``failed``, ``pid`` and ``ppid``.
    """
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch) / "report.json"
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_RUNNER, str(output), package, *modules],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        if proc.returncode != 0 or not output.exists():
            pytest.fail(f"import probe crashed (exit {proc.returncode})\nstderr: {proc.stderr}\nstdout: {proc.stdout}")
        report = json.loads(output.read_text())
    report["process_stdout"] = proc.stdout
    return report


def _all_hook_modules() -> list[str]:
    """Return dotted module names for every .py under hooks/, excluding tests
    and __pycache__. Skips files with leading underscore at the package root
    so __main__ does not execute the CLI.
    """
    modules: list[str] = []
    for path in HOOKS_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "__main__.py":
            continue
        rel = path.relative_to(REPO_ROOT)
        dotted = ".".join(rel.with_suffix("").parts)
        modules.append(dotted)
    return sorted(modules)


def _selected_hook_modules(items) -> tuple[str, ...]:
    return tuple(
        sorted(
            item.callspec.params["module"]
            for item in items
            if item.originalname == test_module_import_does_not_write_to_stdout.__name__
        )
    )


@functools.cache
def _hook_report(modules: tuple[str, ...]) -> dict:
    # The suite-wide home-isolation fixture repoints $HOME. Where the
    # interpreter installs into the user site (~/.local/lib/pythonX/
    # site-packages — the layout on the self-hosted runner), that alone drops
    # every dependency from the child's sys.path and the probe reports an
    # import failure that says more about the fixture than the module. Hand
    # the child the parent's resolved path so it imports exactly what pytest
    # imported.
    return _probe_modules("hooks", list(modules), _probe_env())


@pytest.mark.xdist_group("import-probe")
@pytest.mark.parametrize("module", _all_hook_modules())
def test_module_import_does_not_write_to_stdout(module: str, request: pytest.FixtureRequest) -> None:
    if module in _ALLOWED_NOISY:
        pytest.skip(f"{module} is in _ALLOWED_NOISY quarantine")

    # Probe only what this worker's shard selected, so splitting the suite
    # splits the probe too.
    report = _hook_report(_selected_hook_modules(request.session.items))
    noise = report["shared_stdout"] + report["process_stdout"]
    if noise:
        pytest.fail(
            f"a dependency of hooks writes to stdout on import — would corrupt hook JSON.\ncaptured stdout:\n{noise}"
        )
    result = report["modules"][module]
    if result["failed"]:
        pytest.fail(f"{module}: import failed\nstderr: {result['stderr']}\nstdout: {result['stdout']}")
    if result["stdout"]:
        pytest.fail(
            f"{module}: writes to stdout on import — would corrupt hook JSON.\ncaptured stdout:\n{result['stdout']}"
        )


def _write_package(root: Path, name: str, modules: dict[str, str]) -> None:
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    for module, source in modules.items():
        (package / f"{module}.py").write_text(source)


def _probe_env(*extra_paths: Path, **overrides: str) -> dict[str, str]:
    return {
        **os.environ,
        "CLAUDE_HOOK_LOG_ENABLED": "false",
        "PYTHONPATH": os.pathsep.join([*map(str, extra_paths), *(p for p in sys.path if p)]),
        **overrides,
    }


class TestProbeHarness:
    """The probe itself is load-bearing: when it misreports, every module in
    this file becomes undiagnosable. These guard the ways it has failed.
    """

    def test_noise_is_reported_against_every_module_that_loads_it(self, tmp_path: Path) -> None:
        """Each module imports in a clean child, so a shared first-party module's
        noise lands on every module that pulls it in, not only on the first.
        """
        _write_package(
            tmp_path,
            "noisypkg",
            {
                "shared": "print('loaded')\n",
                "first": "from noisypkg import shared\n",
                "second": "from noisypkg import shared\n",
                "raw": "import os\nos.write(1, b'raw\\n')\n",
                "quiet": "",
            },
        )
        modules = ["noisypkg.first", "noisypkg.second", "noisypkg.raw", "noisypkg.quiet"]
        report = _probe_modules("noisypkg", modules, _probe_env(tmp_path))
        stdout = {name: result["stdout"] for name, result in report["modules"].items()}
        assert stdout == {
            "noisypkg.first": "loaded\n",
            "noisypkg.second": "loaded\n",
            "noisypkg.raw": "raw\n",
            "noisypkg.quiet": "",
        }

    def test_dependency_noise_is_reported_as_shared(self, tmp_path: Path) -> None:
        _write_package(tmp_path, "noisydep", {"core": "print('dependency')\n"})
        _write_package(tmp_path, "quietpkg", {"user": "import noisydep.core\n"})
        report = _probe_modules(
            "quietpkg",
            ["quietpkg.user"],
            _probe_env(tmp_path),
        )
        assert report["shared_stdout"] == "dependency\n"

    def test_import_failure_reports_the_exception(self) -> None:
        """A failing import must surface its traceback.

        Redirecting stderr to a StringIO and letting the exception escape sends
        the traceback into the buffer and drops it — the report then says only
        that something broke.
        """
        report = _probe_modules("hooks", ["hooks._definitely_not_a_real_module"], _probe_env())
        result = report["modules"]["hooks._definitely_not_a_real_module"]
        assert result["failed"]
        assert "ModuleNotFoundError" in result["stderr"], (
            f"probe swallowed the traceback — stderr was {result['stderr']!r}"
        )

    def test_probe_resolves_imports_independently_of_home(self, tmp_path: Path) -> None:
        """Dependencies must stay importable when $HOME is repointed.

        The suite-wide isolation fixture rewrites $HOME. Where the interpreter
        installs into the user site — the self-hosted runner's layout — that
        alone strips every dependency from the child's path, and every module
        importing one fails for a reason that has nothing to do with the module.
        """
        _write_package(tmp_path, "homepkg", {"light": "import tomlkit\n"})
        report = _probe_modules("homepkg", ["homepkg.light"], _probe_env(tmp_path, HOME=str(tmp_path / "elsewhere")))
        result = report["modules"]["homepkg.light"]
        assert not result["failed"], f"import broke under a rewritten $HOME:\n{result['stderr']}"

    def test_every_module_is_probed_from_one_interpreter(self, tmp_path: Path) -> None:
        _write_package(tmp_path, "pairpkg", {"first": "", "second": ""})
        report = _probe_modules(
            "pairpkg",
            ["pairpkg.first", "pairpkg.second"],
            _probe_env(tmp_path),
        )
        results = list(report["modules"].values())
        assert len({result["ppid"] for result in results}) == 1
        assert len({result["pid"] for result in results}) == 2

    def test_module_children_import_side_by_side(self, tmp_path: Path) -> None:
        flag = tmp_path / "sibling.flag"
        waiter = (
            "import os, time\n"
            "deadline = time.monotonic() + 2\n"
            f"while not os.path.exists({str(flag)!r}):\n"
            "    if time.monotonic() > deadline:\n"
            "        raise TimeoutError('sibling child never ran alongside')\n"
            "    time.sleep(0.01)\n"
        )
        _write_package(tmp_path, "sidepkg", {"waiter": waiter, "signal": f"open({str(flag)!r}, 'w').close()\n"})
        report = _probe_modules("sidepkg", ["sidepkg.waiter", "sidepkg.signal"], _probe_env(tmp_path))
        waited = report["modules"]["sidepkg.waiter"]
        assert not waited["failed"], waited["stderr"]

    def test_modules_and_dependencies_execute_once_and_dependencies_load_before_the_fork(self, tmp_path: Path) -> None:
        log = tmp_path / "imports.log"
        record = (
            f"import os\nwith open({str(log)!r}, 'a') as _log:\n    _log.write(f'{{__name__}} {{os.getpid()}}\\n')\n"
        )
        _write_package(tmp_path, "countdep", {"core": record})
        _write_package(tmp_path, "stray", {"core": record})
        (tmp_path / "stray" / "__init__.py").write_text("from stray import core\n")
        _write_package(
            tmp_path,
            "countpkg",
            {
                "first": record + "from . import helper\nfrom countpkg.helper import stray\n",
                "helper": record + "from countdep import core\nstray = None\n",
                "second": record,
            },
        )
        report = _probe_modules("countpkg", ["countpkg.first", "countpkg.second"], _probe_env(tmp_path))
        executed = [line.split() for line in log.read_text().splitlines()]
        assert sorted(name for name, _ in executed) == [
            "countdep.core",
            "countpkg.first",
            "countpkg.helper",
            "countpkg.second",
        ]
        assert dict(executed)["countdep.core"] == str(report["modules"]["countpkg.first"]["ppid"])

    def test_every_module_probe_shares_one_xdist_group(self) -> None:
        marks = getattr(test_module_import_does_not_write_to_stdout, "pytestmark", [])
        groups = [mark.args for mark in marks if mark.name == "xdist_group"]
        assert len(groups) == 1

    def test_a_worker_probes_only_the_modules_its_session_selected(self) -> None:
        def item(name: str, **params: str) -> SimpleNamespace:
            return SimpleNamespace(originalname=name, callspec=SimpleNamespace(params=params))

        items = [
            item("test_module_import_does_not_write_to_stdout", module="hooks.config"),
            item("test_something_else", module="hooks.mcp"),
            SimpleNamespace(originalname="test_without_params"),
            item("test_module_import_does_not_write_to_stdout", module="hooks._redis"),
        ]
        assert _selected_hook_modules(items) == ("hooks._redis", "hooks.config")
