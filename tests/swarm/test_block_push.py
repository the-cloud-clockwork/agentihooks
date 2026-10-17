import subprocess

import pytest

from scripts.swarm import cli, publish, stack
from tests.swarm.test_cli import env

pytestmark = pytest.mark.xdist_group("fakeredis")
_fixture = env
AGENT = "engineer@a1b2c3-0001"
BRANCH = "engineer-a1b2c3-0001"
PUSHED = getattr(publish, "pushed_branches", None)


def _git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def worktree(env, monkeypatch, tmp_path):
    _, ledger, _ = env
    origin, primary = tmp_path / "origin.git", tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "-b", "dev", str(primary))
    for key, value in (("user.email", "test@example.invalid"), ("user.name", "Test"), ("core.hooksPath", "/dev/null")):
        _git(primary, "config", key, value)
    _git(primary, "remote", "add", "origin", str(origin))
    _git(primary, "commit", "--allow-empty", "-m", "Initial")
    _git(primary, "push", "-u", "origin", "dev")
    top = tmp_path / "worktrees" / BRANCH
    _git(primary, "worktree", "add", "-b", BRANCH, str(top), "origin/dev")
    monkeypatch.setattr(publish, "pushed_branches", PUSHED)
    monkeypatch.chdir(primary)
    cli.main(["sw", "create", "--repo", str(primary)])
    cli.main(["sw", "start"])
    return ledger, origin.resolve(), primary.resolve(), top.resolve()


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


def test_block_records_the_pushed_branch_on_the_task(worktree):
    ledger, origin, primary, top = worktree
    _git(top, "commit", "--allow-empty", "-m", "Task work")
    _git(top, "push", "-u", "origin", BRANCH)
    assert block() == 0
    row = ledger.rows["t1"]
    assert row["state"] == "blocked"
    assert (row["branch"], row["branch_repo"]) == (BRANCH, str(origin))
    assert row["repository_branches"] == {str(primary): BRANCH}


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


@pytest.mark.parametrize(
    ("failing", "says"),
    [("branch", "cannot read the worktree branch"), ("rev-list", "cannot count the worktree commits")],
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
