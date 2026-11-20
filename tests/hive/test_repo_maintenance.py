import json
import subprocess
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
            ["git", "-C", str(checkout), "gc", "--auto", "--quiet"],
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
