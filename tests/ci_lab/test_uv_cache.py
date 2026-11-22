import re
import stat
import tempfile
from pathlib import Path

import pytest

from scripts.ci_lab.sandbox import CannotRun
from scripts.ci_lab.uv_cache import cache_for


def _node(folder: Path, warmed: str) -> Path:
    bucket = folder / "bucket"
    bucket.mkdir(parents=True)
    (folder / "warmed-lock").write_text(warmed)
    (folder / "package").write_text("original")
    (folder / "package").chmod(0o444)
    (bucket / "link").symlink_to(folder / "package")
    bucket.chmod(0o555)
    return folder


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.feature("lab-venv-offline-cache")
def test_the_worker_cache_is_writable_and_reused_until_the_node_warms_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "no-temp-folder"))
    source, root = _node(tmp_path / "node", "first\n"), tmp_path / "worker"
    first = cache_for(source, root)
    assert first.parent == root / "uv-cache"
    assert (first / "package").read_text() == "original"
    assert (_mode(first / "package"), _mode(first / "bucket")) == (0o644, 0o755)
    assert _mode(first / "warmed-lock") == _mode(source / "warmed-lock")
    assert (first / "bucket" / "link").readlink() == source / "package"
    (first / "package").write_text("private")
    (first / "bucket" / "new").write_text("new")
    assert (source / "package").read_text() == "original"
    assert (_mode(source / "package"), _mode(source / "bucket")) == (0o444, 0o555)
    assert not (source / "bucket" / "new").exists()
    assert cache_for(source, root) == first
    assert (first / "package").read_text() == "private"
    (source / "warmed-lock").write_text("second\n")
    second = cache_for(source, root)
    assert (second.parent, second == first) == (first.parent, False)
    assert (second / "package").read_text() == "original"
    assert (first / "package").read_text() == "private"


@pytest.mark.feature("lab-venv-offline-cache")
def test_an_unconfigured_node_cache_uses_one_empty_writable_worker_cache(tmp_path: Path) -> None:
    root = tmp_path / "worker"
    cache = cache_for(None, root)
    assert cache.parent == root / "uv-cache"
    assert list(cache.iterdir()) == []
    (cache / "kept").touch()
    assert cache_for(None, root) == cache
    assert (cache / "kept").is_file()


@pytest.mark.feature("lab-venv-offline-cache")
@pytest.mark.parametrize("exists", [False, True], ids=["missing", "unwarmed"])
def test_a_node_cache_that_never_finished_warming_fails_by_name(tmp_path: Path, exists: bool) -> None:
    source = tmp_path / "node"
    if exists:
        source.mkdir()
    message = (
        f"^uv cache miss: the node uv cache {re.escape(str(source))} holds no warmed-lock marker, "
        "so node warming never finished$"
    )
    with pytest.raises(CannotRun, match=message):
        cache_for(source, tmp_path / "worker")
