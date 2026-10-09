import json
import subprocess
from dataclasses import replace

import pytest

from scripts.handoff.envelope import REASONS, build, reclaim
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

WORKTREE = "/wt/repo/engineer-a1b2c3-0001"
PR = "https://github.com/o/r/pull/9"
HEAD = "4f2a9c0e1b7d3a5c6e8f0a1b2c3d4e5f6a7b8c9d"


def _done(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


UPSTREAM = ["git", "-C", WORKTREE, "rev-parse", "--abbrev-ref", "@{upstream}"]
GIT = {"capture_output": True, "text": True}


def _ls_remote(branch):
    return ["git", "-C", WORKTREE, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"]


def _run(calls, upstream="", remote=(0, HEAD), raises=()):
    tracked = upstream.removeprefix("origin/") or "engineer-a1b2c3-0001"

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[3] in raises:
            raise OSError(argv[3])
        if argv == UPSTREAM:
            assert kwargs == {**GIT, "timeout": 10}
            return _done(f"{upstream}\n" if upstream else "", 0 if upstream else 128)
        if argv == _ls_remote(tracked):
            assert kwargs == {**GIT, "timeout": 20}
            return _done(f"{remote[1]}\t{argv[-1]}\n" if remote[0] == 0 else "", remote[0])
        if argv[:4] == ["git", "-C", "/repo", "worktree"]:
            return _done(
                f"worktree /repo\nbranch refs/heads/dev\n\nworktree {WORKTREE}\nbranch refs/heads/engineer-a1b2c3-0001\n"
            )
        if argv[:3] == ["git", "-C", WORKTREE]:
            return _done("engineer-a1b2c3-0001\n")
        if argv[:3] == ["gh", "pr", "view"]:
            checks = [
                {"conclusion": "SUCCESS"},
                {"conclusion": "FAILURE"},
                {"state": "PENDING"},
                {"conclusion": "SKIPPED"},
            ]
            return _done(json.dumps({"state": "OPEN", "statusCheckRollup": checks}))
        return _done(code=1)

    return run


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    return store


def _agent():
    return AgentRecord(
        "engineer@a1b2c3-0001",
        "eng",
        "t1",
        seat="eng-1@sw",
        conversation_id="conv-7",
        profile="engineer",
        harness="codex",
        model="gpt-6.1-sol",
        effort="medium",
        account="primary",
    )


def test_the_envelope_carries_every_fact_the_runtime_holds(store):
    agent = _agent()
    store.claim("sw", "t1", agent.name, 60_000)
    store.claim("sw", "t2", "someone-else", 60_000)
    inbox = InboxStore(store.redis)
    open_item = inbox.send("ci@a1b2c3-0001", agent.name, "contract confirmed")
    closed = inbox.send("ci@a1b2c3-0001", agent.name, "old")
    inbox.close(closed.id, agent.name, "done", "handled the request")
    rows = [{"id": "t1", "phase": "p7", "pr_url": PR}, {"id": "t2", "phase": "p7"}]
    calls = []
    envelope = build(store, "sw", agent, "recycle", rows, 1_791_300_000_000, run=_run(calls))
    assert envelope == {
        "seat": "eng-1@sw",
        "agent": "engineer@a1b2c3-0001",
        "reason": "recycle",
        "task": "t1",
        "phase": "p7",
        "ledger": "sw",
        "time": "2026-10-06T15:20:00+00:00",
        "worktree": WORKTREE,
        "branch": "engineer-a1b2c3-0001",
        "remote_branch": "engineer-a1b2c3-0001",
        "remote_head": HEAD,
        "continue_from": "origin/engineer-a1b2c3-0001",
        "fresh_reason": "none",
        "pull_request": {"url": PR, "state": "OPEN", "checks": {"pass": 2, "fail": 1, "pending": 1}},
        "inbox": [{"id": open_item.id, "from": "ci@a1b2c3-0001", "state": "pending"}],
        "claims": ["t1"],
        "conversation_id": "conv-7",
        "launch": {
            "profile": "engineer",
            "harness": "codex",
            "model": "gpt-6.1-sol",
            "effort": "medium",
            "account": "primary",
            "model_source": "",
            "model_confidence": None,
            "profile_decision": {},
            "overlays": [],
        },
    }


@pytest.mark.parametrize(("harness", "session", "recorded"), [("claude", "max", "high"), ("codex", "xhigh", "high")])
def test_a_master_envelope_records_its_launch_effort_inside_the_swarm_range(store, harness, session, recorded):
    master = AgentRecord("master@a1b2c3-0001", "master", "master", harness=harness, effort=session)
    envelope = build(store, "sw", master, "recycle", [], 0, run=lambda argv, **_: _done(code=1))
    assert envelope["launch"]["effort"] == recorded
    assert envelope["launch"]["harness"] == harness


def test_a_master_envelope_follows_the_configured_swarm_range(store):
    store.update("sw", effort_min="low", effort_max="medium")
    master = AgentRecord("master@a1b2c3-0001", "master", "master", harness="claude", effort="max")
    envelope = build(store, "sw", master, "recycle", [], 0, run=lambda argv, **_: _done(code=1))
    assert envelope["launch"]["effort"] == "medium"


@pytest.mark.parametrize(("harness", "effort"), [("copilot", "max"), ("claude", "")])
def test_a_master_envelope_keeps_an_effort_it_cannot_rank(store, harness, effort):
    master = AgentRecord("master@a1b2c3-0001", "master", "master", harness=harness, effort=effort)
    envelope = build(store, "sw", master, "recycle", [], 0, run=lambda argv, **_: _done(code=1))
    assert envelope["launch"]["effort"] == effort


def test_a_lane_envelope_records_its_exact_launch_effort(store):
    agent = replace(_agent(), harness="claude", effort="max")
    envelope = build(store, "sw", agent, "recycle", [], 0, run=_run([]))
    assert envelope["launch"]["effort"] == "max"


def test_facts_the_runtime_cannot_find_are_named_unknown(store):
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", seat="eng-1@sw")
    envelope = build(store, "sw", agent, "quota", None, 0, run=lambda argv, **_: _done(code=1))
    assert envelope["phase"] == envelope["worktree"] == envelope["branch"] == "unknown"
    assert envelope["remote_branch"] == envelope["remote_head"] == "unknown"
    assert envelope["continue_from"] == "fresh"
    assert envelope["fresh_reason"] == "the predecessor's branch is unknown"
    assert envelope["pull_request"] == "unknown" and envelope["claims"] == "unknown"
    assert envelope["conversation_id"] == "unknown"


def test_no_pull_request_on_the_task_is_none_not_unknown(store):
    envelope = build(store, "sw", _agent(), "recycle", [{"id": "t1", "phase": "p7"}], 0, run=_run([]))
    assert envelope["pull_request"] == "none"


def test_the_reasons_are_the_approved_transfer_kinds():
    assert REASONS == ("recycle", "quota", "succession", "takeover", "reopen", "restore", "inbox", "exit", "operator")


def test_an_unknown_reason_is_refused(store):
    with pytest.raises(ValueError):
        build(store, "sw", _agent(), "vacation", [], 0, run=_run([]))


def test_the_upstream_names_the_remote_branch_a_continued_life_pushes_to(store):
    envelope = build(store, "sw", _agent(), "recycle", [], 0, run=_run([], upstream="origin/engineer-a1b2c3-0000"))
    assert envelope["branch"] == "engineer-a1b2c3-0001"
    assert envelope["remote_branch"] == "engineer-a1b2c3-0000"
    assert envelope["continue_from"] == "origin/engineer-a1b2c3-0000"


def test_an_upstream_on_another_remote_leaves_the_local_branch(store):
    envelope = build(store, "sw", _agent(), "recycle", [], 0, run=_run([], upstream="fork/engineer-a1b2c3-0000"))
    assert envelope["remote_branch"] == "engineer-a1b2c3-0001"


def test_a_branch_missing_on_the_remote_starts_the_successor_fresh_and_says_why(store):
    envelope = build(store, "sw", _agent(), "recycle", [], 0, run=_run([], remote=(2, "")))
    assert envelope["remote_head"] == "none"
    assert envelope["continue_from"] == "fresh"
    assert envelope["fresh_reason"] == "branch engineer-a1b2c3-0001 is not on the remote"


def test_an_unreadable_remote_starts_the_successor_fresh_and_says_why(store):
    envelope = build(store, "sw", _agent(), "recycle", [], 0, run=_run([], remote=(128, "")))
    assert envelope["remote_head"] == "unknown"
    assert envelope["continue_from"] == "fresh"
    assert envelope["fresh_reason"] == "the remote head of branch engineer-a1b2c3-0001 could not be read"


def test_an_upstream_lookup_that_fails_to_run_falls_back_to_the_local_branch(store):
    envelope = build(store, "sw", _agent(), "recycle", [], 0, run=_run([], raises=("rev-parse",)))
    assert (envelope["remote_branch"], envelope["remote_head"]) == ("engineer-a1b2c3-0001", HEAD)


def test_a_remote_lookup_that_fails_to_run_starts_the_successor_fresh(store):
    envelope = build(store, "sw", _agent(), "recycle", [], 0, run=_run([], raises=("ls-remote",)))
    assert (envelope["remote_head"], envelope["continue_from"]) == ("unknown", "fresh")


OLDER, NEWER = "engineer@a1b2c3-0001", "engineer@a1b2c3-0002"


def _remote(heads, upstreams=None, unread=(), raises=()):
    upstreams = upstreams or {}

    def run(argv, **kwargs):
        if argv[3] in raises:
            raise OSError(argv[3])
        if argv[:4] == ["git", "-C", "/repo", "worktree"]:
            return _done(
                f"worktree /repo\nbranch refs/heads/dev\n\nworktree {WORKTREE}\nbranch refs/heads/{OLDER.replace('@', '-')}\n"
            )
        if argv[3] == "rev-parse":
            assert argv[2] == WORKTREE
            return _done(
                f"{upstreams[WORKTREE]}\n" if WORKTREE in upstreams else "", 0 if WORKTREE in upstreams else 128
            )
        assert argv[:6] == ["git", "-C", "/repo", "ls-remote", "--exit-code", "origin"]
        assert kwargs == {**GIT, "timeout": 20}
        branch = argv[-1].removeprefix("refs/heads/")
        if branch in unread:
            return _done(code=128)
        return _done(f"{heads[branch]}\t{argv[-1]}\n") if branch in heads else _done(code=2)

    return run


def test_a_reclaim_continues_the_newest_branch_an_earlier_life_pushed():
    run = _remote({"engineer-a1b2c3-0001": "a" * 40, "engineer-a1b2c3-0002": HEAD})
    assert reclaim("/repo", [NEWER, OLDER], "", run=run) == {
        "remote_branch": "engineer-a1b2c3-0002",
        "remote_head": HEAD,
        "continue_from": "origin/engineer-a1b2c3-0002",
        "fresh_reason": "none",
    }


def test_a_reclaim_skips_a_life_that_never_pushed_for_an_older_one_that_did():
    verdict = reclaim("/repo", [NEWER, OLDER], "", run=_remote({"engineer-a1b2c3-0001": HEAD}))
    assert (verdict["continue_from"], verdict["remote_head"]) == ("origin/engineer-a1b2c3-0001", HEAD)


def test_a_reclaim_follows_the_upstream_a_continuing_life_pushed_to():
    run = _remote({"engineer-a1b2c3-0000": HEAD}, upstreams={WORKTREE: "origin/engineer-a1b2c3-0000"})
    assert reclaim("/repo", [OLDER], "", run=run)["continue_from"] == "origin/engineer-a1b2c3-0000"


def test_a_reclaim_falls_back_to_the_branch_the_ledger_recorded():
    verdict = reclaim("/repo", [NEWER], "feature-x", run=_remote({"feature-x": HEAD}))
    assert (verdict["remote_branch"], verdict["remote_head"]) == ("feature-x", HEAD)


def test_a_reclaim_with_no_pushed_branch_starts_fresh_and_names_what_it_checked():
    assert reclaim("/repo", [NEWER, OLDER], "feature-x", run=_remote({})) == {
        "remote_branch": "none",
        "remote_head": "none",
        "continue_from": "fresh",
        "fresh_reason": "no earlier life pushed a branch: checked engineer-a1b2c3-0002, engineer-a1b2c3-0001 and "
        "feature-x on the remote",
    }


def test_a_reclaim_whose_remote_cannot_be_read_starts_fresh_and_says_so():
    verdict = reclaim("/repo", [NEWER], "", run=_remote({}, unread=("engineer-a1b2c3-0002",)))
    assert verdict["continue_from"] == "fresh"
    assert verdict["fresh_reason"] == "the remote heads of engineer-a1b2c3-0002 could not be read"


def test_a_reclaim_whose_git_fails_to_run_starts_fresh_without_raising():
    verdict = reclaim("/repo", [OLDER], "", run=_remote({}, raises=("worktree", "ls-remote")))
    assert verdict["continue_from"] == "fresh"
    assert verdict["fresh_reason"] == "the remote heads of engineer-a1b2c3-0001 could not be read"


def test_a_reclaim_names_two_unpushed_branches_with_and():
    verdict = reclaim("/repo", [NEWER, OLDER], "", run=_remote({}))
    assert verdict["fresh_reason"] == (
        "no earlier life pushed a branch: checked engineer-a1b2c3-0002 and engineer-a1b2c3-0001 on the remote"
    )


def test_a_reclaim_names_one_unpushed_branch_alone():
    verdict = reclaim("/repo", [OLDER], "", run=_remote({}))
    assert verdict["fresh_reason"] == "no earlier life pushed a branch: checked engineer-a1b2c3-0001 on the remote"


def test_a_reclaim_lists_every_remote_head_it_could_not_read():
    unread = ("engineer-a1b2c3-0002", "engineer-a1b2c3-0001")
    verdict = reclaim("/repo", [NEWER, OLDER], "", run=_remote({}, unread=unread))
    assert verdict["fresh_reason"] == (
        "the remote heads of engineer-a1b2c3-0002, engineer-a1b2c3-0001 could not be read"
    )
