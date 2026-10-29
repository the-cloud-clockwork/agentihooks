import pytest

from scripts.gates import push_stop
from scripts.gates.progress import Progress
from tests.gates.test_push_stop import BRANCH, ME, NOW, SLUG, git, wait_on
from tests.gates.test_push_stop import rig as rig

RUN = "https://github.com/o/r/actions/runs/123"


def preflight(rig):
    head = rig.commit()
    git(rig.tree, "push", "-u", "origin", BRANCH)
    Progress(rig.store.redis, SLUG).outcome(ME, "pushed", NOW - 10)
    return {"kind": "mutation", "target": RUN, "task": "t1", "head": head}


def test_clean_pushed_work_without_a_pull_request_passes_during_its_preflight(rig):
    wait_on(rig, preflight(rig))
    assert rig.ledger.task["pr_url"] == ""
    assert rig.stop().allowed
    assert rig.inbox() == []


@pytest.mark.parametrize(
    "change",
    [
        {"task": "other"},
        {"task": ""},
        {"head": "stale"},
        {"head": ""},
        {"kind": "checks"},
        {"target": "https://github.com/other/repo/actions/runs/123"},
        {"target": "not an Actions run"},
    ],
)
def test_a_foreign_or_stale_preflight_does_not_release_progress_debt(rig, change):
    wait_on(rig, {**preflight(rig), **change})
    assert not rig.stop().allowed


def test_an_expired_preflight_does_not_release_progress_debt(rig):
    wait_on(rig, preflight(rig), at=NOW - 20, until=NOW)
    assert not rig.stop().allowed


def test_a_preflight_does_not_release_work_in_a_non_github_repository(rig):
    on = preflight(rig)
    git(rig.tree, "remote", "set-url", "origin", str(rig.origin))
    wait_on(rig, on)
    assert not rig.stop().allowed


def test_a_preflight_does_not_release_dirty_files(rig):
    wait_on(rig, preflight(rig))
    (rig.tree / "work").write_text("dirty")
    assert not rig.stop().allowed


def test_a_preflight_does_not_release_a_refused_unpushed_commit(rig, monkeypatch):
    on = preflight(rig)
    on["head"] = rig.commit("more")
    monkeypatch.setattr(push_stop, "push", lambda *args: False)
    wait_on(rig, on)
    assert not rig.stop().allowed
    assert push_stop.count(rig.tree, "HEAD", "--not", "--remotes=origin") == 1


def other_repo(rig):
    origin, seed = rig.root.parent / "other.git", rig.root.parent / "other-seed"
    git(rig.root.parent, "init", "--bare", "-b", "dev", str(origin))
    git(rig.root.parent, "clone", str(origin), str(seed))
    (seed / "readme").write_text("other\n")
    git(seed, "add", "readme")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", "origin", "HEAD:refs/heads/dev")
    tree = rig.root / "other" / BRANCH
    git(seed, "worktree", "add", "-b", BRANCH, str(tree), "origin/dev")
    (tree / "chart").write_text("chart")
    git(tree, "add", "chart")
    git(tree, "commit", "-m", "chart")
    git(tree, "push", "-u", "origin", BRANCH)
    git(seed, "remote", "set-url", "origin", "https://github.com/o/other.git")
    git(seed, "remote", "set-url", "--push", "origin", str(origin))
    return tree


def test_a_matching_preflight_releases_progress_debt_across_the_agents_other_repositories(rig):
    on = preflight(rig)
    other_repo(rig)
    wait_on(rig, on)
    assert rig.stop().allowed
    assert rig.inbox() == []


def test_a_stale_preflight_still_refuses_progress_debt_with_another_repository(rig):
    on = preflight(rig)
    other_repo(rig)
    wait_on(rig, {**on, "head": "stale"})
    assert not rig.stop().allowed
