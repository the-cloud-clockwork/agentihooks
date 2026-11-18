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

from scripts.ci_lab import sandbox, venvs

DIGEST = "a" * 64
OTHER = "b" * 64
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
pathlib.Path(os.environ["UV_PROJECT_ENVIRONMENT"]).mkdir(parents=True, exist_ok=True)
print("uv answered", config["code"])
sys.exit(config["code"])
"""


@dataclass(frozen=True)
class Uv:
    folder: Path
    record: Path

    def answer(self, code: int = 0, sleep: float = 0.0, hold: Path | None = None) -> None:
        config = {"record": str(self.record), "code": code, "sleep": sleep, "hold": "" if hold is None else str(hold)}
        (self.folder / "uv.json").write_text(json.dumps(config))

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


def _sync(tree: Path) -> dict[str, object]:
    argv = ["sync", "--frozen", "--no-install-project", "--all-extras", "--python", sys.executable, "--project"]
    return {"argv": [*argv, str(tree)], "cwd": str(tree.resolve())}


def _call(seen: dict[str, Any]) -> dict[str, object]:
    return {"argv": seen["argv"], "cwd": seen["cwd"]}


def test_the_baked_venv_lives_where_the_image_puts_it() -> None:
    assert venvs.BAKED == Path("/opt/agentihooks-venv")
    assert venvs.MARKER == "lockfile.sha256"


def test_the_baked_venv_serves_a_matching_digest(tmp_path: Path, uv: Uv) -> None:
    baked = _baked(tmp_path, DIGEST)
    assert venvs.venv_for(DIGEST, _tree(tmp_path), tmp_path / "venvs", _build(), baked) == baked
    assert uv.seen() == []
    assert not (tmp_path / "venvs").exists()


@pytest.mark.parametrize("baked_digest", [None, OTHER])
def test_a_digest_the_baked_venv_lacks_builds_its_own_venv(tmp_path: Path, uv: Uv, baked_digest: str | None) -> None:
    venv = tmp_path / "pod" / "venvs" / DIGEST
    tree = _tree(tmp_path)
    built = venvs.venv_for(DIGEST, tree, tmp_path / "pod" / "venvs", _build(), _baked(tmp_path, baked_digest))
    assert built == venv
    assert [_call(seen) for seen in uv.seen()] == [_sync(tree)]
    assert (venv / "lockfile.sha256").read_text() == f"{DIGEST}\n"


@pytest.mark.feature("lab-venv-build-sandbox")
@pytest.mark.parametrize("whitelist", [{"KEEP": "1"}, {"KEEP": "1", "PATH": "/opt/lab/bin"}])
def test_a_build_hook_sees_only_the_whitelist_and_the_sandbox_environment(
    tmp_path: Path, uv: Uv, whitelist: dict[str, str]
) -> None:
    root = tmp_path / "venvs"
    venv = venvs.venv_for(DIGEST, _tree(tmp_path), root, _build(whitelist), _baked(tmp_path, None))
    (seen,) = uv.seen()
    env = seen["env"]
    directory = Path(env["HOME"]).parent
    assert "CI_LAB_HOST_SECRET" not in env
    own = {"PATH": whitelist.get("PATH", os.defpath), "UV_PROJECT_ENVIRONMENT": str(venv)}
    assert env == sandbox.environment(directory, sandbox.PortRange(first=20000, last=20099), {**whitelist, **own})
    assert directory.parent == root / "builds"
    assert list((root / "builds").iterdir()) == []


def test_a_built_venv_is_reused_for_its_digest_only(tmp_path: Path, uv: Uv) -> None:
    baked = _baked(tmp_path, None)
    tree, other_tree = _tree(tmp_path), _tree(tmp_path, "other")
    first = venvs.venv_for(DIGEST, tree, tmp_path / "venvs", _build(), baked)
    again = venvs.venv_for(DIGEST, other_tree, tmp_path / "venvs", _build(), baked)
    other = venvs.venv_for(OTHER, other_tree, tmp_path / "venvs", _build(), baked)
    assert first == again == tmp_path / "venvs" / DIGEST
    assert other == tmp_path / "venvs" / OTHER
    assert [_call(seen) for seen in uv.seen()] == [_sync(tree), _sync(other_tree)]


@pytest.mark.feature("lab-venv-build-sandbox")
def test_a_failed_build_leaves_no_marker_and_builds_again(tmp_path: Path, uv: Uv) -> None:
    baked, tree, root = _baked(tmp_path, None), _tree(tmp_path), tmp_path / "venvs"
    uv.answer(code=3)
    message = f"^uv sync for {re.escape(str(tree))} ended with code 3; its log is {re.escape(str(root / 'builds'))}/"
    with pytest.raises(sandbox.CannotRun, match=message):
        venvs.venv_for(DIGEST, tree, root, _build(), baked)
    assert not (root / DIGEST / "lockfile.sha256").exists()
    (kept,) = (root / "builds").iterdir()
    assert (kept / "log.txt").read_text() == "uv answered 3\n"
    uv.answer()
    assert venvs.venv_for(DIGEST, tree, root, _build(), baked) == root / DIGEST
    assert [_call(seen) for seen in uv.seen()] == [_sync(tree)] * 2


@pytest.mark.feature("lab-venv-build-sandbox")
def test_a_build_past_its_time_limit_is_cannot_run(tmp_path: Path, uv: Uv) -> None:
    tree, root = _tree(tmp_path), tmp_path / "venvs"
    uv.answer(sleep=5.0)
    with pytest.raises(sandbox.CannotRun, match=f"^uv sync for {re.escape(str(tree))} ended with code None; "):
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
    built: list[Path] = []

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
    assert built == [tmp_path / "venvs" / DIGEST] * 2
    assert [_call(seen) for seen in uv.seen()] == [_sync(tree)]
