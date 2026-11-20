import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from scripts.ci_lab import sandbox

BWRAP = ["--dev-bind", "/", "/", "--unshare-net", "--die-with-parent", "--"]
SHOW_ENV = "import json, os; print(json.dumps({'cwd': os.getcwd(), 'env': dict(os.environ)}))"
NETWORK = """
import os, socket
first = int(os.environ["CI_LAB_PORT_FIRST"])
server = socket.create_server(("127.0.0.1", first))
socket.create_connection(("127.0.0.1", first), timeout=2).close()
server.close()
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2).close()
except OSError:
    print("egress refused")
else:
    print("egress open")
"""
READ_ONLY = """
import os, sys
for tree in sys.argv[1:]:
    gate = os.path.join(tree, "gate.py")
    attempts = (
        ("chmod", lambda: os.chmod(gate, 0o644)),
        ("rewrite", lambda: open(gate, "w").close()),
        ("plant", lambda: open(os.path.join(tree, "planted.py"), "w").close()),
    )
    for name, attempt in attempts:
        try:
            attempt()
        except OSError as error:
            print(name, error.errno)
        else:
            print(name, "landed")
open("unit.txt", "w").close()
"""


def _gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return True
    return state == "Z"


def _gone_within(pid: int, seconds: float) -> bool:
    for _ in range(int(seconds * 10)):
        if _gone(pid):
            return True
        time.sleep(0.1)
    return _gone(pid)


def _bwrap_isolates() -> bool:
    path = shutil.which("bwrap")
    if path is None:
        return False
    probe = subprocess.run([path, "--dev-bind", "/", "/", "--unshare-net", "true"], capture_output=True, check=False)
    return probe.returncode == 0


def _fake_bwrap(folder: Path, record: Path) -> None:
    folder.mkdir()
    script = folder / "bwrap"
    script.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > \'{record}\'\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n'
    )
    script.chmod(0o755)


def _sandbox(root: Path, ports: sandbox.PortAllocator, whitelist: dict[str, str]) -> sandbox.Sandbox:
    root.mkdir()
    return sandbox.Sandbox(root=root, cwd=root, ports=ports, whitelist=whitelist)


def test_the_argv_wraps_the_unit_in_bubblewrap_with_a_cleared_environment() -> None:
    argv = sandbox.argv_for(["python", "-c", "x"], {"B": "2", "A": "1"}, "/usr/bin/bwrap")
    assert argv == ["env", "-i", "A=1", "B=2", "/usr/bin/bwrap", *BWRAP, "python", "-c", "x"]


def test_the_argv_without_bubblewrap_keeps_the_host_network() -> None:
    assert sandbox.argv_for(["python"], {"A": "1"}, None) == ["env", "-i", "A=1", "python"]


def test_the_environment_is_private_and_whitelisted(tmp_path: Path) -> None:
    ports = sandbox.PortRange(first=20000, last=20099)
    env = sandbox.environment(tmp_path, ports, {"PATH": "/bin", "HOME": "/host"})
    assert env == {
        "PATH": "/bin",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "XDG_CONFIG_HOME": str(tmp_path / "home" / ".config"),
        "XDG_CACHE_HOME": str(tmp_path / "home" / ".cache"),
        "XDG_DATA_HOME": str(tmp_path / "home" / ".local" / "share"),
        "XDG_STATE_HOME": str(tmp_path / "home" / ".local" / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "CI": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CI_LAB_PORT_FIRST": "20000",
        "CI_LAB_PORT_LAST": "20099",
    }


def test_the_whitelist_key_is_the_digest_of_its_names_and_values() -> None:
    expected = hashlib.sha256(b'{"A":"1","B":"2"}').hexdigest()
    assert sandbox.whitelist_key({"B": "2", "A": "1"}) == expected
    assert sandbox.whitelist_key({"B": "2", "A": "3"}) != expected
    assert sandbox.whitelist_key({"B": "2", "C": "1"}) != expected


def test_port_ranges_and_outcomes_are_frozen(tmp_path: Path) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(sandbox.PortRange(first=1, last=2), "first", 3)
    with pytest.raises(FrozenInstanceError):
        setattr(sandbox.Outcome(code=0, log=tmp_path), "code", 1)


def test_the_allocator_hands_out_disjoint_ranges_inside_the_span() -> None:
    ports = sandbox.PortAllocator(first=100, last=299, width=100)
    assert ports.take() == sandbox.PortRange(first=100, last=199)
    assert ports.take() == sandbox.PortRange(first=200, last=299)
    with pytest.raises(sandbox.CannotRun, match="^no free port range$"):
        ports.take()


def test_the_allocator_never_hands_out_a_partial_range() -> None:
    ports = sandbox.PortAllocator(first=100, last=298, width=100)
    assert ports.take() == sandbox.PortRange(first=100, last=199)
    with pytest.raises(sandbox.CannotRun):
        ports.take()


def test_a_returned_range_is_handed_out_again() -> None:
    ports = sandbox.PortAllocator(first=100, last=199, width=100)
    taken = ports.take()
    ports.give(taken)
    assert ports.take() == taken


def test_a_timeout_kills_the_whole_process_tree(tmp_path: Path) -> None:
    whitelist = {"PATH": os.environ["PATH"]}
    unit = _sandbox(tmp_path / "root", sandbox.PortAllocator(first=20000, last=20099, width=100), whitelist)
    outcome = unit.run(["/bin/sh", "-c", "sleep 60 & echo $!; wait"], timeout=3, net=True)
    assert outcome.code is None
    assert _gone_within(int(outcome.log.read_text().split()[0]), 5)


@pytest.mark.feature("lab-sandbox-group-killed-at-exit")
@pytest.mark.parametrize("code", [0, 3])
def test_a_child_left_after_the_unit_exits_is_killed_when_the_run_returns(tmp_path: Path, code: int) -> None:
    whitelist = {"PATH": os.environ["PATH"]}
    unit = _sandbox(tmp_path / "root", sandbox.PortAllocator(first=20000, last=20099, width=100), whitelist)
    outcome = unit.run(["/bin/sh", "-c", f"sleep 60 & echo $!; exit {code}"], timeout=30, net=True)
    assert outcome.code == code
    assert _gone_within(int(outcome.log.read_text().split()[0]), 5)


def test_the_unit_sees_only_the_whitelist_and_its_private_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI_LAB_HOST_SECRET", "leak")
    root = tmp_path / "root"
    unit = _sandbox(root, sandbox.PortAllocator(first=20000, last=20099, width=100), {"KEEP": "1"})
    outcome = unit.run([sys.executable, "-c", SHOW_ENV], timeout=30, net=True)
    directory = outcome.log.parent
    assert (outcome.code, outcome.log.name, directory.parent) == (0, "log.txt", root)
    seen = json.loads(outcome.log.read_text())
    assert seen["cwd"] == str(root.resolve())
    assert "CI_LAB_HOST_SECRET" not in seen["env"]
    expected = sandbox.environment(directory, sandbox.PortRange(first=20000, last=20099), {"KEEP": "1"})
    assert seen["env"] == expected
    for name in ("home", "tmp", "runtime"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o700


def test_the_port_range_returns_to_the_allocator_after_each_run(tmp_path: Path) -> None:
    unit = _sandbox(tmp_path / "root", sandbox.PortAllocator(first=20000, last=20099, width=100), {})
    assert unit.run(["/bin/true"], timeout=30, net=True).code == 0
    assert unit.run(["/bin/true"], timeout=30, net=True).code == 0


def test_a_missing_bwrap_is_cannot_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    unit = _sandbox(tmp_path / "root", sandbox.PortAllocator(first=20000, last=20099, width=100), {})
    message = "^bwrap is not installed, so a unit without network cannot run$"
    with pytest.raises(sandbox.CannotRun, match=message):
        unit.run(["/bin/true"], timeout=30)


def test_a_unit_without_network_runs_inside_bwrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = tmp_path / "args"
    _fake_bwrap(tmp_path / "bin", record)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    unit = _sandbox(tmp_path / "root", sandbox.PortAllocator(first=20000, last=20099, width=100), {})
    assert unit.run(["/bin/true"], timeout=30).code == 0
    assert record.read_text().splitlines() == [*BWRAP, "/bin/true"]


def test_loopback_works_and_egress_is_refused_without_network(tmp_path: Path) -> None:
    if not _bwrap_isolates():
        pytest.skip("bubblewrap cannot create a network namespace on this runner")
    unit = _sandbox(tmp_path / "root", sandbox.PortAllocator(first=20000, last=20099, width=100), {})
    outcome = unit.run([sys.executable, "-c", NETWORK], timeout=30)
    assert (outcome.code, outcome.log.read_text().strip()) == (0, "egress refused")


@pytest.mark.feature("lab-sandbox-read-only-trees")
@pytest.mark.parametrize(("net", "network"), [(False, ["--unshare-net"]), (True, [])])
def test_a_unit_with_read_only_trees_runs_inside_bwrap_with_each_tree_bound_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: bool, network: list[str]
) -> None:
    record = tmp_path / "args"
    _fake_bwrap(tmp_path / "bin", record)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    grader, base, root = tmp_path / "grader", tmp_path / "base", tmp_path / "root"
    root.mkdir()
    ports = sandbox.PortAllocator(first=20000, last=20099, width=100)
    unit = sandbox.Sandbox(root=root, cwd=root, ports=ports, whitelist={}, readonly=[grader, base])
    assert unit.run(["/bin/true"], timeout=30, net=net).code == 0
    binds = ["--ro-bind", str(grader), str(grader), "--ro-bind", str(base), str(base)]
    isolation = ["--dev-bind", "/", "/", *binds, *network, "--die-with-parent", "--"]
    assert record.read_text().splitlines() == [*isolation, "/bin/true"]


@pytest.mark.feature("lab-sandbox-read-only-trees")
def test_a_unit_with_network_and_read_only_trees_cannot_run_without_bwrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    root = tmp_path / "root"
    root.mkdir()
    ports = sandbox.PortAllocator(first=20000, last=20099, width=100)
    unit = sandbox.Sandbox(root=root, cwd=root, ports=ports, whitelist={}, readonly=[tmp_path / "grader"])
    message = "^bwrap is not installed, so a unit with read only trees cannot run$"
    with pytest.raises(sandbox.CannotRun, match=message):
        unit.run(["/bin/true"], timeout=30, net=True)


@pytest.mark.parametrize("net", [False, True])
def test_a_unit_cannot_restore_write_bits_or_write_into_a_read_only_tree(tmp_path: Path, net: bool) -> None:
    if not _bwrap_isolates():
        pytest.skip("bubblewrap cannot create namespaces on this runner")
    trees = [tmp_path / "grader", tmp_path / "base"]
    for tree in trees:
        tree.mkdir()
        (tree / "gate.py").write_text("graded\n")
        (tree / "gate.py").chmod(0o444)
    worktree, root = tmp_path / "worktree", tmp_path / "root"
    worktree.mkdir()
    root.mkdir()
    ports = sandbox.PortAllocator(first=20000, last=20099, width=100)
    unit = sandbox.Sandbox(root=root, cwd=worktree, ports=ports, whitelist={}, readonly=trees)
    outcome = unit.run([sys.executable, "-c", READ_ONLY, *map(str, trees)], timeout=30, net=net)
    refused = [f"{name} {errno.EROFS}" for _ in trees for name in ("chmod", "rewrite", "plant")]
    assert (outcome.code, outcome.log.read_text().splitlines()) == (0, refused)
    for tree in trees:
        assert [path.name for path in tree.iterdir()] == ["gate.py"]
        assert (tree / "gate.py").read_text() == "graded\n"
        assert stat.S_IMODE((tree / "gate.py").stat().st_mode) == 0o444
    assert (worktree / "unit.txt").is_file()
