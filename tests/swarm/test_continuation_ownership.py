import subprocess
from functools import partial

import pytest

from scripts.handoff import envelope
from scripts.swarm import continuation, prompt, stack
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.test_tick import store as store

REPO = "https://github.com/the-cloud-clockwork/agentihooks"
HEAD = "a" * 40
CASES = [
    ("t576", "master-323133-0113", "2942", "engineer@323133-1452", "t725", "3261"),
    ("t688", "engineer-323133-1424", "3186", "engineer@323133-1443", "t733", "3256"),
]


def _origin(repo):
    assert repo == "/repo"
    return stack.repo_key(REPO)


def _launch(store, monkeypatch, case, *, recorded=True, missing=False, handoff=False, setup=None):
    task, branch, pr, prior, foreign_task, foreign_pr = case
    foreign = prior.replace("@", "-")
    ledger = FakeLedger(
        [
            {
                "id": task,
                "title": "Continue claimed work",
                "branch": branch if recorded else "",
                "branch_repo": REPO,
                "pr_url": f"{REPO}/pull/{pr}",
            },
            {
                "id": foreign_task,
                "branch": foreign,
                "branch_repo": REPO,
                "pr_url": f"{REPO}/pull/{foreign_pr}",
                "state": "done",
            },
        ]
    )
    if setup:
        setup(ledger.rows[task])
    store.put_agent("sw", AgentRecord(prior, "eng", task, started_at=1))
    store.drop_agent("sw", prior, at=2)
    heads = {foreign: "b" * 40}
    if not missing:
        heads[branch] = HEAD

    def run(argv, **kwargs):
        assert argv[2] == "/repo"
        if argv[3] == "worktree":
            return subprocess.CompletedProcess(argv, 0, f"worktree /prior\nbranch refs/heads/{foreign}\n", "")
        if argv[3] == "rev-parse":
            return subprocess.CompletedProcess(argv, 0, f"origin/{foreign}\n", "")
        assert argv[3] == "ls-remote"
        candidate = argv[-1].removeprefix("refs/heads/")
        output = f"{heads[candidate]}\t{argv[-1]}\n" if candidate in heads else ""
        return subprocess.CompletedProcess(argv, 0 if output else 2, output, "")

    monkeypatch.setattr("scripts.swarm.tick.reclaim", partial(envelope.reclaim, run=run))
    monkeypatch.setattr(stack, "origin_key", _origin)
    if handoff:
        saved = branch if handoff == "recorded" else foreign
        store.put_handoff(
            "sw",
            task,
            "Continue the claimed task",
            envelope={
                "task": task,
                "remote_branch": saved,
                "remote_head": "b" * 40,
                "continue_from": f"origin/{saved}",
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
    assert launched["reclaim"]["fresh_reason"] == (
        "no earlier life pushed a branch: checked master-323133-0113 on the remote"
        if recorded
        else "no branch is recorded for this task outside another task's pull request"
    )
    assert "Start your worktree fresh from dev:" in rendered
    assert "HEAD:engineer-323133-1452" not in rendered


def test_stale_handoff_never_offers_a_foreign_push_target(store, monkeypatch):
    _, rendered = _launch(store, monkeypatch, CASES[1], handoff=True)
    assert "HEAD:engineer-323133-1424" in rendered
    assert "HEAD:engineer-323133-1443" not in rendered


@pytest.mark.parametrize(
    "changes,other,envelope_data,expected",
    [
        ({}, {}, {}, "origin/engineer-a1b2c3-0001"),
        ({"branch": "recorded"}, {}, {}, "origin/recorded"),
        ({}, {"id": "other", "branch": "engineer-a1b2c3-0001", "pr_url": f"{REPO}/pull/2"}, {}, "fresh"),
        ({"branch": "recorded"}, {"id": "other", "branch": "recorded", "pr_url": f"{REPO}/pull/2"}, {}, "fresh"),
        (
            {},
            {"id": "other", "branch": "engineer-a1b2c3-0001", "pr_url": f"{REPO}/pull/1"},
            {},
            "origin/engineer-a1b2c3-0001",
        ),
        ({}, {"id": "other", "branch": "engineer-a1b2c3-0001"}, {}, "origin/engineer-a1b2c3-0001"),
        (
            {},
            {"id": "other", "branch": "engineer-a1b2c3-0001", "pr_url": "https://github.com/other/repo/pull/2"},
            {},
            "origin/engineer-a1b2c3-0001",
        ),
        ({"branch_repo": "https://github.com/other/repo"}, {}, {}, "fresh"),
        (
            {},
            {},
            {"task": "t1", "continue_from": "origin/engineer-a1b2c3-0001", "remote_head": HEAD},
            "origin/engineer-a1b2c3-0001",
        ),
        ({"branch": "recorded"}, {}, {"task": "other", "continue_from": "origin/foreign"}, "origin/recorded"),
    ],
)
def test_task_continuation_respects_recorded_ownership(store, monkeypatch, changes, other, envelope_data, expected):
    task = {"id": "t1", "pr_url": f"{REPO}/pull/1", **changes}
    rows = {"t1": task, **({"other": other} if other else {})}
    monkeypatch.setattr(stack, "origin_key", _origin)

    def run(argv, **kwargs):
        assert argv[3] == "ls-remote"
        return subprocess.CompletedProcess(argv, 0, f"{HEAD}\t{argv[-1]}\n", "")

    result = continuation.resolve(
        "/repo", task, rows, ["engineer@a1b2c3-0001"], envelope_data, partial(envelope.reclaim, run=run)
    )
    assert result["continue_from"] == expected
    if expected == "fresh":
        assert result["fresh_reason"] != "none"


def test_repaired_handoff_keeps_its_transfer_facts(store, monkeypatch):
    task = {"id": "t1", "branch": "recorded"}
    saved = {"task": "t1", "reason": "quota", "seat": "eng-1@sw", "continue_from": "origin/foreign"}
    monkeypatch.setattr(stack, "origin_key", _origin)

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, f"{HEAD}\t{argv[-1]}\n", "")

    result = continuation.resolve("/repo", task, {"t1": task}, [], saved, partial(envelope.reclaim, run=run))
    assert result == {
        **saved,
        "remote_branch": "recorded",
        "remote_head": HEAD,
        "continue_from": "origin/recorded",
        "fresh_reason": "none",
    }


def test_matching_handoff_checks_that_its_recorded_branch_still_exists(store, monkeypatch):
    launched, rendered = _launch(store, monkeypatch, CASES[0], missing=True, handoff="recorded")
    assert launched["handoff_envelope"]["continue_from"] == "fresh"
    assert "HEAD:master-323133-0113" not in rendered


def test_foreign_repository_rejection_preserves_handoff_facts(store, monkeypatch):
    task = {"id": "t1", "branch": "recorded", "branch_repo": "https://github.com/other/repo"}
    saved = {"task": "t1", "reason": "quota", "seat": "eng-1@sw", "continue_from": "origin/recorded"}
    monkeypatch.setattr(stack, "origin_key", _origin)
    result = continuation.resolve("/repo", task, {"t1": task}, [], saved, envelope.reclaim)
    assert result == {
        **saved,
        "remote_branch": "none",
        "remote_head": "none",
        "continue_from": "fresh",
        "fresh_reason": "the task branch belongs to another repository",
    }


def test_parked_task_never_cuts_from_a_branch_registered_to_another_pull_request(store, monkeypatch):
    def parked(task):
        task.update(branch="engineer-323133-1452", parked_on=["t725"], stacked_base="base")

    launched, rendered = _launch(store, monkeypatch, CASES[0], setup=parked)
    assert launched["reclaim"]["continue_from"] == "fresh"
    assert "--from origin/engineer-323133-1452" not in rendered


def test_parked_task_keeps_its_valid_branch_and_restack_step(store, monkeypatch):
    def parked(task):
        task.update(parked_on=["t725"], stacked_base="base")

    _, rendered = _launch(store, monkeypatch, CASES[0], setup=parked)
    assert "--from origin/master-323133-0113" in rendered
    assert "restack" in rendered


@pytest.mark.parametrize("saved_task", ["t1", "other", None])
def test_handoff_picks_its_claimed_branch_among_multiple_prior_lives(store, monkeypatch, saved_task):
    task = {"id": "t1"}
    saved = {"continue_from": "origin/engineer-a1b2c3-0001"}
    if saved_task is not None:
        saved["task"] = saved_task
    monkeypatch.setattr(stack, "origin_key", _origin)

    def run(argv, **kwargs):
        assert argv[:6] == ["git", "-C", "/repo", "ls-remote", "--exit-code", "origin"]
        return subprocess.CompletedProcess(argv, 0, f"{HEAD}\t{argv[-1]}\n", "")

    result = continuation.resolve(
        "/repo",
        task,
        {"t1": task},
        ["engineer@a1b2c3-0002", "engineer@a1b2c3-0001"],
        saved,
        partial(envelope.reclaim, run=run),
    )
    expected = "origin/engineer-a1b2c3-0002" if saved_task == "other" else "origin/engineer-a1b2c3-0001"
    assert result["continue_from"] == expected


@pytest.mark.parametrize(
    "branch_repo,pr_repo,expected",
    [
        ("https://github.com/other/repo", REPO, "origin/engineer-a1b2c3-0001"),
        (REPO, "https://github.com/other/repo", "fresh"),
    ],
)
def test_branch_repository_identity_takes_precedence_over_pull_request_repository(
    store,
    monkeypatch,
    branch_repo,
    pr_repo,
    expected,
):
    task = {"id": "t1", "pr_url": f"{REPO}/pull/1"}
    other = {"id": "other", "branch": "engineer-a1b2c3-0001", "branch_repo": branch_repo, "pr_url": f"{pr_repo}/pull/2"}
    monkeypatch.setattr(stack, "origin_key", _origin)

    def run(argv, **kwargs):
        assert argv[2] == "/repo"
        return subprocess.CompletedProcess(argv, 0, f"{HEAD}\t{argv[-1]}\n", "")

    result = continuation.resolve(
        "/repo", task, {"t1": task, "other": other}, ["engineer@a1b2c3-0001"], {}, partial(envelope.reclaim, run=run)
    )
    assert result["continue_from"] == expected


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ({"handoff_envelope": {"continue_from": "origin/engineer-a1b2c3-0001"}}, "engineer-a1b2c3-0001"),
        ({"reclaim": {"continue_from": "origin/engineer-a1b2c3-0002"}}, "engineer-a1b2c3-0002"),
        (
            {
                "handoff_envelope": {"continue_from": "origin/engineer-a1b2c3-0001"},
                "reclaim": {"continue_from": "origin/engineer-a1b2c3-0002"},
            },
            "engineer-a1b2c3-0001",
        ),
        ({"handoff_envelope": None, "reclaim": {"continue_from": "origin/origin/x"}}, "origin/x"),
        ({"handoff_envelope": {"continue_from": None}}, ""),
        ({"handoff_envelope": {}}, ""),
        ({}, ""),
        ({"handoff_envelope": {"continue_from": "fresh"}}, ""),
        ({"handoff_envelope": {"continue_from": "upstream/engineer-a1b2c3-0001"}}, ""),
        ({"handoff_envelope": {"continue_from": "engineer-a1b2c3-0001"}}, ""),
        *[({"reclaim": {"continue_from": f"origin/{name}"}}, "") for name in ("dev", "main", "master", "v1")],
    ],
)
def test_retained_names_the_predecessor_branch_a_continuation_carries_on(task, expected):
    assert continuation.retained(task) == expected
