import subprocess

import pytest

from scripts.swarm import resume, snapshot
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import Placed, SpawnError
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

ID = "0f8e6d4c-1111-2222-3333-444455556666"


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def agent(**changes):
    fields = {"name": "sw-eng-1", "lane": "eng", "task": "t1", "harness": "claude", "account": "a1"}
    return AgentRecord(**{**fields, "conversation_id": ID, **changes})


def always(*_):
    return True


@pytest.mark.parametrize(
    ("record", "worktree", "exists", "quota", "reason"),
    [
        (agent(), "/wt/sw-eng-1", always, always, ""),
        (agent(conversation_id=""), "/wt/sw-eng-1", always, always, "no conversation id"),
        (agent(), "", always, always, "no worktree"),
        (agent(), "/wt/sw-eng-1", lambda _: False, always, "worktree gone"),
        (agent(), "/wt/sw-eng-1", always, lambda *_: False, "account a1 out of quota"),
        (agent(), "/wt/sw-eng-1", always, lambda *_: None, ""),
        (agent(account=""), "/wt/sw-eng-1", always, lambda *_: False, ""),
    ],
)
def test_the_resume_decision_names_why_an_agent_cannot_reopen_its_conversation(record, worktree, exists, quota, reason):
    assert resume.blocker(record, worktree, exists, quota) == reason


def test_the_quota_check_asks_about_the_agents_own_harness_and_account():
    asked = []
    resume.blocker(agent(harness="codex", account="cx1"), "/wt", always, lambda *a: asked.append(a))
    assert asked == [("codex", "cx1")]


class ResumingRuntime(FakeRuntime):
    def __init__(self, fail=None):
        super().__init__()
        self.resumed, self.resume_fail = [], fail

    def resume(self, config, agent, text):
        if self.resume_fail:
            raise SpawnError(self.resume_fail)
        self.resumed.append((agent.name, agent.conversation_id, text))
        self.live.add(agent.name)
        return Placed(
            pane_id="w2:p9",
            harness=agent.harness,
            account=agent.account,
            model="opus",
            effort="high",
            model_source="lane-default",
            model_confidence=0.5,
        )


def saved(store, tmp_path, conversation=ID, worktree=True, **fields):
    store.create(SwarmConfig("sw", str(tmp_path), 2, 1))
    store.put_agent("sw", agent(pane_id="w1:p1", conversation_id=conversation, seat="eng-1@sw", started_at=5, **fields))
    master = AgentRecord("sw-master-1", MASTER, MASTER, pane_id="w1:m1", harness="claude", seat="master@sw")
    store.put_agent("sw", master)
    store.claim("sw", "t1", "sw-eng-1", 600_000)
    (tmp_path / "wt").mkdir(exist_ok=True)
    listing = f"worktree {tmp_path / 'wt'}\nbranch refs/heads/sw-eng-1\n" if worktree else ""
    snapshot.take(store, "sw", 99, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, listing, ""))
    store.redis.flushall()


def restore(store, runtime):
    return snapshot.restore(store, "sw", live=set(), runtime=runtime, has_quota=lambda *_: True, now_ms=1_000)


def test_restore_reopens_an_agent_in_its_own_conversation_and_forces_one_without_an_id_fresh(store, tmp_path):
    saved(store, tmp_path)
    rt = ResumingRuntime()
    outcomes = restore(store, rt)
    assert [(o.name, o.outcome, o.reason) for o in outcomes] == [
        ("sw-eng-1", "resumed", "own conversation reopened"),
        ("sw-master-1", "awaiting-decision", "no conversation id"),
    ]
    ((name, conversation, _),) = rt.resumed
    assert (name, conversation) == ("sw-eng-1", ID)
    eng = next(a for a in store.agents("sw") if a.name == "sw-eng-1")
    assert (eng.state, eng.pane_id, eng.seat, eng.task, eng.started_at) == ("working", "w2:p9", "eng-1@sw", "t1", 1_000)
    assert store.claimant("sw", "t1") == "sw-eng-1"
    assert next(a for a in store.agents("sw") if a.lane == MASTER).state == "awaiting-decision"
    assert store.config("sw").state == "paused"


def test_a_resumed_agent_is_told_it_was_restored_and_must_reread_its_task_folder_and_the_ledger(store, tmp_path):
    saved(store, tmp_path)
    rt = ResumingRuntime()
    restore(store, rt)
    text = rt.resumed[0][2]
    assert "restored" in text
    assert "/.agentihooks/swarm/sw/tasks/t1" in text
    assert str(snapshot.ledger_path("sw")) in text
    assert "Before acting, re-read" in text


def test_a_resumed_agent_record_takes_the_model_effort_and_source_its_relaunch_reports(store, tmp_path):
    saved(store, tmp_path, model="sonnet", effort="low", model_source="luna", model_confidence=0.99)
    restore(store, ResumingRuntime())
    eng = next(a for a in store.agents("sw") if a.name == "sw-eng-1")
    assert (eng.model, eng.effort, eng.model_source, eng.model_confidence) == ("opus", "high", "lane-default", 0.5)


def test_a_resume_that_fails_to_start_leaves_the_agent_to_start_fresh_with_the_reason(store, tmp_path):
    saved(store, tmp_path)
    outcomes = restore(store, ResumingRuntime(fail="herdr never reported the conversation"))
    assert (outcomes[0].outcome, outcomes[0].reason) == (
        "awaiting-decision",
        "resume failed to start: herdr never reported the conversation",
    )
    assert {a.state for a in store.agents("sw")} == {"awaiting-decision"}


def test_a_gone_worktree_forces_fresh_without_trying_to_resume(store, tmp_path):
    saved(store, tmp_path, worktree=False)
    rt = ResumingRuntime()
    outcomes = restore(store, rt)
    assert (outcomes[0].outcome, outcomes[0].reason) == ("awaiting-decision", "no worktree")
    assert rt.resumed == []


def test_the_outcomes_are_kept_on_the_swarm(store, tmp_path):
    saved(store, tmp_path)
    restore(store, ResumingRuntime())
    assert [(r["name"], r["outcome"], r["reason"], r["task"]) for r in store.restored("sw")] == [
        ("sw-eng-1", "resumed", "own conversation reopened", "t1"),
        ("sw-master-1", "awaiting-decision", "no conversation id", MASTER),
    ]
