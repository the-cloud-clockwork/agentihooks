import os
import shutil
from types import SimpleNamespace

import pytest

from scripts.doctor import detect, loop
from scripts.swarm.health.verdicts import VerdictStore
from scripts.swarm.store import RedisStore

NOW = 10_000_000
BLOCK = 4096


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def test_a_planted_full_runtime_is_reported_in_one_pass_and_returns_only_on_share_growth(tmp_path, monkeypatch, store):
    runtime = tmp_path / "runtime"
    largest = runtime / "cx" / "old-execution"
    largest.mkdir(parents=True)
    payload = largest / "codex-home"
    payload.write_bytes(b"x" * 81 * BLOCK)
    os.utime(largest, (9_400, 9_400))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ignored").write_bytes(b"x" * 100 * BLOCK)
    (largest / "linked-home").symlink_to(outside, target_is_directory=True)
    os.utime(largest, (9_400, 9_400))
    sample = SimpleNamespace(total=100 * BLOCK, used=81 * BLOCK)
    reads = []

    def usage(path):
        assert path == runtime
        reads.append(path)
        return sample

    monkeypatch.setattr(shutil, "disk_usage", usage)
    readers = detect.readers(store, None, "watched", NOW, environ={"XDG_RUNTIME_DIR": str(runtime)}, telemetry=False)
    found, failed = detect.collect({"runtime storage": readers["runtime storage"]})
    assert failed == []
    [finding] = found
    assert finding.kind == "runtime storage"
    assert finding.subject == str(runtime)
    assert finding.measure == 0.81
    assert "81.0%" in finding.summary
    evidence = "\n".join(finding.evidence)
    assert str(runtime) in evidence
    assert "331776" in evidence and "409600" in evidence
    allocated = sum(path.lstat().st_blocks * 512 for path in (largest, payload, largest / "linked-home"))
    assert finding.evidence[1] == f"execution {largest}: {allocated} allocated bytes, age 10.0 minutes"
    assert "outside" not in evidence
    assert len(reads) == 1
    [first] = loop.record(store, "doctor", found, NOW, 60_000)
    assert first["id"] == finding.id
    VerdictStore(store.redis, loop.verdicts(store, "doctor").key).judge(
        finding.id, "early-real", "planted full folder", "master", NOW
    )
    for share in (81, 80, 79, 81):
        sample.used = share * BLOCK
        later = detect.readers(
            store, None, "watched", NOW + 120_000, environ={"XDG_RUNTIME_DIR": str(runtime)}, telemetry=False
        )["runtime storage"]()
        assert loop.record(store, "doctor", later, NOW + 120_000, 60_000) == []
    sample.used = 82 * BLOCK
    grown = readers["runtime storage"]()
    assert grown[0].measure == 0.82
    assert loop.record(store, "doctor", grown, NOW + 59_999, 60_000) == []
    [returned] = loop.record(store, "doctor", grown, NOW + 60_000, 60_000)
    assert returned["id"] == finding.id
    assert len(reads) == 6


@pytest.mark.parametrize("used,total", [(0, 100), (79, 100), (80, 100), (160, 200)])
def test_a_healthy_runtime_never_scans_execution_homes(tmp_path, monkeypatch, used, total):
    from scripts.doctor import runtime

    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(used=used, total=total))
    monkeypatch.setattr(os, "walk", lambda *args, **kwargs: pytest.fail("healthy runtime was scanned"))
    assert runtime.findings(NOW, {"XDG_RUNTIME_DIR": str(tmp_path)}) == []


def test_runtime_pressure_names_only_the_three_largest_execution_homes_with_sizes_and_ages(tmp_path, monkeypatch):
    from scripts.doctor import runtime

    for name, blocks, stamp in (
        ("small", 1, 9_940),
        ("largest", 30, 8_800),
        ("second", 20, 9_400),
        ("third", 10, 10_060),
    ):
        directory = tmp_path / "cx" / name
        directory.mkdir(parents=True)
        (directory / "payload").write_bytes(b"x" * blocks * BLOCK)
        os.utime(directory, (stamp, stamp))
    (tmp_path / "cx" / "not-a-home").write_bytes(b"x")
    (tmp_path / "cx" / "linked").symlink_to(tmp_path / "cx" / "largest", target_is_directory=True)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(used=81, total=100))
    [finding] = runtime.findings(NOW, {"XDG_RUNTIME_DIR": str(tmp_path)})
    assert finding.threshold == "over 80% full"
    assert finding.evidence[0] == f"runtime {tmp_path}: 81 of 100 bytes used"
    assert len(finding.evidence) == 4
    for evidence, name, minutes, blocks in zip(
        finding.evidence[1:], ("largest", "second", "third"), ("20.0", "10.0", "0.0"), (30, 20, 10), strict=True
    ):
        directory = tmp_path / "cx" / name
        allocated = directory.stat().st_blocks * 512 + blocks * BLOCK
        assert evidence == f"execution {directory}: {allocated} allocated bytes, age {minutes} minutes"


def test_pressure_without_codex_homes_is_still_reported(tmp_path, monkeypatch):
    from scripts.doctor import runtime

    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(used=100, total=100))
    [finding] = runtime.findings(NOW, {"XDG_RUNTIME_DIR": str(tmp_path)})
    assert finding.measure == 1
    assert finding.summary == "user runtime folder is 100.0% full"
    assert finding.evidence == (f"runtime {tmp_path}: 100 of 100 bytes used",)


def test_a_linked_execution_root_is_not_scanned(tmp_path, monkeypatch):
    from scripts.doctor import runtime

    root = tmp_path / "runtime"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "execution").mkdir(parents=True)
    (root / "cx").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(used=90, total=100))
    monkeypatch.setattr(os, "walk", lambda *args, **kwargs: pytest.fail("linked execution root was scanned"))
    [finding] = runtime.findings(NOW, {"XDG_RUNTIME_DIR": str(root)})
    assert finding.evidence == (f"runtime {root}: 90 of 100 bytes used",)


def test_a_disappearing_execution_does_not_hide_later_execution_homes(tmp_path, monkeypatch):
    from scripts.doctor import runtime

    departing = tmp_path / "cx" / "departing"
    survivor = tmp_path / "cx" / "survivor"
    departing.mkdir(parents=True)
    survivor.mkdir()
    original_stat = type(tmp_path).stat
    calls = []

    def stat(path, *args, **kwargs):
        if path == departing:
            calls.append(path)
            if len(calls) == 3:
                raise FileNotFoundError("execution removed after listing")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "stat", stat)
    monkeypatch.setattr(type(tmp_path), "glob", lambda path, pattern: iter((departing, survivor)))
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(used=90, total=100))
    [finding] = runtime.findings(NOW, {"XDG_RUNTIME_DIR": str(tmp_path)})
    assert len(finding.evidence) == 2
    assert str(survivor) in finding.evidence[1]
    assert "departing" not in finding.evidence[1]
    assert len(calls) == 3


def test_a_missing_user_runtime_is_skipped(tmp_path, monkeypatch):
    from scripts.doctor import runtime

    monkeypatch.setattr(shutil, "disk_usage", lambda path: pytest.fail("absent runtime was read"))
    assert runtime.findings(NOW, {"XDG_RUNTIME_DIR": str(tmp_path / "missing")}) == []


def test_without_xdg_runtime_the_detector_reads_the_current_users_runtime(tmp_path, monkeypatch):
    from scripts.doctor import runtime

    seen = []
    monkeypatch.setattr(os, "getuid", lambda: 1234)
    monkeypatch.setattr(runtime, "Path", lambda path: seen.append(path) or tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(used=90, total=100))
    [finding] = runtime.findings(NOW, {})
    assert seen == ["/run/user/1234"]
    assert finding.subject == str(tmp_path)
    assert finding.measure == 0.9


def test_a_runtime_capacity_read_failure_is_named_by_the_collection_pass(tmp_path, monkeypatch, store):
    def unreadable(path):
        raise PermissionError("runtime unavailable")

    monkeypatch.setattr(shutil, "disk_usage", unreadable)
    reader = detect.readers(store, None, "watched", NOW, environ={"XDG_RUNTIME_DIR": str(tmp_path)})
    assert detect.collect({"runtime storage": reader["runtime storage"]}) == (
        [],
        ["the runtime storage detector failed: PermissionError: runtime unavailable"],
    )
