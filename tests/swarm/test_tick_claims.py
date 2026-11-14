"""Tick claim pass tests: dependencies, stacking, parking, territory overlap and claim order by rank."""

import subprocess
from dataclasses import replace

import pytest

from scripts.swarm import stack
from scripts.swarm.store import SwarmError
from scripts.swarm.tick import tick
from tests.swarm import test_tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime, spawned_ids, workers

store = test_tick.store
reclaims = test_tick.reclaims


@pytest.mark.feature("swarm-tick-claim-after-dependency")
def test_a_task_waits_on_an_open_dependency_and_is_claimed_once_it_is_done(store):
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1"] and ledger.rows["t2"]["state"] == "open"
    ledger.rows["t1"]["state"] = "done"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"]


def test_a_critical_chain_successor_is_claimed_the_tick_after_its_predecessor_merges_while_other_work_waits(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger(
        [{"id": "a"}, {"id": "b", "depends_on": ["a"]}, {"id": "c", "depends_on": ["b"]}, {"id": "x"}, {"id": "y"}]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["a"]
    ledger.rows["a"]["state"] = "done"
    ledger.rows["x"]["rank"] = "urgent"
    ledger.rows["y"]["branch"] = "engineer-a1b2c3-0009"
    ledger.stamps = {"tasks/a/state": {"by": "engineer@a1b2c3-0001", "at": 61_000}, "tasks/x/rank": {"by": "operator"}}
    actions = tick("sw", store, ledger, runtime, now_ms=121_000)
    assert spawned_ids(runtime) == ["a", "b"]
    assert [ledger.rows[t]["state"] for t in ("x", "y", "c")] == ["open", "open", "open"]
    assert "chain guard claimed task b 60 seconds after task a merged on the critical chain" in actions
    assert (
        "b",
        "The dispatcher's chain guard gave this task the next free eng seat 60 seconds after the task before it "
        "on the critical chain merged.",
        "dispatcher",
    ) in ledger.comments


def test_a_task_starts_stacked_on_claimed_and_in_pr_dependencies_that_carry_a_branch(store):
    store.update("sw", max_eng=3)
    ledger = FakeLedger([{"id": "t1"}, {"id": "t0"}, {"id": "t2", "depends_on": ["t1", "t0", "t9"]}, {"id": "t9"}])
    ledger.rows["t9"].update(state="done", done=True)
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1", "t0"]
    ledger.rows["t1"]["branch"] = "engineer-a1b2c3-0001"
    ledger.rows["t0"].update(state="pr", branch="engineer-a1b2c3-0002")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t0", "t2"]
    assert runtime.tasks[-1]["stack_base"] == [
        {"task": "t1", "branch": "engineer-a1b2c3-0001"},
        {"task": "t0", "branch": "engineer-a1b2c3-0002"},
    ]
    assert runtime.tasks[0]["stack_base"] == []


@pytest.mark.parametrize("dependency_state", ["claimed", "pr"])
def test_retained_pull_waits_for_dependency_merge_before_resuming(store, reclaims, dependency_state):
    ledger = FakeLedger(
        [
            {"id": "t1"},
            {"id": "t2", "depends_on": ["t1"], "branch": "reviewed-work", "pr_url": "https://github.com/o/r/pull/3"},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state=dependency_state, branch="dependency-work")

    tick("sw", store, ledger, runtime, now_ms=2_000)

    assert spawned_ids(runtime) == ["t1"]
    assert ledger.rows["t2"]["state"] == "open"
    assert ledger.rows["t2"]["claimed_by"] == ""
    assert reclaims == []
    assert store.claimant("sw", "t2") is None
    assert store.claims("sw", "t2") == 0

    ledger.rows["t1"].update(state="done", done=True)
    tick("sw", store, ledger, runtime, now_ms=3_000)

    assert spawned_ids(runtime) == ["t1", "t2"]
    assert ledger.rows["t2"]["state"] == "pr"
    assert ledger.rows["t2"]["pr_url"] == "https://github.com/o/r/pull/3"
    assert reclaims == [("/repo", [], "reviewed-work")]
    assert runtime.tasks[-1]["stack_base"] == []


def test_a_stacked_claim_carries_the_repository_only_of_a_dependency_branch_in_another_repository(store, monkeypatch):
    asked = []
    monkeypatch.setattr(
        stack,
        "shell",
        lambda argv: asked.append(argv) or subprocess.CompletedProcess(argv, 0, "git@github.com:O/home.git\n", ""),
    )
    store.update("sw", max_eng=3)
    ledger = FakeLedger([{"id": "t1"}, {"id": "t0"}, {"id": "t2", "depends_on": ["t1", "t0"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(branch="engineer-a1b2c3-0001", branch_repo="https://github.com/o/other")
    ledger.rows["t0"].update(branch="engineer-a1b2c3-0002", branch_repo="https://github.com/o/home")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t0", "t2"]
    assert runtime.tasks[-1]["stack_base"] == [
        {"task": "t1", "branch": "engineer-a1b2c3-0001", "repo": "https://github.com/o/other"},
        {"task": "t0", "branch": "engineer-a1b2c3-0002"},
    ]
    assert ["git", "-C", "/repo", "remote", "get-url", "origin"] in asked


def test_a_stacked_claim_names_the_recorded_repository_when_its_own_origin_cannot_be_read(store, monkeypatch):
    monkeypatch.setattr(stack, "shell", lambda argv: subprocess.CompletedProcess(argv, 128, "", "no origin\n"))
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(branch="engineer-a1b2c3-0001", branch_repo="https://github.com/o/other")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.tasks[-1]["stack_base"] == [
        {"task": "t1", "branch": "engineer-a1b2c3-0001", "repo": "https://github.com/o/other"}
    ]


def test_a_stacked_claim_names_the_recorded_repository_when_git_cannot_run(store, monkeypatch):
    def refuse(argv):
        raise SwarmError("git -C could not run: no git")

    monkeypatch.setattr(stack, "shell", refuse)
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(branch="engineer-a1b2c3-0001", branch_repo="XXXX")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"]
    assert runtime.tasks[-1]["stack_base"] == [{"task": "t1", "branch": "engineer-a1b2c3-0001", "repo": "XXXX"}]


@pytest.mark.parametrize(
    ("state", "branch"), [("open", "engineer-x"), ("blocked", "engineer-x"), ("claimed", ""), ("pr", "")]
)
def test_a_dependency_that_is_not_active_or_has_no_branch_still_holds_the_task(store, state, branch):
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state=state, branch=branch, out_of_scope=True)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1"] and ledger.rows["t2"]["state"] == "open"


@pytest.mark.parametrize("field", ["depends_on", "parked_on"])
@pytest.mark.feature("swarm-tick-legacy-claim-holds-and-overlaps")
def test_a_task_naming_a_task_missing_from_the_ledger_is_held(store, field):
    ledger, runtime = FakeLedger([{"id": "t2", field: ["gone"]}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == [] and ledger.rows["t2"]["state"] == "open"


def test_a_parked_task_waits_for_its_blocker_and_then_takes_a_finish_claim_with_its_handoff(store):
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"], "parked_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["branch"] = "engineer-a1b2c3-0001"
    store.put_handoff("sw", "t2", "parked on its branch until t1 merges")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1"] and ledger.rows["t2"]["state"] == "open"
    ledger.rows["t1"].update(state="done", done=True)
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert spawned_ids(runtime) == ["t1", "t2"]
    assert runtime.tasks[-1]["handoff"] == "parked on its branch until t1 merges"
    assert runtime.tasks[-1]["stack_base"] == []


def test_a_parked_task_takes_the_free_slot_the_tick_after_its_blocker_is_done_ahead_of_fresh_work(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger(
        [
            {"id": "t1", "state": "pr", "branch": "engineer-a1b2c3-0001"},
            {"id": "fresh"},
            {"id": "t2", "depends_on": ["t1"], "parked_on": ["t1"], "branch": "engineer-a1b2c3-0002"},
        ]
    )
    runtime = FakeRuntime()
    ledger.rows["t1"].update(state="done", done=True)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2"] and ledger.rows["fresh"]["state"] == "open"


def test_a_reopened_task_with_a_branch_is_claimed_before_fresh_work_even_when_its_territory_overlaps(store):
    store.update("sw", max_eng=1)
    ledger, runtime = FakeLedger([{"id": "t1", "territory": ["hooks"]}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    later = FakeLedger(
        [
            {"id": "fresh", "territory": ["docs"]},
            {"id": "reopened", "branch": "engineer-a1b2c3-0003", "territory": ["hooks/x.py"]},
        ]
    )
    ledger.rows.update(later.rows)
    store.update("sw", max_eng=2)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "reopened"] and ledger.rows["fresh"]["state"] == "open"


def test_a_fresh_task_of_a_higher_rank_still_goes_ahead_of_a_resumed_one(store):
    store.update("sw", autonomy="assist")
    store.update("sw", max_eng=1)
    ledger = FakeLedger([{"id": "resumed", "branch": "engineer-a1b2c3-0004"}, {"id": "urgent", "rank": "urgent"}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["urgent"]


def test_a_resumed_task_whose_launch_failed_takes_only_a_slot_left_over(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger([{"id": "resumed", "branch": "engineer-a1b2c3-0005"}, {"id": "fresh"}])
    store.note_launch_failure("sw", "resumed", "canary timeout")
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["fresh"]


@pytest.mark.parametrize(
    ("parked_on", "blocker_done", "lives"), [(["t1"], False, 0), ([], False, 1), (["t1"], True, 1)]
)
def test_only_a_claim_that_ends_parked_on_an_unfinished_blocker_keeps_its_claim_life(
    store, parked_on, blocker_done, lives
):
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["branch"] = "engineer-a1b2c3-0001"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"] and store.claims("sw", "t2") == 1
    agent = next(a for a in workers(store) if a.task == "t2")
    ledger.rows["t1"].update(state="done" if blocker_done else "pr", done=blocker_done)
    ledger.rows["t2"]["parked_on"] = parked_on
    store.put_handoff("sw", "t2", "stopped here", seat=agent.seat)
    store.put_agent("sw", replace(agent, state="finished"))
    store.update("sw", state="paused")
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert agent.name in runtime.killed and ledger.rows["t2"]["state"] == "open"
    assert store.claims("sw", "t2") == lives


@pytest.mark.feature("swarm-tick-legacy-claim-holds-and-overlaps")
def test_an_overlapping_task_is_claimed_once_the_non_overlapping_work_is_taken(store):
    ledger = FakeLedger(
        [
            {"id": "t1", "territory": ["hooks"]},
            {"id": "t2", "territory": ["hooks/x.py"]},
            {"id": "t3", "territory": ["docs"]},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1", "t3"]
    store.update("sw", max_eng=3)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t3", "t2"]
    assert runtime.tasks[2]["overlaps"] == [{"task": "t1", "claimant": runtime.spawned[0][1], "areas": ["hooks/x.py"]}]
    assert runtime.tasks[0]["overlaps"] == [] and runtime.tasks[1]["overlaps"] == []


@pytest.mark.feature("swarm-tick-legacy-claim-holds-and-overlaps")
def test_a_broad_territory_never_stops_a_claim(store):
    ledger = FakeLedger(
        [
            {"id": "t1", "territory": ["scripts"]},
            {"id": "t2", "territory": ["scripts/swarm/tick.py", "docs"]},
            {"id": "t3", "lane": "ci", "territory": ["./scripts/"]},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1", "t3", "t2"]
    first, second = runtime.spawned[0][1], runtime.spawned[1][1]
    assert runtime.tasks[1]["overlaps"] == [{"task": "t1", "claimant": first, "areas": ["scripts"]}]
    assert runtime.tasks[2]["overlaps"] == [
        {"task": "t1", "claimant": first, "areas": ["scripts/swarm/tick.py"]},
        {"task": "t3", "claimant": second, "areas": ["scripts/swarm/tick.py"]},
    ]


@pytest.mark.feature("swarm-tick-legacy-claim-holds-and-overlaps")
def test_an_overlap_names_the_live_owner_after_its_missing_claimant_is_repaired(store):
    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    del ledger.rows["t1"]["claimed_by"]
    ledger.rows["t2"] = {"id": "t2", "lane": "eng", "state": "open", "out_of_scope": False, "territory": ["hooks"]}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"]
    assert runtime.tasks[1]["overlaps"] == [{"task": "t1", "claimant": runtime.spawned[0][1], "areas": ["hooks"]}]


@pytest.mark.parametrize("claimant", ["engineer@a1b2c3-0099", "eng-1@sw", ""])
@pytest.mark.feature("swarm-tick-legacy-claim-holds-and-overlaps")
def test_an_overlap_released_for_checks_has_no_coordination_recipient(store, claimant):
    from scripts.swarm import prompt, ready

    ledger = FakeLedger(
        [
            {"id": "t1", "state": "pr", "awaiting": "merge", "claimed_by": claimant, "territory": ["hooks"]},
            {"id": "t2", "title": "Build it", "territory": ["hooks/x.py"]},
        ]
    )
    ready.save(store, "sw", "t1", {"agent": claimant})
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2"]
    overlap = runtime.tasks[0]["overlaps"]
    assert overlap == [{"task": "t1", "claimant": "", "areas": ["hooks/x.py"], "checks": True}]
    text = prompt.build("sw", "/repo", "eng", runtime.spawned[0][1], runtime.tasks[0])
    assert "Task t1, waiting on checks with no agent; no coordination note needed, shares hooks/x.py." in text
    assert f"claimed by {claimant}" not in text


@pytest.mark.feature("swarm-tick-legacy-claim-holds-and-overlaps")
def test_a_reused_seat_does_not_make_its_new_agent_the_owner_of_an_overlap(store):
    from scripts.swarm import prompt, ready

    ledger = FakeLedger([{"id": "other", "territory": ["docs"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = store.agents("sw")[0]
    ledger.rows.update(
        FakeLedger(
            [
                {"id": "t1", "state": "pr", "awaiting": "merge", "claimed_by": agent.seat, "territory": ["hooks"]},
                {"id": "t2", "title": "Build it", "territory": ["hooks/x.py"]},
            ]
        ).rows
    )
    ready.save(store, "sw", "t1", {"agent": "departed"})
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["other", "t2"]
    overlap = runtime.tasks[1]["overlaps"]
    assert overlap == [{"task": "t1", "claimant": "", "areas": ["hooks/x.py"], "checks": True}]
    text = prompt.build("sw", "/repo", "eng", runtime.spawned[1][1], runtime.tasks[1])
    assert "waiting on checks with no agent; no coordination note needed" in text
    assert agent.name not in text and agent.seat not in text


def test_territories_that_only_share_a_name_prefix_do_not_overlap(store):
    ledger = FakeLedger(
        [{"id": "t1", "territory": ["scripts/swarm"]}, {"id": "t2", "territory": ["scripts/swarm_ledger"]}]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1", "t2"]


def test_a_task_without_territory_is_claimed_alongside_anything(store):
    ledger = FakeLedger(
        [{"id": "t1", "territory": ["hooks"]}, {"id": "t2"}, {"id": "t3", "lane": "ci", "territory": []}]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1", "t3", "t2"]


def test_an_urgent_ready_task_is_claimed_ahead_of_an_older_normal_one(store):
    store.update("sw", autonomy="assist")
    store.update("sw", max_eng=1)
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "rank": "urgent"}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2"]


def test_a_blocked_urgent_task_is_skipped_for_a_ready_normal_one(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger(
        [{"id": "t0", "state": "blocked"}, {"id": "t1", "rank": "urgent", "depends_on": ["t0"]}, {"id": "t2"}]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2"] and ledger.rows["t1"]["state"] == "open"


def test_an_urgent_task_sharing_a_territory_an_active_claim_holds_is_still_claimed(store):
    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t2"] = {**ledger.rows["t1"], "id": "t2", "state": "open", "claimed_by": "", "rank": "urgent"}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"] and ledger.rows["t1"]["state"] == "claimed"


def test_an_urgent_task_wins_a_shared_territory_over_an_earlier_normal_one(store):
    store.update("sw", autonomy="assist")
    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}, {"id": "t2", "rank": "urgent", "territory": ["hooks"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2", "t1"]


def test_an_urgent_overlapping_task_is_claimed_ahead_of_lower_ranked_clear_work(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t2"] = {**ledger.rows["t1"], "id": "t2", "state": "open", "claimed_by": "", "rank": "urgent"}
    ledger.rows["t3"] = {**ledger.rows["t2"], "id": "t3", "rank": "low", "territory": ["docs"]}
    ledger.stamps = {f"tasks/{t}/rank": {"by": "operator"} for t in ("t2", "t3")}
    store.update("sw", max_eng=2)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"]


def test_equal_ranks_keep_ledger_order_and_ranks_order_the_rest(store):
    store.update("sw", autonomy="assist")
    store.update("sw", max_eng=6)
    ledger = FakeLedger(
        [
            {"id": "t1", "rank": "low"},
            {"id": "t2"},
            {"id": "t3", "rank": "high"},
            {"id": "t4", "rank": "normal"},
            {"id": "t5", "rank": "high"},
            {"id": "t6", "rank": "urgent"},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t6", "t3", "t5", "t2", "t4", "t1"]


def test_claims_follow_rank_then_the_small_fast_clear_task_then_critical_path_depth(store):
    store.update("sw", autonomy="assist")
    store.update("sw", max_eng=6)
    ledger = FakeLedger(
        [
            {"id": "shallow", "phase": "p1"},
            {"id": "deep", "title": "deep", "rank": "normal", "phase": "p2"},
            {"id": "w1", "depends_on": ["deep"], "rank": "low", "phase": "p2"},
            {"id": "w2", "depends_on": ["w1"], "rank": "low", "phase": "p2"},
            {"id": "small", "difficulty": "S", "phase": "p3"},
            {"id": "top", "rank": "high", "phase": "p9"},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["top", "small", "deep", "shallow"]


def test_a_rank_change_applies_on_the_next_tick(store):
    store.update("sw", autonomy="assist")
    store.update("sw", max_eng=1)
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2"}, {"id": "t3"}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1"]
    ledger.rows["t1"]["state"] = "done"
    store.put_agent("sw", replace(workers(store)[0], state="finished"))
    ledger.rows["t3"]["rank"] = "high"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime)[1:] == ["t3"]


@pytest.mark.feature("swarm-tick-legacy-claim-holds-and-overlaps")
def test_a_swarm_whose_only_open_task_waits_on_a_blocked_one_drains(store):
    ledger = FakeLedger([{"id": "t1", "state": "blocked"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    assert "drained" in tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == []
