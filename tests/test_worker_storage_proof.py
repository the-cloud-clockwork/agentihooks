import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location(
        "worker_storage_proof", Path(__file__).parents[1] / "docker/swarm-node/storage-proof.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
