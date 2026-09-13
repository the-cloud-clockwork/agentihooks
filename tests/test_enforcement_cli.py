import json
import os
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
    assert "Active enforcements: 1" in listed
    assert "[1/1] LOCAL" in listed
    assert f"ID: {entry['id']}" in listed
    assert "Cadence: every 10 tool calls" in listed
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


def test_list_wraps_long_messages(monkeypatch, capsys):
    monkeypatch.setattr(install.shutil, "get_terminal_size", lambda fallback: os.terminal_size((60, 24)))
    install._print_enforcements(
        [
            {
                "source": "runtime",
                "id": "long-message",
                "cadence": 5,
                "tag": "doctrine",
                "message": "A long enforcement message that must wrap across terminal lines instead of overflowing.",
            }
        ]
    )
    output = capsys.readouterr().out
    assert "[1/1] RUNTIME" in output
    assert "    A long enforcement message that must wrap across" in output
    assert "    terminal lines instead of overflowing." in output
