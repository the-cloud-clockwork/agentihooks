from pathlib import Path

import pytest

from scripts.swarm_v2 import worker_storage

pytestmark = pytest.mark.unit


@pytest.fixture
def probe():
    return worker_storage


@pytest.fixture
def proc_files(monkeypatch):
    files = {}
    monkeypatch.setattr(Path, "read_text", lambda path: files[str(path)])
    return files


def test_the_storage_check_rejects_guest_tmpfs(probe, proc_files):
    proc_files["/proc/self/mountinfo"] = "30 20 0:44 / / rw - tmpfs tmpfs rw\n"
    with pytest.raises(RuntimeError, match="not on a block device"):
        probe.filesystem(Path("/home/worker"))


def test_the_storage_check_resolves_both_paths_to_the_root_block_device(probe, proc_files):
    proc_files["/proc/self/mountinfo"] = "30 20 254:0 / / rw - ext4 /dev/vda rw\n"
    for folder in (Path("/home/worker"), Path("/tmp")):
        assert probe.filesystem(folder) == {"device": "254:0", "mount": "/", "type": "ext4", "source": "/dev/vda"}


@pytest.mark.parametrize("device,source", [("0:44", "/dev/vda"), ("254:0", "tmpfs")])
def test_the_storage_check_requires_both_a_block_device_number_and_source(probe, proc_files, device, source):
    proc_files["/proc/self/mountinfo"] = f"30 20 {device} / / rw - ext4 {source} rw\n"
    with pytest.raises(RuntimeError, match="not on a block device"):
        probe.filesystem(Path("/tmp"))


def test_the_storage_check_sees_a_tmpfs_mount_below_the_root_disk(probe, proc_files):
    proc_files["/proc/self/mountinfo"] = (
        "30 20 254:0 / / rw - ext4 /dev/vda rw\n31 30 0:44 / /tmp rw - tmpfs tmpfs rw\n"
    )
    with pytest.raises(RuntimeError, match="not on a block device"):
        probe.filesystem(Path("/tmp"))


def test_mounts_with_equal_depth_do_not_compare_metadata_dictionaries(probe, proc_files):
    proc_files["/proc/self/mountinfo"] = (
        "30 20 254:0 / / rw - ext4 /dev/vda rw\n31 20 254:1 / / rw - ext4 /dev/vdb rw\n"
    )
    assert probe.filesystem(Path("/home/worker"))["source"] == "/dev/vda"


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
        with pytest.raises(RuntimeError, match="^large file write increased guest memory beyond 64 MiB$"):
            probe.main()
    else:
        probe.main()
    assert list(root.iterdir()) == []


def test_the_storage_check_finishes_both_writes_and_removes_its_files(probe, monkeypatch, tmp_path, capsys):
    root = tmp_path / "writes"
    root.mkdir()
    monkeypatch.setattr(probe, "Path", lambda value: root)
    monkeypatch.setattr(probe, "MIB", 1024)
    monkeypatch.setattr(probe, "SIZE", 16 * 1024)
    monkeypatch.setattr(probe, "filesystem", lambda path: {"type": "ext4"})
    monkeypatch.setattr(probe, "memory", lambda: {"used": 100, "anonymous": 20, "shmem": 4})
    probe.main()
    output = capsys.readouterr().out
    assert output.count('"bytes": 16384') == 2
    assert output.endswith("storage proof passed\n")
    assert list(root.iterdir()) == []


def test_storage_measurement_preserves_an_existing_file_with_a_predictable_name(probe, monkeypatch, tmp_path):
    root = tmp_path / "writes"
    root.mkdir()
    original = root / f"storage-proof-{probe.os.getpid()}"
    original.write_bytes(b"existing worker data")
    monkeypatch.setattr(probe, "Path", lambda value: root)
    monkeypatch.setattr(probe, "MIB", 1024)
    monkeypatch.setattr(probe, "SIZE", 16 * 1024)
    monkeypatch.setattr(probe, "filesystem", lambda path: {"type": "ext4"})
    monkeypatch.setattr(probe, "memory", lambda: {"used": 100, "anonymous": 20, "shmem": 4})
    probe.main()
    assert original.read_bytes() == b"existing worker data"
    assert list(root.iterdir()) == [original]


def test_the_guest_memory_reading_separates_usage_anonymous_and_shared_pages(probe, proc_files):
    proc_files["/proc/meminfo"] = "MemTotal: 2048 kB\nMemAvailable: 1024 kB\nAnonPages: 128 kB\nShmem: 64 kB\n"
    assert probe.memory() == {"used": 1048576, "anonymous": 131072, "shmem": 65536}


class ProbeFile:
    def __init__(self, folder, files):
        self.folder = folder
        self.size = 0
        self.removed = False
        files.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.removed = True
        return None

    def write(self, chunk):
        self.size += len(chunk)
        assert self.size <= 1073741824
        return len(chunk)

    def fileno(self):
        return 17


@pytest.fixture
def default_filesystem(probe, monkeypatch):
    from types import SimpleNamespace

    files, flushes = [], []

    def device(path):
        assert str(path) in ("/home/worker", "/tmp")
        return {"type": "ext4"}

    def create(dir, buffering):
        assert buffering == 0
        return ProbeFile(str(dir), files)

    def stat(fd):
        assert fd == 17
        return SimpleNamespace(st_size=files[-1].size)

    def sync(fd):
        assert fd == 17

    def evict(fd, offset, length, flag):
        assert (fd, offset, length, flag) == (17, 0, 0, probe.os.POSIX_FADV_DONTNEED)
        flushes.append((files[-1].folder, files[-1].size))

    monkeypatch.setattr(probe.tempfile, "TemporaryFile", create)
    monkeypatch.setattr(probe, "filesystem", device)
    monkeypatch.setattr(probe, "memory", lambda: {"used": 100, "anonymous": 20, "shmem": 4})
    monkeypatch.setattr(probe.os, "fstat", stat)
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
