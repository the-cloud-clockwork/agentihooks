"""Tests for hooks.context.prepush_guard."""

import subprocess

import pytest

from hooks.context import prepush_guard
from hooks.context.prepush_guard import check_prepush
from hooks.hook_manager import BlockAction
from scripts import ci_prepush

pytestmark = pytest.mark.usefixtures("outside_a_guarded_repository")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def _init(root, guarded=True):
    root.mkdir(parents=True)
    if guarded:
        (root / "scripts" / "ci_prepush").mkdir(parents=True)
        (root / "scripts" / "ci_prepush" / "__init__.py").write_text("")
    _git(root, "init", "-q", "-b", "work")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "commit", "-q", "--allow-empty", "-m", "base")
    return root


@pytest.fixture
def repo(tmp_path):
    return _init(tmp_path / "repo")


def _bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def _stamp(root):
    ci_prepush.stamp_path(root).write_text(_git(root, "rev-parse", "HEAD") + "\n")


def _message(root):
    return (
        f"BLOCKED: HEAD in {root} has not passed `python -m scripts.ci_prepush`. "
        "Run it there, fix what fails, commit, and push once it passes."
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push -u origin HEAD",
        "git push origin work:work",
        "echo hi && git push --set-upstream origin work",
        "if true; then git push origin HEAD; fi",
        "env GIT_TRACE=0 git push origin HEAD",
        "/usr/bin/git push origin HEAD",
        "timeout 300 git push origin HEAD",
        "git -c user.name=x push origin HEAD",
        "git push origin HEAD:work :stale",
        "git push origin HEAD:feature/diffcheck/x",
    ],
)
def test_push_without_a_passing_run_is_blocked(repo, command):
    with pytest.raises(BlockAction) as blocked:
        check_prepush(_bash(command, repo))

    assert str(blocked.value) == _message(repo)


@pytest.mark.parametrize(
    "prefix",
    [
        "if",
        "then",
        "else",
        "elif",
        "do",
        "while",
        "until",
        "time",
        "exec",
        "nohup",
        "env",
        "command",
        "!",
        "{",
        "A=1",
        "timeout -s TERM -k 5s 30s",
        "sudo -u worker",
        "nice -n 10",
        "env -u UNUSED A=1",
    ],
)
def test_every_shell_prefix_is_looked_through(repo, prefix):
    with pytest.raises(BlockAction):
        check_prepush(_bash(f"{prefix} git push origin HEAD", repo))


def test_push_from_another_directory_resolves_the_repository(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_DIR", str(repo))
    monkeypatch.setenv("HOME", str(tmp_path))
    for command in (
        f"cd {repo} && git push",
        f"git -C {repo} push origin HEAD",
        "git -C $REPO_DIR push origin HEAD",
        "git -C ~/repo push origin HEAD",
        "cd ~/repo && git push",
        "cd $REPO_DIR && git push",
    ):
        with pytest.raises(BlockAction):
            check_prepush(_bash(command, tmp_path))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(BlockAction):
        check_prepush(_bash(f"git -C {tmp_path} -C repo push", elsewhere))


@pytest.mark.parametrize(
    "command",
    [
        "git status && cd {worktree} && git push",
        "cd {worktree}\ngit push -u origin HEAD",
        "cd ../worktree && git push",
        "cd .. && git -C worktree push origin HEAD",
        "cd /nowhere-at-all; cd {worktree} && git push",
    ],
)
def test_the_push_is_graded_in_the_folder_the_command_changes_into(repo, tmp_path, command):
    worktree = _init(tmp_path / "worktree")
    _stamp(worktree)

    check_prepush(_bash(command.format(worktree=worktree), repo))

    _stamp(repo)
    _git(worktree, "commit", "-q", "--allow-empty", "-m", "more")
    with pytest.raises(BlockAction) as blocked:
        check_prepush(_bash(command.format(worktree=worktree), repo))
    assert str(blocked.value) == _message(worktree)


@pytest.mark.parametrize("workdir", ["{worktree}", "../worktree"])
def test_the_push_is_graded_in_the_shell_tool_workdir(repo, tmp_path, workdir):
    worktree = _init(tmp_path / "worktree")
    _stamp(worktree)
    payload = _bash("git push origin HEAD", repo)
    payload["tool_input"]["workdir"] = workdir.format(worktree=worktree)

    check_prepush(payload)

    _stamp(repo)
    _git(worktree, "commit", "-q", "--allow-empty", "-m", "more")
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(worktree)


def test_a_later_push_in_the_same_command_is_checked(repo, tmp_path):
    other = _init(tmp_path / "other", guarded=False)

    with pytest.raises(BlockAction):
        check_prepush(_bash(f"git -C {other} push && git push origin --delete old && git push", repo))


def test_push_of_a_passing_head_is_allowed(repo):
    _stamp(repo)

    check_prepush(_bash("git push -u origin HEAD", repo))


def test_a_commit_after_the_run_blocks_again(repo):
    _stamp(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "more")

    with pytest.raises(BlockAction):
        check_prepush(_bash("git push", repo))


@pytest.mark.parametrize(
    "command",
    [
        "git push origin --delete work",
        "git push origin -d work",
        "git push origin :work",
        "git push origin HEAD:diffcheck/plant",
        "git push -u origin HEAD:refs/heads/diffcheck/plant",
        "git push --dry-run origin HEAD",
        "git push -n origin HEAD",
        "git status",
        "git -C . status",
        "git",
        "git commit -m 'then git push'",
        "echo git push",
        "make push",
        "cat <<'EOF' > notes.md\ngit push\nEOF",
    ],
)
def test_deletes_plants_dry_runs_and_other_commands_pass(repo, command):
    check_prepush(_bash(command, repo))


def test_a_repository_without_the_command_is_not_guarded(tmp_path):
    root = _init(tmp_path / "other", guarded=False)

    check_prepush(_bash("git push", root))
    check_prepush(_bash("git push", tmp_path / "missing"))


def test_a_guard_error_lets_the_push_through_and_is_logged(repo, monkeypatch):
    logged = []
    monkeypatch.setattr(prepush_guard, "_toplevel", lambda _: (_ for _ in ()).throw(OSError("no git")))
    monkeypatch.setattr(prepush_guard, "log", lambda *args: logged.append(args))

    check_prepush(_bash("git push", repo))

    assert logged == [("prepush_guard failed", {"error": "no git"})]


def test_pre_tool_use_runs_the_guard_on_bash(repo, monkeypatch):
    from hooks import config, hook_manager

    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    for flag in ("QUOTA_POLICY_ENABLED", "BROADCAST_ENABLED", "ENFORCEMENT_INJECTION_ENABLED", "RETRY_BREAKER_ENABLED"):
        monkeypatch.setattr(config, flag, False)

    with pytest.raises(BlockAction, match="python -m scripts.ci_prepush"):
        hook_manager.on_pre_tool_use({"session_id": "session", **_bash("git push -u origin HEAD", repo)})


def test_cmd_payloads_are_checked_for_pushes(repo):
    payload = _bash("", repo)
    payload["tool_input"] = {"cmd": "git push origin HEAD"}
    with pytest.raises(BlockAction):
        check_prepush(payload)
