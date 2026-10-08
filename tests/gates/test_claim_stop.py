import json

import pytest

from scripts.gates import Call, Gate, Who
from scripts.gates.base import Decision
from scripts.gates.claim_stop import PLAIN, STREAK, ClaimStop, parked_by, refusal, ruling
from scripts.gates.progress import Progress
from scripts.gates.verdicts import Verdicts
from scripts.swarm import idle
from scripts.swarm.keyspace import ROOT as KEY_ROOT
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
        self.comments, self.updates = [], []

    def tasks(self, slug):
        return list(self.rows.values()) if slug == SLUG else []

    def update_task(self, slug, task_id, fields, by="swarm"):
        self.updates.append((slug, task_id, fields, by))
        self.rows[task_id].update(fields)

    def comment(self, slug, task_id, text, by):
        self.comments.append((slug, task_id, text, by))


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
    assert ruling(task, PullRequest("OPEN", None, 1, True, False), None) == ("", URL)
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


@pytest.mark.parametrize("kind", ["ops", "tune", "troubleshoot", "research"])
def test_a_merged_noncode_task_passes_on_a_live_wait_and_owes_its_contract_without_one(kind):
    task = {"id": "t1", "pr_url": URL, "kind": kind}
    assert ruling(task, MERGED, {"until": 1, "reason": "measuring"}) == ("", "")
    assert ruling(task, MERGED, {"until": 1, "on": {"kind": "reply", "target": "m-1"}}) == ("", "")
    assert ruling(task, MERGED, {"until": 1, "on": {"kind": "task", "target": "t2"}}) == ("", "")
    assert ruling(task, MERGED, None) == ("contract", "")
    assert ruling(task, MERGED, {"until": 1, "on": {"kind": "checks", "target": URL}}) == ("contract", "")


@pytest.mark.parametrize("kind", [None, "code", "ci"])
def test_a_merged_code_or_ci_task_owes_done_whatever_it_waits_on(kind):
    task = {"id": "t1", "pr_url": URL, **({"kind": kind} if kind else {})}
    assert ruling(task, MERGED, {"until": 1, "reason": "measuring"}) == ("merged", "")
    assert ruling(task, MERGED, None) == ("merged", "")


@pytest.mark.parametrize(
    "kind, proof",
    [
        ("ops", '--command "<command>" --output "<its output>"'),
        ("tune", '--command "<command>" --output "<its output>"'),
        ("troubleshoot", '--root-cause "<cause>" --evidence "<what shows it>" --fix <pr url> | --filed "<follow up>"'),
        ("research", "--finding <link>"),
    ],
)
def test_the_contract_refusal_names_the_proof_its_kind_closes_on(kind, proof):
    task, block = {"id": "t1", "pr_url": URL, "kind": kind}, f'agentihooks swarm {SLUG} block "<why>"'
    assert refusal("contract", SLUG, task, MERGED) == (
        f"your pull request {URL} merged, and {kind} task t1 closes on its proof contract: close it with agentihooks "
        f"swarm {SLUG} done {proof}. Name the wait: agentihooks swarm {SLUG} wait --on checks <pr url> | reply "
        f"<inbox item> | task <id>, or a bare wait of at most 60 minutes; or block with {block}"
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


def test_a_tune_task_with_a_merged_fix_stops_on_its_measurement_wait_until_the_wait_ends(rig):
    rig.task.update(state="pr", pr_url=URL, kind="tune")
    rig.pulls[URL] = MERGED
    idle.declare_wait(rig.store.redis, SLUG, ME, rig.clock[0] + 60_000, "after measurement", rig.clock[0])
    assert rig.stop().allowed
    rig.clock[0] += 60_000
    decision = rig.stop()
    assert not decision.allowed
    assert decision.reason.startswith(f"your pull request {URL} merged, and tune task t1 closes on its proof contract")


def test_a_tune_task_cannot_stop_on_the_checks_wait_of_its_merged_pull_request(rig):
    rig.task.update(state="pr", pr_url=URL, kind="tune")
    rig.pulls[URL] = PENDING
    assert rig.stop().allowed
    rig.pulls[URL] = MERGED
    for _ in range(STREAK - 1):
        assert not rig.stop().allowed
    assert rig.stop().allowed
    assert rig.ledger.rows["t1"]["state"] == "blocked"
    assert rig.ledger.comments[0][2].endswith(f"while {PLAIN['contract']}.")


def test_a_stop_on_pending_checks_passes_and_records_a_checked_wait(rig):
    rig.task.update(state="pr", pr_url=URL)
    rig.pulls[URL] = PENDING
    assert rig.stop().allowed
    held = idle.wait(rig.store.redis, SLUG, ME)
    assert held["on"] == {"kind": "checks", "target": URL}
    assert held["at"] == rig.clock[0] and held["until"] > rig.clock[0]
    assert held["reason"] == f"checks on {URL}"


@pytest.mark.parametrize("on", [{"kind": "reply", "target": "a1b2c3"}, {"kind": "task", "target": "t9"}])
def test_a_stop_on_pending_checks_keeps_a_live_reply_or_task_wait_and_its_reason(rig, on):
    rig.task.update(state="pr", pr_url=URL)
    rig.pulls[URL] = PENDING
    until = rig.clock[0] + 60_000
    idle.declare_wait(rig.store.redis, SLUG, ME, until, "the reviewer's answer", rig.clock[0] - 1, on=on)
    rig.clock[0] += 1_000
    assert rig.stop().allowed
    held = idle.wait(rig.store.redis, SLUG, ME)
    assert (held["on"], held["reason"], held["until"]) == (on, "the reviewer's answer", until)


def test_a_stop_on_pending_checks_replaces_an_expired_reply_wait_with_the_checks_wait(rig):
    rig.task.update(state="pr", pr_url=URL)
    rig.pulls[URL] = PENDING
    on = {"kind": "reply", "target": "a1b2c3"}
    idle.declare_wait(rig.store.redis, SLUG, ME, rig.clock[0] + 60_000, "the reviewer's answer", rig.clock[0], on=on)
    rig.clock[0] += 60_000
    assert rig.stop().allowed
    held = idle.wait(rig.store.redis, SLUG, ME)
    assert held["on"] == {"kind": "checks", "target": URL}
    assert held["reason"] == f"checks on {URL}"


def test_a_stop_on_pending_checks_turns_a_live_bare_wait_into_the_checks_wait(rig):
    rig.task.update(state="pr", pr_url=URL)
    rig.pulls[URL] = PENDING
    idle.declare_wait(rig.store.redis, SLUG, ME, rig.clock[0] + 60_000, "reviewers", rig.clock[0])
    assert rig.stop().allowed
    assert idle.wait(rig.store.redis, SLUG, ME)["on"] == {"kind": "checks", "target": URL}


def test_a_live_wait_lets_the_stop_through_and_an_expired_one_does_not(rig):
    idle.declare_wait(rig.store.redis, SLUG, ME, rig.clock[0] + 60_000, "reviewers", rig.clock[0])
    assert rig.stop().allowed
    rig.clock[0] += 60_000
    assert not rig.stop().allowed


def test_the_third_block_in_a_row_blocks_the_task_and_lets_the_stop_through(rig):
    assert not rig.stop().allowed
    second = rig.stop()
    assert not second.allowed and second.reason.endswith("(stop block 2 of 2; the next one blocks the task)")
    assert rig.stop().allowed
    assert rig.ledger.rows["t1"]["state"] == "blocked"
    assert rig.ledger.updates == [(SLUG, "t1", {"state": "blocked"}, ME)]
    (slug, task_id, text, by) = rig.ledger.comments[0]
    assert (slug, task_id, by) == (SLUG, "t1", ME)
    assert text == (
        "Blocked by the stop gate: the agent stopped 3 times while it held the task with no pull request and no wait."
    )
    assert rig.store.claimant(SLUG, "t1") is None
    assert [(a.state, a.pane_id) for a in rig.store.agents(SLUG)] == [("finished", "p1")]
    assert [(r["gate"], r["kind"], r["agent"], r["tool"]) for r in rig.rows()] == [("claim-stop", "blocked", ME, "")]
    assert rig.store.redis.get(f"{KEY_ROOT}:swarm:{SLUG}:stop-blocks:{ME}") is None


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
    assert rig.ledger.comments[0][2].endswith(f"while {PLAIN[owed]}.")
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
    idle.end_wait(rig.store.redis, SLUG, ME, rig.clock[0])
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
    def unread(slug):
        raise AssertionError(f"read the ledger of {slug!r} for an agent the gate skips")

    rig.ledger.tasks = unread
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


def _finish(rig, name=ME):
    rig.store.put_agent(SLUG, AgentRecord(name=name, lane="eng", task="t1", pane_id="p1", state="finished"))


@pytest.mark.parametrize("dependency", ["claimed", "done"])
def test_a_task_its_claimant_parked_and_handed_off_lets_the_stop_through(rig, dependency):
    rig.ledger.rows["d0"] = {"id": "d0", "state": dependency}
    rig.task.update(parked_on=["d0"])
    _finish(rig)
    rig.store.put_agent(SLUG, AgentRecord(name="engineer@100001-0002", lane="eng", task="t2", state="working"))
    assert rig.stop() == Decision()
    assert rig.store.redis.get(rig.store.key(SLUG, "stop-blocks", ME)) is None


def test_a_working_successor_on_a_parked_task_owes_its_stop(rig):
    rig.ledger.rows["d0"] = {"id": "d0", "state": "done"}
    rig.task.update(parked_on=["d0"])
    decision = rig.stop()
    assert not decision.allowed and "you hold task t1 with no open pull request and no wait" in decision.reason


def test_a_handed_off_claimant_with_nothing_parked_owes_its_stop(rig):
    _finish(rig)
    assert not rig.stop().allowed


def test_only_the_stopping_agents_own_record_releases_a_parked_task(rig):
    rig.task.update(parked_on=["d0"])
    _finish(rig, "engineer@100001-0002")
    assert not rig.stop().allowed


def test_parked_by_reads_the_parked_list_and_the_callers_record(rig):
    _finish(rig)
    assert parked_by(rig.store, WHO, {"parked_on": ["d0"]})
    assert not parked_by(rig.store, WHO, {"depends_on": ["d0"]})
    assert not parked_by(rig.store, WHO, {"parked_on": []})
    assert not parked_by(rig.store, Who(name=ME, swarm="other", lane="eng", task="t1"), {"parked_on": ["d0"]})


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
    assert rig.store.claimant(SLUG, "t1") is None
    assert [(a.name, a.lane, a.task, a.state) for a in rig.store.agents(SLUG)] == [(ME, "eng", "t1", "finished")]


def test_an_outcome_before_the_first_block_leaves_the_count_running(rig):
    Progress(rig.store.redis, SLUG).outcome(ME, "pushed", rig.clock[0] - 5)
    assert not rig.stop().allowed
    second = rig.stop()
    assert second.reason.endswith("(stop block 2 of 2; the next one blocks the task)")
    held = json.loads(rig.store.redis.get(f"{KEY_ROOT}:swarm:{SLUG}:stop-blocks:{ME}"))
    assert held == {"count": 2, "at": rig.clock[0]}


def test_an_outcome_at_the_moment_of_a_block_belongs_before_it(rig):
    assert not rig.stop().allowed
    Progress(rig.store.redis, SLUG).outcome(ME, "pushed", rig.clock[0])
    assert rig.stop().reason.endswith("(stop block 2 of 2; the next one blocks the task)")


def test_the_real_clock_reads_milliseconds():
    import time

    assert abs(ClaimStop().now() - time.time() * 1000) < 5_000


def test_the_gate_entry_runs_it_by_name():
    from scripts.gates.entry import GATES

    assert isinstance(GATES["claim-stop"], ClaimStop)
