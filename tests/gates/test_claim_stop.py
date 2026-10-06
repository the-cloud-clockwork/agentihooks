import json

import pytest

from scripts.gates import Call, Gate, Who
from scripts.gates.claim_stop import PLAIN, STREAK, ClaimStop, refusal, ruling
from scripts.gates.progress import Progress
from scripts.gates.verdicts import Verdicts
from scripts.swarm import idle
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord, RedisStore

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG, ME = "demo", "engineer@100001-0001"
URL = "https://github.com/o/r/pull/7"
WHO = Who(name=ME, swarm=SLUG, lane="eng", task="t1")
STOP = Call("")
PENDING = PullRequest("OPEN", None, 1, False, False)
GREEN = PullRequest("OPEN", None, 1, False, True)
RED = PullRequest("OPEN", None, 1, True, True, ("tests (1)", "lint"))
MERGED = PullRequest("MERGED", 5, 1, False, True)
CLOSED = PullRequest("CLOSED", None, 1, False, True)


class FakeLedger:
    def __init__(self, task):
        self.rows = {task["id"]: task}
        self.comments = []

    def tasks(self, slug):
        return list(self.rows.values())

    def update_task(self, slug, task_id, fields, by="swarm"):
        self.rows[task_id].update(fields)

    def comment(self, slug, task_id, text, by):
        self.comments.append((task_id, text, by))


@pytest.fixture
def rig(tmp_path):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent(SLUG, AgentRecord(name=ME, lane="eng", task="t1", pane_id="p1", state="working"))
    store.claim(SLUG, "t1", ME, 600_000)
    task = {"id": "t1", "state": "claimed", "claimed_by": ME, "pr_url": ""}
    ledger = FakeLedger(task)
    pulls = {}
    clock = [1_000_000]
    gate = ClaimStop(
        connect=lambda: store, ledger=lambda: ledger, github=lambda url: pulls.get(url), now=lambda: clock[0]
    )

    def stop(who=WHO):
        return gate.decide(STOP, who, Verdicts(SLUG, gate.name, tmp_path))

    def rows():
        path = tmp_path / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    rig = type("Rig", (), {})()
    rig.store, rig.ledger, rig.task, rig.pulls, rig.clock, rig.gate, rig.stop, rig.rows = (
        store,
        ledger,
        task,
        pulls,
        clock,
        gate,
        stop,
        rows,
    )
    return rig


def test_it_is_a_stop_gate_that_ships_enforcing():
    gate = ClaimStop()
    assert isinstance(gate, Gate)
    assert (gate.name, gate.default_mode, STREAK) == ("claim-stop", "enforce", 3)
    assert gate.matches(Call(""))
    assert not gate.matches(Call("Bash", {"command": "ls"}))


def test_ruling_follows_the_pull_request_then_the_wait():
    task = {"id": "t1", "pr_url": URL}
    assert ruling(task, MERGED, {"until": 1}) == ("merged", "")
    assert ruling(task, RED, {"until": 1}) == ("red", "")
    assert ruling(task, PENDING, None) == ("", URL)
    assert ruling(task, GREEN, {"until": 1}) == ("", "")
    assert ruling(task, GREEN, None) == ("green", "")
    assert ruling(task, None, None) == ("", "")
    assert ruling({"id": "t1"}, None, None) == ("idle", "")
    assert ruling(task, CLOSED, None) == ("idle", "")
    assert ruling({"id": "t1"}, None, {"until": 1}) == ("", "")
    assert ruling(task, CLOSED, {"until": 1}) == ("", "")


def test_each_refusal_names_the_one_command_that_clears_it():
    task, block = {"id": "t1", "pr_url": URL}, f'agentihooks swarm {SLUG} block "<why>"'
    assert refusal("merged", SLUG, task, MERGED) == (
        f"your pull request {URL} merged: close the task now with agentihooks swarm {SLUG} done --pr {URL}"
    )
    assert refusal("red", SLUG, task, RED) == (
        f"checks failed on {URL}: tests (1), lint. Fix them and push, or block with {block}"
    )
    unnamed = PullRequest("OPEN", None, 1, True, True)
    assert refusal("red", SLUG, task, unnamed).startswith(f"checks failed on {URL}: a check. Fix")
    assert refusal("green", SLUG, task, GREEN) == (
        f"checks passed on {URL}: merge it, then run agentihooks swarm {SLUG} done --pr {URL}"
    )
    assert refusal("idle", SLUG, {"id": "t1"}, None) == (
        f"you hold task t1 with no open pull request and no wait. Name the wait: agentihooks swarm {SLUG} wait --on "
        "checks <pr url> | reply <inbox item> | task <id>, or a bare wait of at most 60 minutes; or block with "
        f"{block}"
    )


def test_a_stop_with_no_pull_request_and_no_wait_is_blocked(rig):
    decision = rig.stop()
    assert not decision.allowed
    assert "you hold task t1 with no open pull request and no wait" in decision.reason
    assert decision.reason.endswith("(stop block 1 of 2; the next one blocks the task)")


def test_a_stop_after_the_merge_is_blocked(rig):
    rig.task.update(state="pr", pr_url=URL)
    rig.pulls[URL] = MERGED
    decision = rig.stop()
    assert not decision.allowed and "merged: close the task now" in decision.reason


def test_a_stop_on_pending_checks_passes_and_records_a_checked_wait(rig):
    rig.task.update(state="pr", pr_url=URL)
    rig.pulls[URL] = PENDING
    assert rig.stop().allowed
    held = idle.wait(rig.store.redis, SLUG, ME)
    assert held["on"] == {"kind": "checks", "target": URL}
    assert held["at"] == rig.clock[0] and held["until"] > rig.clock[0]


def test_a_live_wait_lets_the_stop_through_and_an_expired_one_does_not(rig):
    idle.declare_wait(rig.store.redis, SLUG, ME, rig.clock[0] + 60_000, "reviewers", rig.clock[0])
    assert rig.stop().allowed
    rig.clock[0] += 120_000
    assert not rig.stop().allowed


def test_the_third_block_in_a_row_blocks_the_task_and_lets_the_stop_through(rig):
    assert not rig.stop().allowed
    second = rig.stop()
    assert not second.allowed and second.reason.endswith("(stop block 2 of 2; the next one blocks the task)")
    assert rig.stop().allowed
    assert rig.ledger.rows["t1"]["state"] == "blocked"
    (task_id, text, by) = rig.ledger.comments[0]
    assert (task_id, by) == ("t1", ME)
    assert text == (
        "Blocked by the stop gate: the agent stopped 3 times while it held the task with no pull request and no wait."
    )
    assert rig.store.claimant(SLUG, "t1") is None
    assert [a.state for a in rig.store.agents(SLUG)] == ["finished"]
    assert [(r["gate"], r["kind"], r["agent"]) for r in rig.rows()] == [("claim-stop", "blocked", ME)]


@pytest.mark.parametrize("owed", sorted(PLAIN))
def test_each_plain_cause_is_a_short_ledger_line(owed):
    words = PLAIN[owed].split()
    assert 8 <= len(words) <= 14 and "-" not in PLAIN[owed] and "/" not in PLAIN[owed]


@pytest.mark.parametrize("pull, owed", [(MERGED, "merged"), (RED, "red"), (GREEN, "green")])
def test_the_block_note_names_what_was_owed(rig, pull, owed):
    rig.task.update(state="pr", pr_url=URL)
    rig.pulls[URL] = pull
    for _ in range(STREAK):
        rig.stop()
    assert rig.ledger.rows["t1"]["state"] == "blocked"
    assert rig.ledger.comments[0][1].endswith(f"while {PLAIN[owed]}.")
    assert rig.rows()[0]["reason"] == refusal(owed, SLUG, rig.task, pull)


def test_an_outcome_between_blocks_starts_the_count_again(rig):
    assert not rig.stop().allowed
    assert not rig.stop().allowed
    rig.clock[0] += 1
    Progress(rig.store.redis, SLUG).outcome(ME, "pushed", rig.clock[0])
    rig.clock[0] += 1
    again = rig.stop()
    assert not again.allowed and again.reason.endswith("(stop block 1 of 2; the next one blocks the task)")


def test_a_stop_that_passes_starts_the_count_again(rig):
    assert not rig.stop().allowed
    assert not rig.stop().allowed
    idle.declare_wait(rig.store.redis, SLUG, ME, rig.clock[0] + 60_000, "reviewers", rig.clock[0])
    assert rig.stop().allowed
    idle.end_wait(rig.store.redis, SLUG, ME)
    again = rig.stop()
    assert not again.allowed and again.reason.endswith("(stop block 1 of 2; the next one blocks the task)")


@pytest.mark.parametrize(
    "who",
    [
        Who(name=ME, swarm="", lane="eng", task="t1"),
        Who(name=ME, swarm=SLUG, lane="eng", task=""),
        Who(name="master@100001-0001", swarm=SLUG, lane="master", task="t1"),
        Who(name="planner@100001-0001", swarm=SLUG, lane="plan", task="t1"),
    ],
)
def test_only_a_pinned_worker_with_a_task_is_gated(rig, who):
    assert rig.stop(who).allowed


def test_a_ci_worker_is_gated(rig):
    ci = "ci@100001-0001"
    rig.task.update(claimed_by=ci)
    assert not rig.stop(Who(name=ci, swarm=SLUG, lane="ci", task="t1")).allowed


@pytest.mark.parametrize(
    "fields", [{"state": "open"}, {"state": "done"}, {"state": "blocked"}, {"claimed_by": "engineer@100001-0002"}]
)
def test_a_task_not_held_in_claimed_or_pr_lets_the_stop_through(rig, fields):
    rig.task.update(fields)
    assert rig.stop().allowed


def test_a_task_missing_from_the_ledger_lets_the_stop_through(rig):
    assert rig.stop(Who(name=ME, swarm=SLUG, lane="eng", task="t9")).allowed


def test_a_pull_request_github_cannot_read_lets_the_stop_through(rig):
    rig.task.update(state="pr", pr_url=URL)
    assert rig.stop().allowed
    assert idle.wait(rig.store.redis, SLUG, ME) is None


def test_the_third_block_with_no_agent_record_still_blocks_the_task(rig):
    rig.store.drop_agent(SLUG, ME)
    rig.stop()
    rig.stop()
    assert rig.stop().allowed
    assert rig.ledger.rows["t1"]["state"] == "blocked"


def test_the_gate_entry_runs_it_by_name():
    from scripts.gates.entry import GATES

    assert isinstance(GATES["claim-stop"], ClaimStop)
