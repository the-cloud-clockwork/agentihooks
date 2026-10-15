import os
import shutil
from types import SimpleNamespace

import pytest

from scripts.doctor import detect, loop
from scripts.swarm.health.verdicts import VerdictStore
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.xdist_group("fakeredis")
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
    assert str(largest) in evidence and "10.0 minutes" in evidence
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
