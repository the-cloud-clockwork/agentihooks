from pathlib import Path

import pytest

from scripts.swarm_v2 import worker_storage

pytestmark = pytest.mark.unit


@pytest.fixture
def probe():
    return worker_storage


def test_the_storage_check_rejects_guest_tmpfs(probe, monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda self: "30 20 0:44 / / rw - tmpfs tmpfs rw\n")
    with pytest.raises(RuntimeError, match="not on a block device"):
        probe.filesystem(Path("/home/worker"))


def test_the_storage_check_resolves_both_paths_to_the_root_block_device(probe, monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda self: "30 20 254:0 / / rw - ext4 /dev/vda rw\n")
    for folder in (Path("/home/worker"), Path("/tmp")):
        assert probe.filesystem(folder) == {"device": "254:0", "mount": "/", "type": "ext4", "source": "/dev/vda"}


def test_the_storage_check_rejects_memory_growth_in_the_final_sample(probe, monkeypatch, tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(probe, "Path", lambda value: root)
    monkeypatch.setattr(probe, "MIB", 1024)
    monkeypatch.setattr(probe, "SIZE", 16 * 1024)
    monkeypatch.setattr(probe, "TOLERANCE", 64)
    monkeypatch.setattr(probe, "filesystem", lambda path: {"type": "ext4"})
    samples = iter([{"used": 100}, {"used": 100}, {"used": 200}])
    monkeypatch.setattr(probe, "memory", lambda: next(samples))
    with pytest.raises(RuntimeError, match="increased guest memory"):
        probe.main()
    assert list(root.iterdir()) == []


def test_the_storage_check_finishes_both_writes_and_removes_its_files(probe, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(probe, "Path", lambda value: tmp_path)
    monkeypatch.setattr(probe, "MIB", 1024)
    monkeypatch.setattr(probe, "SIZE", 16 * 1024)
    monkeypatch.setattr(probe, "filesystem", lambda path: {"type": "ext4"})
    monkeypatch.setattr(probe, "memory", lambda: {"used": 100, "anonymous": 20, "shmem": 4})
    probe.main()
    output = capsys.readouterr().out
    assert output.count('"bytes": 16384') == 2
    assert output.endswith("storage proof passed\n")
    assert list(tmp_path.iterdir()) == []


def test_the_guest_memory_reading_separates_usage_anonymous_and_shared_pages(probe, monkeypatch):
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self: "MemTotal: 2048 kB\nMemAvailable: 1024 kB\nAnonPages: 128 kB\nShmem: 64 kB\n",
    )
    assert probe.memory() == {"used": 1048576, "anonymous": 131072, "shmem": 65536}
