import subprocess

import pytest

from hooks.context import project_resources


def _git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git("init", "-q", cwd=root)
    return root


def test_finds_git_root_from_nested_directory(repo):
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert project_resources.find_project_root(nested) == repo


def test_non_git_directory_has_no_project_root(tmp_path):
    assert project_resources.find_project_root(tmp_path) is None
    with pytest.raises(project_resources.ProjectResourceError, match="not inside a Git project"):
        project_resources.require_project_root(tmp_path)


def test_creates_resource_directory_only_when_requested(repo):
    path = project_resources.project_resource_dir(repo)
    assert not path.exists()
    assert project_resources.project_resource_dir(repo, create=True) == path
    assert path.is_dir()
    assert not (repo / ".gitignore").exists()
    assert "/.agentihooks/" not in (repo / ".git" / "info" / "exclude").read_text()


def test_resource_path_supports_safe_nested_resources(repo):
    path = project_resources.project_resource_path("future/config.json", repo, create_parent=True)
    assert path == repo / ".agentihooks" / "future" / "config.json"
    assert path.parent.is_dir()


@pytest.mark.parametrize("name", ["", "../escape.json", "/tmp/escape.json"])
def test_rejects_unsafe_resource_paths(repo, name):
    with pytest.raises(project_resources.ProjectResourceError, match="invalid project resource path"):
        project_resources.project_resource_path(name, repo)
