import json
import subprocess
import sys

import pytest

from scripts import install


def _git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git("init", "-q", cwd=root)
    return root


def _run(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["agentihooks", *args])
    install.main()


def test_local_set_list_and_clear(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    _run(monkeypatch, "enforcement", "set", "--local", "project reminder", "10")
    created = capsys.readouterr().out
    assert "local, every 10 tool calls" in created

    store = repo / ".agentihooks" / "enforcements.json"
    entry = json.loads(store.read_text())["enforcements"][0]
    assert entry["message"] == "project reminder"
    assert entry["cadence"] == 10

    _run(monkeypatch, "enforcement", "list", "--local")
    listed = capsys.readouterr().out
    assert "[local]" in listed
    assert "project reminder" in listed

    _run(monkeypatch, "enforcement", "clear", "--local", "--id", entry["id"])
    assert "Cleared 1 enforcement" in capsys.readouterr().out
    assert json.loads(store.read_text()) == {"enforcements": []}


def test_local_command_outside_git_project_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["agentihooks", "enforcement", "list", "--local"])
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert exc.value.code == 1
    assert "not inside a Git project" in capsys.readouterr().err
    assert not (tmp_path / ".agentihooks").exists()
