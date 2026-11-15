import os
import subprocess
from pathlib import Path

import pytest

from scripts.ci_lab import venvs

DIGEST = "a" * 64
OTHER = "b" * 64


class Uv:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, options))
        if self.fail:
            raise subprocess.CalledProcessError(1, argv)
        env = options["env"]
        assert isinstance(env, dict)
        Path(env["UV_PROJECT_ENVIRONMENT"]).mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0)


@pytest.fixture
def uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Uv:
    fake = Uv()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    return fake


def _baked(tmp_path: Path, digest: str | None) -> Path:
    baked = tmp_path / "baked"
    baked.mkdir()
    if digest is not None:
        (baked / "lockfile.sha256").write_text(f"{digest}\n")
    return baked


def _build(tree: Path, venv: Path) -> tuple[list[str], dict[str, object]]:
    argv = ["uv", "sync", "--frozen", "--no-install-project", "--all-extras", "--project", str(tree)]
    return argv, {"check": True, "env": {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)}}


def test_the_baked_venv_lives_where_the_image_puts_it() -> None:
    assert venvs.BAKED == Path("/opt/agentihooks-venv")
    assert venvs.MARKER == "lockfile.sha256"


def test_the_baked_venv_serves_a_matching_digest(tmp_path: Path, uv: Uv) -> None:
    baked = _baked(tmp_path, DIGEST)
    assert venvs.venv_for(DIGEST, tmp_path / "tree", tmp_path / "venvs", baked) == baked
    assert uv.calls == []
    assert not (tmp_path / "venvs").exists()


@pytest.mark.parametrize("baked_digest", [None, OTHER])
def test_a_digest_the_baked_venv_lacks_builds_its_own_venv(tmp_path: Path, uv: Uv, baked_digest: str | None) -> None:
    venv = tmp_path / "venvs" / DIGEST
    built = venvs.venv_for(DIGEST, tmp_path / "tree", tmp_path / "venvs", _baked(tmp_path, baked_digest))
    assert built == venv
    assert uv.calls == [_build(tmp_path / "tree", venv)]
    assert (venv / "lockfile.sha256").read_text() == f"{DIGEST}\n"


def test_a_built_venv_is_reused_for_its_digest_only(tmp_path: Path, uv: Uv) -> None:
    baked = _baked(tmp_path, None)
    first = venvs.venv_for(DIGEST, tmp_path / "tree", tmp_path / "venvs", baked)
    again = venvs.venv_for(DIGEST, tmp_path / "other", tmp_path / "venvs", baked)
    other = venvs.venv_for(OTHER, tmp_path / "other", tmp_path / "venvs", baked)
    assert first == again == tmp_path / "venvs" / DIGEST
    assert other == tmp_path / "venvs" / OTHER
    assert uv.calls == [_build(tmp_path / "tree", first), _build(tmp_path / "other", other)]


def test_a_failed_build_leaves_no_marker_and_builds_again(
    tmp_path: Path, uv: Uv, monkeypatch: pytest.MonkeyPatch
) -> None:
    baked = _baked(tmp_path, None)
    failing = Uv(fail=True)
    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(subprocess.CalledProcessError):
        venvs.venv_for(DIGEST, tmp_path / "tree", tmp_path / "venvs", baked)
    assert not (tmp_path / "venvs" / DIGEST / "lockfile.sha256").exists()
    monkeypatch.setattr(subprocess, "run", uv)
    assert venvs.venv_for(DIGEST, tmp_path / "tree", tmp_path / "venvs", baked) == tmp_path / "venvs" / DIGEST
    assert failing.calls == uv.calls == [_build(tmp_path / "tree", tmp_path / "venvs" / DIGEST)]
