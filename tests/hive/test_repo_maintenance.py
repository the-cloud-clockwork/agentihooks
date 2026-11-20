import hashlib
import json
import os
import subprocess
import zlib
from pathlib import Path

import pytest

from scripts.hive import repo_maintenance


@pytest.mark.feature("hive-startup-recovers-stuck-gc")
@pytest.mark.parametrize("state", ["stuck", "healthy", "missing"])
def test_startup_recovers_only_configured_existing_checkouts_with_a_gc_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    checkout = tmp_path / "agentihooks"
    log = checkout / ".git" / "gc.log"
    if state != "missing":
        log.parent.mkdir(parents=True)
    if state == "stuck":
        log.write_text("warning: There are too many unreachable loose objects; run 'git prune' to remove them.\n")
    other = tmp_path / "unconfigured" / ".git" / "gc.log"
    other.parent.mkdir(parents=True)
    other.write_text("keep this log")
    calls: list[list[str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "prune" in command:
            assert log.is_file()
            assert options == {"check": True, "timeout": 180}
        if "gc" in command:
            assert not log.exists()
            assert options == {"check": True, "timeout": 180}
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("HIVE_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "HIVE_REPOSITORIES", json.dumps([{"name": "agentihooks", "url": "https://example.invalid/repo.git"}])
    )
    startup = Path(__file__).resolve().parents[2] / "deploy" / "worker" / "start.sh"
    block = startup.read_text().split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    exec(compile(block, str(startup), "exec"), {})

    expected = {
        "stuck": [
            ["git", "-C", str(checkout), "prune", "--expire=2.weeks.ago"],
            ["git", "-C", str(checkout), "gc", "--cruft", "--prune=2.weeks.ago", "--quiet"],
        ],
        "healthy": [],
        "missing": [
            ["git", "clone", "--branch", "dev", "--", "https://example.invalid/repo.git", str(checkout)],
        ],
    }
    assert calls == expected[state]
    assert not log.exists()
    assert other.read_text() == "keep this log"


@pytest.mark.feature("hive-startup-recovers-stuck-gc")
def test_failed_pruning_keeps_the_gc_log_and_does_not_start_gc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / ".git" / "gc.log"
    log.parent.mkdir()
    warning = "warning: There are too many unreachable loose objects; run 'git prune' to remove them.\n"
    log.write_text(warning)
    calls: list[list[str]] = []

    def fail(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert options == {"check": True, "timeout": 180}
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        repo_maintenance.recover(tmp_path)
    assert calls == [["git", "-C", str(tmp_path), "prune", "--expire=2.weeks.ago"]]
    assert log.read_text() == warning


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True, timeout=30)


@pytest.mark.feature("hive-startup-recovers-stuck-gc")
def test_recovery_retains_young_unreachable_objects_without_another_gc_warning(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    checkout = tmp_path / "checkout"
    _git("init", "--bare", "--object-format=sha1", str(upstream))
    _git("clone", str(upstream), str(checkout))
    for key, value in [("gc.auto", "1"), ("gc.autoDetach", "false"), ("gc.cruftPacks", "false")]:
        _git("-C", str(checkout), "config", key, value)
    objects: list[tuple[str, Path]] = []
    for number in range(65536):
        payload = f"unreachable-{number}".encode()
        raw = f"blob {len(payload)}\0".encode() + payload
        oid = hashlib.sha1(raw, usedforsecurity=False).hexdigest()
        if not oid.startswith("17"):
            continue
        path = checkout / ".git" / "objects" / oid[:2] / oid[2:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(zlib.compress(raw))
        objects.append((oid, path))
        if len(objects) == 3:
            break
    assert len(objects) == 3
    os.utime(objects[0][1], (1, 1))
    log = checkout / ".git" / "gc.log"
    log.write_text("warning: There are too many unreachable loose objects; run 'git prune' to remove them.\n")

    repo_maintenance.recover(checkout)

    fetched = _git("-C", str(checkout), "fetch", "origin")
    assert "too many unreachable loose objects" not in fetched.stderr
    assert not log.exists()
    assert _git("-C", str(checkout), "cat-file", "-e", objects[0][0], check=False).returncode != 0
    for oid, _path in objects[1:]:
        assert _git("-C", str(checkout), "cat-file", "-e", oid).returncode == 0
    assert "count: 0" in _git("-C", str(checkout), "count-objects", "-v").stdout.splitlines()
