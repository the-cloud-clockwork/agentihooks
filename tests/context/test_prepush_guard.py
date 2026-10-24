"""Tests for hooks.context.prepush_guard."""

import subprocess
from pathlib import Path

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
        "Commit intended changes first, run the gate on that commit, then push. "
        f"Next: cd {root} && python -m scripts.ci_prepush"
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
        "cd -P {worktree} && git push",
        "cd && cd worktree && git push",
        "cd {worktree}; cd missing; git push",
        "env -C {worktree} git push",
        "cd {worktree}; cd ..; cd -; git push",
        "cd {worktree} && (git push)",
    ],
)
def test_the_push_is_graded_in_the_folder_the_command_changes_into(repo, tmp_path, monkeypatch, command):
    monkeypatch.setenv("HOME", str(tmp_path))
    worktree = _init(tmp_path / "worktree")
    _stamp(worktree)

    check_prepush(_bash(command.format(worktree=worktree), repo))

    _stamp(repo)
    _git(worktree, "commit", "-q", "--allow-empty", "-m", "more")
    with pytest.raises(BlockAction) as blocked:
        check_prepush(_bash(command.format(worktree=worktree), repo))
    assert str(blocked.value) == _message(worktree)


@pytest.mark.parametrize(
    "command",
    [
        "(cd {worktree}); git push",
        "(cd {worktree} && true) && git push",
        "cd {worktree} | true; git push",
        "cd {worktree} & git push",
        "env -C {worktree} true; git push",
        "cd {worktree} && cd - && git push",
        "cd -; git push",
    ],
)
def test_a_folder_change_that_does_not_last_leaves_the_push_where_it_started(repo, tmp_path, command):
    worktree = _init(tmp_path / "worktree")
    _stamp(worktree)

    with pytest.raises(BlockAction) as blocked:
        check_prepush(_bash(command.format(worktree=worktree), repo))
    assert str(blocked.value) == _message(repo)


@pytest.mark.parametrize("workdir", ["{worktree}", "../worktree"])
def test_the_push_is_graded_in_the_shell_tool_workdir(repo, tmp_path, workdir):
    worktree = _init(tmp_path / "worktree")
    _stamp(worktree)
    payload = _bash("", repo)
    payload["tool_input"] = {"cmd": "git push origin HEAD", "workdir": workdir.format(worktree=worktree)}

    check_prepush(payload)

    _stamp(repo)
    _git(worktree, "commit", "-q", "--allow-empty", "-m", "more")
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(worktree)


@pytest.fixture
def swarm_worktree(repo, tmp_path, monkeypatch):
    from tests.swarm_ledger import legacy_page

    _git(repo, "add", "scripts/ci_prepush/__init__.py")
    _git(repo, "commit", "-q", "-m", "guard")
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-q", "-b", "task-branch", str(worktree))
    task = {"id": "push", "state": "claimed", "claimed_by": "engineer@test", "branch": "task-branch"}
    folder = tmp_path / "ledgers"
    folder.mkdir()
    legacy_page.store(folder, "rig", {"tasks": [task]})
    monkeypatch.setenv("LEDGER_DIR", str(folder))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "rig")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "push")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@test")
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "worktrees"))
    return worktree


def test_codex_command_only_push_grades_the_claimed_task_worktree(repo, swarm_worktree):
    _stamp(swarm_worktree)
    payload = _bash("git push origin HEAD", repo)

    check_prepush(payload)

    _stamp(repo)
    _git(swarm_worktree, "commit", "-q", "--allow-empty", "-m", "more")
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(swarm_worktree)


@pytest.mark.parametrize(
    "fields",
    [
        {"command": "cd ../repo && git push origin HEAD"},
        {"command": "git -C . push origin HEAD"},
        {"command": "git push origin HEAD", "workdir": "."},
        {"command": "git push origin HEAD", "workdir": "{repo}"},
    ],
)
def test_swarm_fallback_keeps_explicit_directory_selection(repo, swarm_worktree, fields):
    _stamp(swarm_worktree)
    payload = _bash("", repo)
    payload["tool_input"] = {key: value.format(repo=repo) for key, value in fields.items()}
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(repo)

    _stamp(repo)
    _git(swarm_worktree, "commit", "-q", "--allow-empty", "-m", "more")
    check_prepush(payload)


@pytest.mark.parametrize(
    "fields",
    [
        {"AGENTIHOOKS_SWARM": ""},
        {"AGENTIHOOKS_SWARM_TASK": ""},
        {"AGENTIHOOKS_AGENT_NAME": ""},
        {"AGENTIHOOKS_SWARM_TASK": "other"},
        {"AGENTIHOOKS_AGENT_NAME": "other"},
        {"LEDGER_DIR": "{tmp_path}/missing"},
    ],
)
def test_missing_swarm_binding_keeps_the_payload_directory(repo, swarm_worktree, tmp_path, monkeypatch, fields):
    for key, value in fields.items():
        monkeypatch.setenv(key, value.format(tmp_path=tmp_path))
    _stamp(swarm_worktree)
    payload = _bash("git push origin HEAD", repo)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(repo)

    _stamp(repo)
    _git(swarm_worktree, "commit", "-q", "--allow-empty", "-m", "more")
    check_prepush(payload)


@pytest.mark.parametrize(
    "fields",
    [{"id": "other"}, {"claimed_by": "other"}, {"state": "done"}, {"branch": None}, {"branch": "missing"}],
)
def test_missing_claimed_task_worktree_keeps_the_payload_directory(repo, swarm_worktree, tmp_path, fields):
    from tests.swarm_ledger import legacy_page

    task = {"id": "push", "state": "claimed", "claimed_by": "engineer@test", "branch": "task-branch", **fields}
    legacy_page.store(tmp_path / "ledgers", "rig", {"tasks": [task]})
    _stamp(swarm_worktree)
    payload = _bash("git push origin HEAD", repo)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(repo)

    _stamp(repo)
    _git(swarm_worktree, "commit", "-q", "--allow-empty", "-m", "more")
    check_prepush(payload)


def test_swarm_push_in_pr_state_uses_its_recorded_branch(repo, swarm_worktree, tmp_path):
    from tests.swarm_ledger import legacy_page

    task = {"id": "push", "state": "pr", "claimed_by": "engineer@test", "branch": "task-branch"}
    legacy_page.store(tmp_path / "ledgers", "rig", {"tasks": [task]})
    _stamp(repo)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(_bash("git push origin HEAD", repo))
    assert str(blocked.value) == _message(swarm_worktree)


@pytest.mark.parametrize("checkout", ["primary", "worktree"])
def test_a_push_from_a_second_repository_grades_only_that_repository(swarm_worktree, tmp_path, checkout):
    from tests.swarm_ledger import legacy_page

    task = {
        "id": "push",
        "state": "claimed",
        "claimed_by": "engineer@test",
        "branch": "task-branch",
        "worktree": str(swarm_worktree),
    }
    legacy_page.store(tmp_path / "ledgers", "rig", {"tasks": [task]})
    infra = _init(tmp_path / "infra")
    _git(infra, "add", "scripts/ci_prepush/__init__.py")
    _git(infra, "commit", "-q", "-m", "guard")
    if checkout == "worktree":
        infra_tree = tmp_path / "worktrees" / "infra" / "engineer-test-2"
        _git(infra, "worktree", "add", "-q", "-b", "engineer-test-2", str(infra_tree))
        infra = infra_tree
    payload = _bash("git push -u origin HEAD", infra)
    _stamp(swarm_worktree)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(infra)

    _stamp(infra)
    _git(swarm_worktree, "commit", "-q", "--allow-empty", "-m", "more")
    check_prepush(payload)


def test_a_worktree_shares_its_repository_common_dir(repo, swarm_worktree, tmp_path):
    assert prepush_guard._common_dir(swarm_worktree) == str(repo / ".git")
    assert prepush_guard._common_dir(repo) == str(repo / ".git")
    assert prepush_guard._common_dir(tmp_path) is None


@pytest.mark.parametrize("folder", ["engineer-test", "engineer-test-2"])
def test_a_first_push_before_the_branch_is_recorded_grades_the_agent_worktree(repo, swarm_worktree, tmp_path, folder):
    from tests.swarm_ledger import legacy_page

    root = tmp_path / "worktrees"
    own = root / "repo" / folder
    _git(repo, "worktree", "add", "-q", "-b", folder, str(own))
    _git(repo, "worktree", "add", "-q", "-b", "engineer-test-3", str(root / "other" / "engineer-test-3"))
    task = {"id": "push", "state": "claimed", "claimed_by": "engineer@test"}
    legacy_page.store(tmp_path / "ledgers", "rig", {"tasks": [task]})
    _stamp(repo)
    payload = _bash("git push -u origin HEAD", repo)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(own)

    _stamp(own)
    check_prepush(payload)


def test_two_agent_worktrees_of_the_repository_keep_the_payload_directory(repo, swarm_worktree, tmp_path):
    from tests.swarm_ledger import legacy_page

    for folder in ("engineer-test", "engineer-test-2"):
        _git(repo, "worktree", "add", "-q", "-b", folder, str(tmp_path / "worktrees" / "repo" / folder))
        _stamp(tmp_path / "worktrees" / "repo" / folder)
    task = {"id": "push", "state": "claimed", "claimed_by": "engineer@test"}
    legacy_page.store(tmp_path / "ledgers", "rig", {"tasks": [task]})
    payload = _bash("git push -u origin HEAD", repo)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(repo)


def test_a_swarm_push_outside_a_repository_passes_without_a_guard_error(swarm_worktree, tmp_path, monkeypatch):
    from tests.swarm_ledger import legacy_page

    logged = []
    monkeypatch.setattr(prepush_guard, "log", lambda *args: logged.append(args))
    task = {"id": "push", "state": "claimed", "claimed_by": "engineer@test"}
    legacy_page.store(tmp_path / "ledgers", "rig", {"tasks": [task]})
    outside = tmp_path / "outside"
    outside.mkdir()

    check_prepush(_bash("git push -u origin HEAD", outside))

    assert logged == []


@pytest.mark.parametrize("directory", [None, "", "~/development-ledger"])
def test_default_swarm_ledger_location_resolves_the_task_worktree(repo, swarm_worktree, monkeypatch, directory):
    from tests.swarm_ledger import legacy_page

    task = {"id": "push", "state": "claimed", "claimed_by": "engineer@test", "branch": "task-branch"}
    legacy_page.store(Path.home() / "development-ledger", "rig", {"tasks": [task]})
    if directory is None:
        monkeypatch.delenv("LEDGER_DIR")
    else:
        monkeypatch.setenv("LEDGER_DIR", directory)
    _stamp(repo)
    payload = _bash("git push origin HEAD", repo)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(swarm_worktree)

    _stamp(swarm_worktree)
    check_prepush(payload)


def test_task_without_a_claimant_cannot_supply_the_fallback_directory(repo, swarm_worktree, tmp_path, monkeypatch):
    from tests.swarm_ledger import legacy_page

    task = {"id": "push", "state": "claimed", "claimed_by": "", "branch": "task-branch"}
    legacy_page.store(tmp_path / "ledgers", "rig", {"tasks": [task]})
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "")
    _stamp(swarm_worktree)
    payload = _bash("git push origin HEAD", repo)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(repo)

    _stamp(repo)
    _git(swarm_worktree, "commit", "-q", "--allow-empty", "-m", "more")
    check_prepush(payload)


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


@pytest.mark.parametrize("directory", ["workdir", "task", "branch"])
def test_codex_push_in_bundle_worktree_uses_bundle_repository(repo, tmp_path, monkeypatch, directory):
    from hooks.targets.normalizer import normalize_payload
    from tests.swarm_ledger import legacy_page

    bundle = _init(tmp_path / "agentihooks-bundle")
    _git(bundle, "add", "scripts/ci_prepush/__init__.py")
    _git(bundle, "commit", "-q", "-m", "guard")
    folder_name = "continuation" if directory == "branch" else "engineer-test"
    worktree = tmp_path / "worktrees" / "agentihooks-bundle" / folder_name
    worktree.parent.mkdir(parents=True)
    _git(bundle, "worktree", "add", "-q", "-b", "engineer-test", str(worktree))
    task = {
        "id": "push",
        "state": "claimed",
        "claimed_by": "engineer@test",
        "branch": "engineer-test",
        "worktree": str(worktree),
    }
    folder = tmp_path / "ledgers"
    folder.mkdir()
    if directory == "branch":
        task.pop("worktree")
    legacy_page.store(folder, "rig", {"tasks": [task]})
    monkeypatch.setenv("LEDGER_DIR", str(folder))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "rig")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "push")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@test")
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    root = tmp_path / ("unused" if directory == "task" else "worktrees")
    monkeypatch.setenv("WORKTREE_ROOT", str(root))
    payload = {"tool_name": "exec_command", "tool_input": {"command": "git push -u origin HEAD"}, "cwd": str(repo)}
    if directory == "workdir":
        payload["tool_input"]["workdir"] = str(worktree)
    payload = normalize_payload(payload)
    _stamp(repo)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == _message(worktree)
    _stamp(worktree)
    check_prepush(payload)


@pytest.mark.parametrize("step", ["identity", "ledger", "claimant", "worktree"])
def test_codex_unresolved_push_refuses_with_directory_guidance(repo, swarm_worktree, tmp_path, monkeypatch, step):
    from hooks.targets.normalizer import normalize_payload
    from tests.swarm_ledger import legacy_page

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    if step == "identity":
        monkeypatch.setenv("AGENTIHOOKS_SWARM", "")
    elif step == "ledger":
        monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "missing"))
    elif step == "claimant":
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "other")
    else:
        task = {"id": "push", "state": "claimed", "claimed_by": "engineer@test", "branch": "missing"}
        legacy_page.store(tmp_path / "ledgers", "rig", {"tasks": [task]})
    payload = normalize_payload(
        {
            "tool_name": "exec_command",
            "tool_input": {"command": "git push -u origin HEAD"},
            "cwd": str(repo),
        }
    )
    _stamp(repo)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    message = str(blocked.value)
    stage = {
        "identity": "identity binding",
        "ledger": "ledger task",
        "claimant": "claimant or task state",
        "worktree": "worktree lookup",
    }[step]
    assert f"Could not resolve the push directory ({stage})" in message
    assert "git -C /absolute/path/to/worktree push" in message
    assert str(repo) not in message
    payload["tool_input"]["command"] = f"git -C {repo} push -u origin HEAD"
    check_prepush(payload)


@pytest.mark.parametrize("selection", ["workdir", "git-c"])
def test_codex_explicit_directory_without_a_repository_is_refused(repo, tmp_path, monkeypatch, selection):
    from hooks.targets.normalizer import normalize_payload

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    target = tmp_path / "not-a-repository"
    target.mkdir()
    tool_input = {"command": "git push -u origin HEAD"}
    if selection == "workdir":
        tool_input["workdir"] = str(target)
    else:
        tool_input["command"] = f"git -C {target} push -u origin HEAD"
    payload = normalize_payload({"tool_name": "exec_command", "tool_input": tool_input, "cwd": str(repo)})
    _stamp(repo)
    with pytest.raises(BlockAction) as blocked:
        check_prepush(payload)
    assert str(blocked.value) == (
        "BLOCKED: Could not resolve the push directory (repository lookup). "
        "Use git -C /absolute/path/to/worktree push with the intended repository."
    )


def _pushed(root, branch, *entries, remote="origin"):
    subprocess.run(["git", "-C", str(root), "remote", "add", remote, str(root)], capture_output=True)
    log_file = root / ".git" / "logs" / "refs" / "remotes" / remote / branch
    log_file.parent.mkdir(parents=True, exist_ok=True)
    zero = "0" * 40
    log_file.write_text("".join(f"{zero} {zero} t <t@example.com> {stamp} +0000\t{why}\n" for stamp, why in entries))


def _spacing(root, command, now):
    return prepush_guard._crowded(prepush_guard._pushes(_bash(command, root)), now)


def test_a_push_inside_the_spacing_names_when_it_may_go(repo):
    _pushed(repo, "work", (1000, "update by push"))
    assert _spacing(repo, "git push origin HEAD", 1120) == (
        "BLOCKED: the last push to work was 120 s ago and its Tests run is still running; pushing now cancels it. "
        "Keep committing locally and push work after 00:21:40 UTC, in 181 s, so one run grades every commit. "
        "Never leave the final commit unpushed."
    )


@pytest.mark.parametrize(("now", "blocked"), [(1000, True), (1299, True), (1300, False), (999, False)])
def test_the_spacing_holds_from_the_last_push_for_its_whole_window(repo, now, blocked):
    _pushed(repo, "work", (1000, "update by push"))
    assert (_spacing(repo, "git push", now) is not None) is blocked


def test_only_the_newest_push_entry_counts_and_fetches_are_ignored(repo):
    _pushed(repo, "work", (100, "update by push"), (1000, "update by push"), (1250, "fetch: fast-forward"))
    assert _spacing(repo, "git push", 1301) is None
    assert _spacing(repo, "git push", 1299) is not None


@pytest.mark.parametrize(
    ("command", "branch", "remote"),
    [
        ("git push origin HEAD:other", "other", "origin"),
        ("git push -u upstream +refs/heads/work:refs/heads/other", "other", "upstream"),
        ("git push --force-with-lease origin work", "work", "origin"),
        ("git push origin +work", "work", "origin"),
        ("git push origin refs/heads/work", "work", "origin"),
        ("git push -o ci.skip origin work", "work", "origin"),
        ("git push --push-option ci.skip origin :stale HEAD:other", "other", "origin"),
    ],
)
def test_the_spacing_reads_the_branch_and_remote_the_push_targets(repo, command, branch, remote):
    _pushed(repo, branch, (1000, "update by push"), remote=remote)
    assert f"the last push to {branch} was 10 s ago" in _spacing(repo, command, 1010)
    assert _spacing(repo, command.replace(f" {remote} ", " unrecorded "), 1010) is None


def test_a_branch_never_pushed_from_this_clone_goes_at_once(repo):
    assert _spacing(repo, "git push origin HEAD", 1000) is None


def test_zero_spacing_turns_the_check_off(repo, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_PUSH_SPACING_S", "0")
    _pushed(repo, "work", (1000, "update by push"))
    assert _spacing(repo, "git push", 1000) is None


def test_a_passed_push_inside_the_spacing_is_refused_and_a_delete_is_not(repo):
    _stamp(repo)
    _pushed(repo, "work", (prepush_guard.time.time(), "update by push"))
    with pytest.raises(BlockAction) as blocked:
        check_prepush(_bash("git push origin HEAD", repo))
    assert str(blocked.value).startswith("BLOCKED: the last push to work was ")
    check_prepush(_bash("git push origin --delete work", repo))


def test_an_unpassed_push_names_the_gate_before_the_spacing(repo):
    _pushed(repo, "work", (prepush_guard.time.time(), "update by push"))
    with pytest.raises(BlockAction) as blocked:
        check_prepush(_bash("git push origin HEAD", repo))
    assert str(blocked.value) == _message(repo)


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD:work :stale",
        "git push origin HEAD:work HEAD:diffcheck/plant",
    ],
)
def test_deleted_and_diffcheck_refspecs_in_a_mixed_push_are_not_spaced(repo, command):
    _pushed(repo, "stale", (1000, "update by push"))
    _pushed(repo, "diffcheck/plant", (1000, "update by push"))
    assert _spacing(repo, command, 1010) is None
    _pushed(repo, "work", (1000, "update by push"))
    assert "the last push to work was" in _spacing(repo, command, 1010)


@pytest.mark.parametrize(
    "command", ["git push -n origin work", "git push --dry-run origin work", "git push origin HEAD:diffcheck/x"]
)
def test_dry_runs_and_diffcheck_pushes_are_never_spaced(repo, command):
    _pushed(repo, "work", (1000, "update by push"))
    _pushed(repo, "diffcheck/x", (1000, "update by push"))
    assert _spacing(repo, command, 1010) is None


def test_a_bare_push_reads_the_remote_and_branch_it_pushes_to(repo):
    _git(repo, "config", "push.default", "current")
    _git(repo, "config", "remote.pushDefault", "upstream")
    _git(repo, "update-ref", "refs/remotes/upstream/work", "HEAD")
    _pushed(repo, "work", (1000, "update by push"), remote="upstream")
    assert "the last push to work was 10 s ago" in _spacing(repo, "git push", 1010)
    assert _spacing(repo, "git push origin", 1010) is None


def test_a_worktree_reads_the_push_reflog_of_its_repository(repo, swarm_worktree):
    _pushed(repo, "task-branch", (1000, "update by push"))
    assert "the last push to task-branch was 10 s ago" in prepush_guard._crowded(
        [(swarm_worktree, ["origin", "HEAD"])], 1010
    )


def test_a_push_to_a_path_rather_than_a_named_remote_is_not_spaced(repo, tmp_path):
    target = tmp_path / "other"
    _pushed(repo, "work", (1000, "update by push"))
    target.mkdir()
    zero = "0" * 40
    (target / "work").write_text(f"{zero} {zero} t <t@example.com> 1000 +0000\tupdate by push\n")
    assert _spacing(repo, f"git push {target} HEAD", 1010) is None
    assert _spacing(repo, "git push origin HEAD", 1010) is not None
