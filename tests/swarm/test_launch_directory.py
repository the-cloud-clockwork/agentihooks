from types import SimpleNamespace

import pytest

from scripts import init_agent
from scripts.swarm import naming
from scripts.swarm.runtime import HerdrRuntime

pytestmark = pytest.mark.unit


def _value(argv, flag):
    return argv[argv.index(flag) + 1]


def test_a_hosted_repository_identity_launches_in_the_hive_checkout(monkeypatch, tmp_path):
    root = tmp_path / "dev"
    checkout = root / "workbench" / "local-name"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:Org/Name.git\n')
    monkeypatch.setenv("HIVE_REPO_ROOT", str(root))
    config = SimpleNamespace(repo="github.com/org/name", code="c0de", slug="hosted")
    argv = HerdrRuntime(home=tmp_path / "home")._argv(config, "master@hosted", "claude", "open", "m.md", "master")
    assert _value(argv, "--dir") == str(checkout)
    assert _value(argv, "--workspace") == naming.space("github.com/org/name", "c0de", "hosted")
    assert init_agent._resolve_directory(_value(argv, "--dir"), {"HOME": str(tmp_path)}) == checkout


def test_a_local_repository_path_launches_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_REPO_ROOT", str(tmp_path / "empty"))
    config = SimpleNamespace(repo=str(tmp_path), code="c0de", slug="local")
    argv = HerdrRuntime(home=tmp_path / "home")._argv(config, "eng@local", "codex", "open", "e.md", "engineer")
    assert _value(argv, "--dir") == str(tmp_path)
