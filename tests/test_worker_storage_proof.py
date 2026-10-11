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


@pytest.mark.parametrize("device,source", [("0:44", "/dev/vda"), ("254:0", "tmpfs")])
def test_the_storage_check_requires_both_a_block_device_number_and_source(probe, monkeypatch, device, source):
    monkeypatch.setattr(Path, "read_text", lambda self: f"30 20 {device} / / rw - ext4 {source} rw\n")
    with pytest.raises(RuntimeError, match="not on a block device"):
        probe.filesystem(Path("/tmp"))


def test_the_storage_check_sees_a_tmpfs_mount_below_the_root_disk(probe, monkeypatch):
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self: "30 20 254:0 / / rw - ext4 /dev/vda rw\n31 30 0:44 / /tmp rw - tmpfs tmpfs rw\n",
    )
    with pytest.raises(RuntimeError, match="not on a block device"):
        probe.filesystem(Path("/tmp"))


@pytest.mark.parametrize(
    "growth,tolerance,rejected", [(100, 64, True), (67108864, None, False), (67108865, None, True)]
)
def test_the_storage_check_rejects_memory_growth_in_the_final_sample(
    probe, monkeypatch, tmp_path, growth, tolerance, rejected
):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(probe, "Path", lambda value: root)
    monkeypatch.setattr(probe, "MIB", 1024)
    monkeypatch.setattr(probe, "SIZE", 16 * 1024)
    if tolerance is not None:
        monkeypatch.setattr(probe, "TOLERANCE", tolerance)
    monkeypatch.setattr(probe, "filesystem", lambda path: {"type": "ext4"})
    baseline = {"used": 100, "anonymous": 20, "shmem": 4}
    grown = {**baseline, "used": 100 + growth}
    samples = iter([baseline, baseline, grown, baseline, grown])
    monkeypatch.setattr(probe, "memory", lambda: next(samples))
    if rejected:
        with pytest.raises(RuntimeError, match="increased guest memory"):
            probe.main()
    else:
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


class ProbeFile:
    def __init__(self, folder, files):
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
        assert self.size <= 1073741824
        return len(chunk)

    def fileno(self):
        return 17

    def stat(self):
        from types import SimpleNamespace

        return SimpleNamespace(st_size=self.size)

    def unlink(self, missing_ok):
        assert missing_ok is True
        self.removed = True


class ProbeFolder:
    def __init__(self, path, files):
        self.path, self.files = path, files

    def __str__(self):
        return self.path

    def __truediv__(self, name):
        assert name.startswith("storage-proof-")
        return ProbeFile(self.path, self.files)


@pytest.fixture
def default_filesystem(probe, monkeypatch):
    files, flushes = [], []

    def sync(fd):
        assert fd == 17

    def evict(fd, offset, length, flag):
        assert (fd, offset, length, flag) == (17, 0, 0, probe.os.POSIX_FADV_DONTNEED)
        flushes.append((files[-1].folder, files[-1].size))

    monkeypatch.setattr(probe, "Path", lambda path: ProbeFolder(path, files))
    monkeypatch.setattr(probe, "filesystem", lambda path: {"type": "ext4"})
    monkeypatch.setattr(probe, "memory", lambda: {"used": 100, "anonymous": 20, "shmem": 4})
    monkeypatch.setattr(probe.os, "fsync", sync)
    monkeypatch.setattr(probe.os, "posix_fadvise", evict)
    return files, flushes


def test_the_default_workload_writes_one_gibibyte_per_path_and_flushes_every_sixteen_mib(
    probe, default_filesystem, capsys
):
    import json

    files, flushes = default_filesystem
    probe.main()
    assert [(file.folder, file.size, file.removed) for file in files] == [
        ("/home/worker", 1073741824, True),
        ("/tmp", 1073741824, True),
    ]
    for folder in ("/home/worker", "/tmp"):
        assert [size for path, size in flushes if path == folder] == list(range(16777216, 1073741825, 16777216))
    output = capsys.readouterr().out.splitlines()
    assert output[-1] == "storage proof passed"
    records = [json.loads(line) for line in output[:-1]]
    baseline = {"used": 100, "anonymous": 20, "shmem": 4}
    assert records[0] == {"baseline": baseline}
    assert len(records) == 5
    for index, path in ((1, "/home/worker"), (3, "/tmp")):
        assert records[index] == {"path": path, "filesystem": {"type": "ext4"}}
        assert records[index + 1] == {
            "path": path,
            "bytes": 1073741824,
            "before": baseline,
            "after": baseline,
            "peak": baseline,
        }
