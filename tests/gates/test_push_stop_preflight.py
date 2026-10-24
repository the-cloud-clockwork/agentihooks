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
