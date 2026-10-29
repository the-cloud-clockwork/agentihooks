import subprocess

import pytest

from scripts import install
from scripts.profiles import bundles

_FLAGS = {"root": {}, "user": {"user": True}, "shared": {"shared": True}}
_GETTERS = {"root": "_get_bundle_path", "user": "_get_user_bundle_path", "shared": "_get_shared_bundle_path"}


@pytest.fixture
def slots(tmp_path, monkeypatch):
    dirs = {}
    for slot, getter in _GETTERS.items():
        dirs[slot] = tmp_path / slot
        (dirs[slot] / ".git").mkdir(parents=True)
        monkeypatch.setattr(install, getter, lambda path=dirs[slot]: path)
    printed = []
    monkeypatch.setattr(install, "_cprint", printed.append)
    return dirs, printed


@pytest.fixture
def git(monkeypatch):
    calls = []
    result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def run(cmd, **kwargs):
        calls.append(cmd)
        return result

    monkeypatch.setattr(bundles.subprocess, "run", run)
    return calls, result


@pytest.mark.parametrize(("slot", "label"), [("root", "bundle"), ("user", "user bundle"), ("shared", "shared bundle")])
def test_pull_with_nothing_linked_in_the_requested_slot_names_that_slot(slots, monkeypatch, capsys, slot, label):
    monkeypatch.setattr(install, _GETTERS[slot], lambda: None)

    with pytest.raises(SystemExit) as exit_:
        bundles.pull(**_FLAGS[slot])

    assert exit_.value.code == 1
    assert capsys.readouterr().err.strip() == f"No {label} linked."


@pytest.mark.parametrize("slot", ["root", "shared", "user"])
def test_pull_refuses_a_linked_directory_that_is_not_a_git_repository(slots, capsys, slot):
    dirs, _ = slots
    (dirs[slot] / ".git").rmdir()

    with pytest.raises(SystemExit) as exit_:
        bundles.pull(**_FLAGS[slot])

    assert exit_.value.code == 1
    assert f"ERROR: {dirs[slot]} is not a git repository." in capsys.readouterr().err


@pytest.mark.parametrize("slot", ["root", "shared", "user"])
@pytest.mark.parametrize("rebase", [False, True])
def test_pull_runs_git_pull_in_the_requested_slot(slots, git, capsys, slot, rebase):
    dirs, printed = slots
    calls, _ = git

    bundles.pull(rebase=rebase, **_FLAGS[slot])

    cmd = ["git", "-C", str(dirs[slot]), "pull"] + (["--rebase"] if rebase else [])
    assert calls == [cmd]
    out = capsys.readouterr().out
    assert f"Pulling bundle: {dirs[slot]}" in out
    assert f"  $ {' '.join(cmd)}" in out
    assert printed == ["[OK] Bundle updated."]


def test_a_failed_git_pull_exits_and_reports_its_output(slots, git, capsys):
    _, printed = slots
    _, result = git
    result.returncode, result.stdout, result.stderr = 1, "fetched\n", "rejected\n"

    with pytest.raises(SystemExit) as exit_:
        bundles.pull(shared=True)

    assert exit_.value.code == 1
    captured = capsys.readouterr()
    assert "fetched\nrejected\n" in captured.out
    assert "ERROR: git pull failed." in captured.err
    assert printed == []
