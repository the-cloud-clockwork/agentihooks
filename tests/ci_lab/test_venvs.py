import fcntl
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import sandbox, uv_cache, venvs

DIGEST = "a" * 64
OTHER = "b" * 64
LOCK = ".github/test-requirements.lock"
EXCLUDES = ".github/test-excludes.txt"
MISSES = """  × No solution found when resolving dependencies:
  ╰─▶ Because uncached-dependency was not found in the
      cache and offline-fixture depends on uncached-dependency==1.0, and
      uncached-dependency was not found in the cache, we can conclude that
      your requirements are unsatisfiable.
error: Network connectivity is disabled, but the requested data wasn't found in the cache for: `http://index/files/partial-1.0-py3-none-any.whl`"""
UV = """
import json, os, pathlib, subprocess, sys, time
config = json.loads(pathlib.Path(sys.argv[0]).with_name("uv.json").read_text())
hook = "import json, os; print(json.dumps(dict(os.environ)))"
seen = subprocess.run([sys.executable, "-c", hook], capture_output=True, text=True, check=True).stdout
with open(config["record"], "a") as record:
    record.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": json.loads(seen)}) + "\\n")
for _ in range(200):
    if not config["hold"] or os.path.exists(config["hold"]):
        break
    time.sleep(0.05)
time.sleep(config["sleep"])
if sys.argv[1] == "venv":
    pathlib.Path(sys.argv[-1]).mkdir(parents=True, exist_ok=True)
code = config["code"] if config["step"] in ("", sys.argv[1]) else 0
if code and config["say"]:
    print(config["say"])
print("uv answered", code)
sys.exit(code)
"""


@dataclass(frozen=True)
class Uv:
    folder: Path
    record: Path

    def answer(
        self, code: int = 0, sleep: float = 0.0, hold: Path | None = None, step: str = "", say: str = ""
    ) -> None:
        config = {"record": str(self.record), "code": code, "sleep": sleep, "hold": "" if hold is None else str(hold)}
        (self.folder / "uv.json").write_text(json.dumps({**config, "step": step, "say": say}))

    def isolations(self) -> list[list[str]]:
        calls = self.folder / "bwrap.calls"
        return [line.split() for line in calls.read_text().splitlines()] if calls.exists() else []

    def seen(self) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]


@pytest.fixture
def uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Uv:
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "uv"
    script.write_text(f"#!{sys.executable}\n{UV}")
    script.chmod(0o755)
    bwrap = folder / "bwrap"
    calls = folder / "bwrap.calls"
    bwrap.write_text(
        f'#!/bin/sh\nargs=""\nwhile [ "$1" != "--" ]; do args="$args $1"; shift; done\nshift\n'
        f'echo "$args" >> \'{calls}\'\nexec "$@"\n'
    )
    bwrap.chmod(0o755)
    fake = Uv(folder, tmp_path / "record.jsonl")
    fake.answer()
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CI_LAB_HOST_SECRET", "leak")
    return fake


def _build(whitelist: dict[str, str] | None = None, timeout: float = 30.0) -> venvs.Build:
    ports = sandbox.PortAllocator(first=20000, last=20099, width=100)
    return venvs.Build(ports, {"KEEP": "1"} if whitelist is None else whitelist, timeout)


def _tree(tmp_path: Path, name: str = "tree") -> Path:
    tree = tmp_path / name
    tree.mkdir()
    return tree


def _baked(tmp_path: Path, digest: str | None) -> Path:
    baked = tmp_path / "baked"
    baked.mkdir()
    if digest is not None:
        (baked / "lockfile.sha256").write_text(f"{digest}\n")
    return baked


def _built(tree: Path, venv: Path) -> list[dict[str, object]]:
    install = ["--constraint", str(tree / LOCK), "--excludes", str(tree / EXCLUDES), "-r", str(tree / "pyproject.toml")]
    steps = (
        ["venv", "--clear", "--python", sys.executable, str(venv)],
        ["pip", "install", "--python", str(venv / "bin" / "python"), *install, "--extra", "dev", "--extra", "all"],
    )
    return [{"argv": argv, "cwd": str(tree.resolve())} for argv in steps]


def _call(seen: dict[str, Any]) -> dict[str, object]:
    return {"argv": seen["argv"], "cwd": seen["cwd"]}


def test_the_baked_venv_lives_where_the_image_puts_it() -> None:
    assert venvs.BAKED == Path("/opt/agentihooks-venv")
    assert venvs.MARKER == "lockfile.sha256"


def test_the_baked_venv_serves_a_matching_digest(tmp_path: Path, uv: Uv) -> None:
    baked = _baked(tmp_path, DIGEST)
    assert venvs.venv_for(DIGEST, _tree(tmp_path), tmp_path / "venvs", _build(), baked) == (baked, False)
    assert uv.seen() == []
    assert not (tmp_path / "venvs").exists()


@pytest.mark.parametrize("baked_digest", [None, OTHER])
def test_a_digest_the_baked_venv_lacks_builds_its_own_venv(tmp_path: Path, uv: Uv, baked_digest: str | None) -> None:
    venv = tmp_path / "pod" / "venvs" / DIGEST
    tree = _tree(tmp_path)
    built = venvs.venv_for(DIGEST, tree, tmp_path / "pod" / "venvs", _build(), _baked(tmp_path, baked_digest))
    assert built == (venv, True)
    assert [_call(seen) for seen in uv.seen()] == _built(tree, venv)
    assert (venv / "lockfile.sha256").read_text() == f"{DIGEST}\n"


@pytest.mark.feature("lab-venv-build-sandbox")
@pytest.mark.parametrize("whitelist", [{"KEEP": "1"}, {"KEEP": "1", "PATH": "/opt/lab/bin"}])
def test_a_build_hook_sees_only_the_whitelist_and_the_sandbox_environment(
    tmp_path: Path, uv: Uv, whitelist: dict[str, str]
) -> None:
    root = tmp_path / "venvs"
    venvs.venv_for(DIGEST, _tree(tmp_path), root, _build(whitelist), _baked(tmp_path, None))
    seen = uv.seen()
    assert len(seen) == 2
    for call in seen:
        env = call["env"]
        directory = Path(env["HOME"]).parent
        assert "CI_LAB_HOST_SECRET" not in env
        own = {"PATH": whitelist.get("PATH", os.defpath)}
        assert env == sandbox.environment(directory, sandbox.PortRange(first=20000, last=20099), {**whitelist, **own})
        assert directory.parent == root / "builds"
    assert list((root / "builds").iterdir()) == []


def test_a_built_venv_is_reused_for_its_digest_only(tmp_path: Path, uv: Uv) -> None:
    baked = _baked(tmp_path, None)
    tree, other_tree = _tree(tmp_path), _tree(tmp_path, "other")
    first, built = venvs.venv_for(DIGEST, tree, tmp_path / "venvs", _build(), baked)
    again, rebuilt = venvs.venv_for(DIGEST, other_tree, tmp_path / "venvs", _build(), baked)
    other, fresh = venvs.venv_for(OTHER, other_tree, tmp_path / "venvs", _build(), baked)
    assert first == again == tmp_path / "venvs" / DIGEST
    assert other == tmp_path / "venvs" / OTHER
    assert (built, rebuilt, fresh) == (True, False, True)
    assert [_call(seen) for seen in uv.seen()] == [*_built(tree, first), *_built(other_tree, other)]


@pytest.mark.feature("lab-venv-build-sandbox")
def test_a_failed_build_leaves_no_marker_and_builds_again(tmp_path: Path, uv: Uv) -> None:
    baked, tree, root = _baked(tmp_path, None), _tree(tmp_path), tmp_path / "venvs"
    uv.answer(code=3)
    message = f"^uv venv for {re.escape(str(tree))} ended with code 3; its log is {re.escape(str(root / 'builds'))}/"
    with pytest.raises(sandbox.CannotRun, match=message):
        venvs.venv_for(DIGEST, tree, root, _build(), baked)
    assert not (root / DIGEST / "lockfile.sha256").exists()
    (kept,) = (root / "builds").iterdir()
    assert (kept / "log.txt").read_text() == "uv answered 3\n"
    uv.answer()
    assert venvs.venv_for(DIGEST, tree, root, _build(), baked) == (root / DIGEST, True)
    steps = _built(tree, root / DIGEST)
    assert [_call(seen) for seen in uv.seen()] == [steps[0], *steps]


@pytest.mark.feature("lab-venv-build-sandbox")
def test_a_failed_install_names_its_step_and_keeps_only_its_log(tmp_path: Path, uv: Uv) -> None:
    baked, tree, root = _baked(tmp_path, None), _tree(tmp_path), tmp_path / "venvs"
    uv.answer(code=4, step="pip")
    message = (
        f"^uv pip install for {re.escape(str(tree))} ended with code 4; its log is {re.escape(str(root / 'builds'))}/"
    )
    with pytest.raises(sandbox.CannotRun, match=message):
        venvs.venv_for(DIGEST, tree, root, _build(), baked)
    assert not (root / DIGEST / "lockfile.sha256").exists()
    (kept,) = (root / "builds").iterdir()
    assert (kept / "log.txt").read_text() == "uv answered 4\n"
    assert [_call(seen) for seen in uv.seen()] == _built(tree, root / DIGEST)


@pytest.mark.feature("lab-venv-build-sandbox")
def test_a_build_past_its_time_limit_is_cannot_run(tmp_path: Path, uv: Uv) -> None:
    tree, root = _tree(tmp_path), tmp_path / "venvs"
    uv.answer(sleep=5.0)
    with pytest.raises(sandbox.CannotRun, match=f"^uv venv for {re.escape(str(tree))} ended with code None; "):
        venvs.venv_for(DIGEST, tree, root, _build(timeout=1.0), _baked(tmp_path, None))
    assert not (root / DIGEST / "lockfile.sha256").exists()


def test_a_worker_without_uv_cannot_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(sandbox.CannotRun, match="^uv is not installed, so a venv cannot be built$"):
        venvs.venv_for(DIGEST, _tree(tmp_path), tmp_path / "venvs", _build(), _baked(tmp_path, None))
    assert not (tmp_path / "venvs" / DIGEST).exists()


def test_a_second_caller_waits_for_the_running_build(tmp_path: Path, uv: Uv) -> None:
    hold = tmp_path / "hold"
    uv.answer(hold=hold)
    baked, tree = _baked(tmp_path, None), _tree(tmp_path)
    built: list[tuple[Path, bool]] = []

    def call() -> None:
        built.append(venvs.venv_for(DIGEST, tree, tmp_path / "venvs", _build(), baked))

    first, second = threading.Thread(target=call), threading.Thread(target=call)
    first.start()
    for _ in range(200):
        if uv.record.exists():
            break
        time.sleep(0.05)
    with (tmp_path / "venvs" / f"{DIGEST}.lock").open() as held, pytest.raises(BlockingIOError):
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    second.start()
    hold.touch()
    first.join(10)
    second.join(10)
    assert sorted(built) == [(tmp_path / "venvs" / DIGEST, False), (tmp_path / "venvs" / DIGEST, True)]
    assert [_call(seen) for seen in uv.seen()] == _built(tree, tmp_path / "venvs" / DIGEST)


@pytest.mark.feature("lab-venv-offline-cache")
def test_venv_builds_run_offline_without_network_on_the_worker_copy_of_the_node_cache(tmp_path: Path, uv: Uv) -> None:
    node, root = tmp_path / "node", tmp_path / "venvs"
    node.mkdir()
    (node / "warmed-lock").write_text("lock\n")
    (node / "entry").write_text("cached")
    build = venvs.Build(sandbox.PortAllocator(first=20000, last=20099, width=100), {"KEEP": "1"}, 30.0, node)
    venvs.venv_for(DIGEST, _tree(tmp_path), root, build, _baked(tmp_path, None))
    calls = uv.seen()
    assert len(calls) == 2
    cache = uv_cache.cache_for(node, root)
    assert (cache / "entry").read_text() == "cached"
    offline = {"UV_CACHE_DIR": str(cache), "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "UV_LINK_MODE": "copy"}
    assert [{name: call["env"].get(name) for name in offline} for call in calls] == [offline, offline]
    assert uv.isolations() == [["--dev-bind", "/", "/", "--unshare-net", "--die-with-parent"]] * 2


@pytest.mark.feature("lab-venv-offline-cache")
def test_a_build_the_cache_cannot_serve_names_each_miss(tmp_path: Path, uv: Uv) -> None:
    tree, root = _tree(tmp_path), tmp_path / "venvs"
    uv.answer(code=1, step="pip", say=MISSES)
    message = (
        f"^uv cache miss for {re.escape(str(tree))}: uncached-dependency, "
        f"http://index/files/partial-1.0-py3-none-any.whl; its log is {re.escape(str(root / 'builds'))}/[^/]+/log.txt$"
    )
    with pytest.raises(sandbox.CannotRun, match=message):
        venvs.venv_for(DIGEST, tree, root, _build(), _baked(tmp_path, None))
    assert not (root / DIGEST / "lockfile.sha256").exists()
