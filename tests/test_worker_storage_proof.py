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


def test_the_default_workload_writes_one_gibibyte_per_path_and_flushes_every_sixteen_mib(probe, monkeypatch, capsys):
    from types import SimpleNamespace

    files = []
    flushes = []

    class File:
        def __init__(self, folder):
            self.folder = folder
            self.size = 0
            self.removed = False
            files.append(self)

        def open(self, mode, buffering):
            assert (mode, buffering) == ("xb", 0)
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def write(self, chunk):
            self.size += len(chunk)
            return len(chunk)

        def fileno(self):
            return 17

        def stat(self):
            return SimpleNamespace(st_size=self.size)

        def unlink(self, missing_ok):
            assert missing_ok is True
            self.removed = True

    class Folder:
        def __init__(self, path):
            self.path = path

        def __str__(self):
            return self.path

        def __truediv__(self, name):
            assert name.startswith("storage-proof-")
            return File(self.path)

    def sync(fd):
        assert fd == 17

    def evict(fd, offset, length, flag):
        assert (fd, offset, length, flag) == (17, 0, 0, probe.os.POSIX_FADV_DONTNEED)
        flushes.append((files[-1].folder, files[-1].size))

    monkeypatch.setattr(probe, "Path", Folder)
    monkeypatch.setattr(probe, "filesystem", lambda path: {"type": "ext4"})
    monkeypatch.setattr(probe, "memory", lambda: {"used": 100, "anonymous": 20, "shmem": 4})
    monkeypatch.setattr(probe.os, "fsync", sync)
    monkeypatch.setattr(probe.os, "posix_fadvise", evict)
    probe.main()
    assert [(file.folder, file.size, file.removed) for file in files] == [
        ("/home/worker", 1073741824, True),
        ("/tmp", 1073741824, True),
    ]
    for folder in ("/home/worker", "/tmp"):
        assert [size for path, size in flushes if path == folder] == list(range(16777216, 1073741825, 16777216))
    assert capsys.readouterr().out.endswith("storage proof passed\n")
