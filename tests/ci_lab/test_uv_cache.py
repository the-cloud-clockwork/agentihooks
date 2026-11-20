from pathlib import Path

import pytest

from scripts.ci_lab.sandbox import CannotRun
from scripts.ci_lab.uv_cache import cache_for


@pytest.mark.feature("lab-venv-offline-cache")
def test_the_worker_cache_is_writable_and_reused_until_the_node_warms_again(tmp_path: Path) -> None:
    source = tmp_path / "node"
    source.mkdir()
    (source / "warmed-lock").write_text("first\n")
    (source / "package").write_text("original")
    (source / "package").chmod(0o444)
    root = tmp_path / "worker"
    first = cache_for(source, root, 30.0)
    assert first.is_relative_to(root)
    assert first != source
    assert (first / "package").read_text() == "original"
    (first / "package").write_text("private")
    assert (source / "package").read_text() == "original"
    assert cache_for(source, root, 30.0) == first
    assert (first / "package").read_text() == "private"
    (source / "warmed-lock").write_text("second\n")
    second = cache_for(source, root, 30.0)
    assert second != first
    assert (second / "package").read_text() == "original"
    assert (first / "package").read_text() == "private"


@pytest.mark.feature("lab-venv-offline-cache")
def test_an_unconfigured_node_cache_uses_one_empty_writable_worker_cache(tmp_path: Path) -> None:
    cache = cache_for(None, tmp_path, 30.0)
    assert cache.is_dir()
    assert cache.is_relative_to(tmp_path)
    (cache / "kept").touch()
    assert cache_for(None, tmp_path, 30.0) == cache
    assert (cache / "kept").is_file()


@pytest.mark.feature("lab-venv-offline-cache")
def test_a_missing_node_cache_fails_by_name(tmp_path: Path) -> None:
    source = tmp_path / "missing"
    with pytest.raises(CannotRun, match="uv cache miss.*missing"):
        cache_for(source, tmp_path / "worker", 30.0)
