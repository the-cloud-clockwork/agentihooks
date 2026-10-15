import subprocess
from functools import partial

import pytest

from scripts.handoff import envelope
from scripts.swarm import prompt, stack
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.xdist_group("fakeredis")

REPO = "https://github.com/the-cloud-clockwork/agentihooks"
HEAD = "a" * 40
CASES = [
    ("t576", "master-323133-0113", "2942", "engineer@323133-1452", "t725", "3261"),
    ("t688", "engineer-323133-1424", "3186", "engineer@323133-1443", "t733", "3256"),
]


def _launch(store, monkeypatch, case, *, recorded=True, missing=False, handoff=False):
    task, branch, pr, prior, foreign_task, foreign_pr = case
    foreign = prior.replace("@", "-")
    ledger = FakeLedger(
        [
            {"id": task, "branch": branch if recorded else "", "branch_repo": REPO, "pr_url": f"{REPO}/pull/{pr}"},
            {
                "id": foreign_task,
                "branch": foreign,
                "branch_repo": REPO,
                "pr_url": f"{REPO}/pull/{foreign_pr}",
                "state": "done",
            },
        ]
    )
    store.put_agent("sw", AgentRecord(prior, "eng", task, started_at=1))
    store.drop_agent("sw", prior, reason="retired")
    heads = {foreign: "b" * 40}
    if not missing:
        heads[branch] = HEAD

    def run(argv, **kwargs):
        if argv[3] == "worktree":
            return subprocess.CompletedProcess(argv, 0, f"worktree /prior\nbranch refs/heads/{foreign}\n", "")
        if argv[3] == "rev-parse":
            return subprocess.CompletedProcess(argv, 0, f"origin/{foreign}\n", "")
        assert argv[3] == "ls-remote"
        candidate = argv[-1].removeprefix("refs/heads/")
        output = f"{heads[candidate]}\t{argv[-1]}\n" if candidate in heads else ""
        return subprocess.CompletedProcess(argv, 0 if output else 2, output, "")

    monkeypatch.setattr("scripts.swarm.tick.reclaim", partial(envelope.reclaim, run=run))
    monkeypatch.setattr(stack, "origin_key", lambda repo: stack.repo_key(REPO))
    if handoff:
        store.put_handoff(
            "sw",
            task,
            "Continue the claimed task",
            envelope={
                "task": task,
                "remote_branch": foreign,
                "remote_head": "b" * 40,
                "continue_from": f"origin/{foreign}",
                "fresh_reason": "none",
            },
        )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=10_000)
    launched = runtime.tasks[-1]
    rendered = prompt.build("sw", "/repo", "eng", runtime.spawned[-1][1], launched)
    return launched, rendered


@pytest.mark.parametrize("case", CASES, ids=["launch1463", "launch1465"])
def test_reclaimed_launch_uses_its_recorded_task_branch(store, monkeypatch, case):
    launched, rendered = _launch(store, monkeypatch, case)
    branch, foreign = case[1], case[3].replace("@", "-")
    assert launched["reclaim"]["continue_from"] == f"origin/{branch}"
    assert f"HEAD:{branch}" in rendered
    assert f"--from origin/{branch}" in rendered
    assert f"HEAD:{foreign}" not in rendered


@pytest.mark.parametrize("recorded,missing", [(False, False), (True, True)])
def test_missing_task_branch_never_uses_a_foreign_pull_request(store, monkeypatch, recorded, missing):
    launched, rendered = _launch(store, monkeypatch, CASES[0], recorded=recorded, missing=missing)
    assert launched["reclaim"]["continue_from"] == "fresh"
    assert "Start your worktree fresh from dev:" in rendered
    assert "HEAD:engineer-323133-1452" not in rendered


def test_stale_handoff_never_offers_a_foreign_push_target(store, monkeypatch):
    _, rendered = _launch(store, monkeypatch, CASES[1], handoff=True)
    assert "HEAD:engineer-323133-1424" in rendered
    assert "HEAD:engineer-323133-1443" not in rendered
