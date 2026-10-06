import json

import pytest

from scripts import claude_trust


def _config(tmp_path, trusted):
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"projects": {str(path): {"hasTrustDialogAccepted": True} for path in trusted}}))
    return config


def _accepted(config, directory):
    return json.loads(config.read_text())["projects"].get(str(directory), {}).get("hasTrustDialogAccepted")


@pytest.mark.parametrize("git_marker", ["dir", "file"])
def test_a_git_repo_under_a_trusted_parent_is_marked(tmp_path, git_marker):
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    if git_marker == "dir":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_text("gitdir: elsewhere\n")
    config = _config(tmp_path, [tmp_path / "work"])

    assert claude_trust.ensure_trusted(repo, {"HOME": str(tmp_path)}) == ("marked", "")
    assert _accepted(config, repo) is True


def test_a_subfolder_of_a_trusted_repo_is_left_as_is(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    config = _config(tmp_path, [repo])
    original = config.read_text()

    assert claude_trust.ensure_trusted(repo / "sub", {"HOME": str(tmp_path)}) == ("trusted", "")
    assert config.read_text() == original


def test_a_plain_folder_under_a_trusted_parent_is_left_as_is(tmp_path):
    folder = tmp_path / "work" / "plain"
    folder.mkdir(parents=True)
    config = _config(tmp_path, [tmp_path / "work"])
    original = config.read_text()

    assert claude_trust.ensure_trusted(folder, {"HOME": str(tmp_path)}) == ("trusted", "")
    assert config.read_text() == original
