import subprocess

import pytest

from scripts.swarm import cli, publish, stack
from tests.swarm.test_cli import env

_fixture = env
AGENT = "engineer@a1b2c3-0001"
BRANCH = "engineer-a1b2c3-0001"
PUSHED = publish.pushed_branches


def _git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _repository(tmp_path, name):
    origin, primary = tmp_path / f"{name}.git", tmp_path / name
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "-b", "dev", str(primary))
    for key, value in (("user.email", "test@example.invalid"), ("user.name", "Test"), ("core.hooksPath", "/dev/null")):
        _git(primary, "config", key, value)
    _git(primary, "remote", "add", "origin", str(origin))
    _git(primary, "commit", "--allow-empty", "-m", "Initial")
    _git(primary, "push", "-u", "origin", "dev")
    top = tmp_path / "worktrees" / name / BRANCH
    _git(primary, "worktree", "add", "--no-track", "-b", BRANCH, str(top), "origin/dev")
    return origin.resolve(), primary.resolve(), top.resolve()


@pytest.fixture
def worktree(env, monkeypatch, tmp_path):
    _, ledger, _ = env
    origin, primary, top = _repository(tmp_path, "repo")
    monkeypatch.setattr(publish, "pushed_branches", PUSHED)
    monkeypatch.chdir(primary)
    cli.main(["sw", "create", "--repo", str(primary)])
    cli.main(["sw", "start"])
    return ledger, origin, primary, top


def block():
    return cli.main(["sw", "--as", AGENT, "block", "waiting on a token only the operator can create"])


def _refused(worktree, capsys):
    ledger, _, _, top = worktree
    capsys.readouterr()
    assert block() == 1
    assert capsys.readouterr().err == (
        f"swarm: the worktree {top} holds commits origin lacks; "
        f"push them with git -C {top} push -u origin {BRANCH} before blocking\n"
    )
    assert ledger.rows["t1"].get("state") != "blocked"
    assert "branch" not in ledger.rows["t1"]


def test_block_refuses_a_task_branch_never_pushed_that_holds_commits(worktree, capsys):
    _git(worktree[3], "commit", "--allow-empty", "-m", "Task work")
    _refused(worktree, capsys)


def test_block_refuses_commits_made_after_the_last_push(worktree, capsys):
    top = worktree[3]
    _git(top, "commit", "--allow-empty", "-m", "Task work")
    _git(top, "push", "-u", "origin", BRANCH)
    _git(top, "commit", "--allow-empty", "-m", "More task work")
    _refused(worktree, capsys)


def test_block_refuses_from_inside_the_task_worktree_too(worktree, capsys, monkeypatch):
    top = worktree[3]
    _git(top, "commit", "--allow-empty", "-m", "Task work")
    monkeypatch.chdir(top)
    _refused(worktree, capsys)


def test_block_records_the_pushed_branch_on_the_task(worktree, monkeypatch):
    ledger, origin, primary, top = worktree
    _git(top, "commit", "--allow-empty", "-m", "Task work")
    _git(top, "push", "-u", "origin", BRANCH)
    writers = []
    update = ledger.update_task
    monkeypatch.setattr(
        ledger,
        "update_task",
        lambda slug, task, fields, **kw: (
            writers.append((task, sorted(fields), kw.get("by"))) or update(slug, task, fields, **kw)
        ),
    )
    assert block() == 0
    row = ledger.rows["t1"]
    assert row["state"] == "blocked"
    assert (row["branch"], row["branch_repo"]) == (BRANCH, str(origin))
    assert row["repository_branches"] == {str(primary): BRANCH}
    assert ("t1", ["branch", "branch_repo", "repository_branches"], AGENT) in writers


@pytest.mark.parametrize("tracking", [True, False])
@pytest.mark.parametrize("ahead", [True, False])
def test_block_checks_the_published_continuation_branch(worktree, capsys, tracking, ahead):
    ledger, _, primary, top = worktree
    _git(top, "commit", "--allow-empty", "-m", "Predecessor work")
    _git(top, "push", "origin", "HEAD:predecessor")
    ledger.rows["t1"].update({"branch": "predecessor", "repository_branches": {str(primary): "predecessor"}})
    if tracking:
        _git(top, "branch", "--set-upstream-to", "origin/predecessor")
        ledger.rows["t1"].update({"branch": "stale", "repository_branches": {str(primary): "stale"}})
    if ahead:
        _git(top, "commit", "--allow-empty", "-m", "Unpublished continuation work")
        _git(top, "push", "origin", BRANCH)
    capsys.readouterr()
    assert block() == (1 if ahead else 0)
    if ahead:
        assert capsys.readouterr().err == (
            f"swarm: the worktree {top} holds commits origin lacks; "
            f"push them with git -C {top} push -u origin HEAD:predecessor before blocking\n"
        )
        assert ledger.rows["t1"]["state"] != "blocked"
    else:
        assert ledger.rows["t1"]["state"] == "blocked"


def test_block_checks_the_worktree_of_the_branch_recorded_for_its_repository(worktree, capsys):
    ledger, _, primary, _ = worktree
    top = (primary.parent / "worktrees" / "recorded").resolve()
    _git(primary, "worktree", "add", "--no-track", "-b", "eng-t1", str(top), "origin/dev")
    _git(top, "commit", "--allow-empty", "-m", "Task work")
    _git(primary, "worktree", "remove", "--force", str(primary.parent / "worktrees" / "repo" / BRANCH))
    ledger.rows["t1"].update({"branch": "stale", "repository_branches": {str(primary): "eng-t1"}})
    capsys.readouterr()
    assert block() == 1
    assert capsys.readouterr().err == (
        f"swarm: the worktree {top} holds commits origin lacks; "
        f"push them with git -C {top} push -u origin eng-t1 before blocking\n"
    )


def test_block_without_task_commits_blocks_and_records_no_branch(worktree):
    ledger = worktree[0]
    assert block() == 0
    assert ledger.rows["t1"]["state"] == "blocked"
    assert "branch" not in ledger.rows["t1"]


def test_block_without_a_task_worktree_blocks_as_before(worktree):
    ledger, _, primary, top = worktree
    _git(primary, "worktree", "remove", "--force", str(top))
    assert block() == 0
    assert ledger.rows["t1"]["state"] == "blocked"
    assert "branch" not in ledger.rows["t1"]


def _second_repository(worktree, tmp_path):
    ledger, _, primary, top = worktree
    origin, other, other_top = _repository(tmp_path, "antoncore")
    ledger.rows["t1"]["repositories"] = [str(primary), str(other)]
    _git(top, "commit", "--allow-empty", "-m", "Task work")
    _git(top, "push", "-u", "origin", BRANCH)
    return origin, other, other_top


def test_block_records_the_pushed_branch_of_every_task_repository(worktree, tmp_path):
    ledger, _, primary, _ = worktree
    other_origin, other, other_top = _second_repository(worktree, tmp_path)
    _git(other_top, "commit", "--allow-empty", "-m", "Antoncore work")
    _git(other_top, "push", "-u", "origin", BRANCH)
    assert block() == 0
    row = ledger.rows["t1"]
    assert (row["state"], row["branch"], row["branch_repo"]) == ("blocked", BRANCH, str(other_origin))
    assert row["repository_branches"] == {str(primary): BRANCH, str(other): BRANCH}


def test_block_records_only_the_pushed_repository_when_another_has_no_task_commits(worktree, tmp_path):
    ledger, origin, primary, _ = worktree
    _second_repository(worktree, tmp_path)
    assert block() == 0
    row = ledger.rows["t1"]
    assert (row["branch"], row["branch_repo"]) == (BRANCH, str(origin))
    assert row["repository_branches"] == {str(primary): BRANCH}


def test_block_refuses_when_a_second_task_repository_holds_unpushed_commits(worktree, tmp_path, capsys):
    ledger = worktree[0]
    _, _, other_top = _second_repository(worktree, tmp_path)
    _git(other_top, "commit", "--allow-empty", "-m", "Antoncore work")
    capsys.readouterr()
    assert block() == 1
    assert capsys.readouterr().err == (
        f"swarm: the worktree {other_top} holds commits origin lacks; "
        f"push them with git -C {other_top} push -u origin {BRANCH} before blocking\n"
    )
    assert ledger.rows["t1"]["state"] != "blocked" and "branch" not in ledger.rows["t1"]


@pytest.mark.parametrize(
    ("failing", "says"),
    [
        ("branch", "cannot read the worktree branch"),
        ("for-each-ref", "cannot read the worktree upstream"),
        ("rev-list", "cannot count the worktree commits"),
    ],
)
def test_block_refuses_when_a_worktree_git_read_fails(worktree, capsys, monkeypatch, failing, says):
    real = stack.shell

    def answer(argv):
        if argv[3:4] == [failing]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom\n")
        return real(argv)

    monkeypatch.setattr(stack, "shell", answer)
    capsys.readouterr()
    assert block() == 1
    assert capsys.readouterr().err == f"swarm: {says}: boom\n"
    assert worktree[0].rows["t1"].get("state") != "blocked"
