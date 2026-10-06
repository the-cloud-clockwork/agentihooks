import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_core, ledger_stats  # noqa: E402

HOUR = 3_600_000
NOW = 10 * HOUR
MINUTE = 60_000


def task(tid, state, phase="p1", depends_on=(), **extra):
    return {"id": tid, "state": state, "done": state == "done", "phase": phase, "depends_on": list(depends_on), **extra}


def event(kind, tid, at):
    return {"kind": kind, "target": f"tasks/{tid}", "at": at, "by": "eng", "rev": 1}


def doc(**fields):
    base = {"phases": [], "followups": [], "tasks": [], "time_left_minutes": None}
    return {**base, **fields}


class TestStalePhases:
    def test_a_done_phase_with_an_unfinished_task_is_stale(self):
        d = doc(
            phases=[{"id": "p1", "title": "One", "done": True}],
            tasks=[task("a", "done"), task("b", "claimed")],
        )
        assert ledger_stats.stale_phases(d) == ["p1 One is done with b not done"]

    def test_an_open_phase_whose_tasks_are_all_done_is_stale(self):
        d = doc(phases=[{"id": "p1", "title": "One", "done": False}], tasks=[task("a", "done")])
        assert ledger_stats.stale_phases(d) == ["p1 One is open with every task done"]

    def test_matching_phases_empty_phases_and_out_of_scope_items_are_not_stale(self):
        d = doc(
            phases=[
                {"id": "p1", "title": "One", "done": True},
                {"id": "p2", "title": "Two", "done": False},
                {"id": "p3", "title": "Three", "done": False},
                {"id": "p4", "title": "Four", "done": False, "out_of_scope": True},
            ],
            tasks=[
                task("a", "done"),
                task("x", "open", out_of_scope=True),
                task("b", "open", phase="p2"),
                task("c", "done", phase="p4"),
            ],
        )
        assert ledger_stats.stale_phases(d) == []

    def test_a_phase_without_tasks_does_not_hide_later_phases(self):
        d = doc(
            phases=[{"id": "p0", "title": "Empty", "done": False}, {"id": "p1", "title": "One", "done": True}],
            tasks=[task("a", "open"), task("b", "pr")],
        )
        assert ledger_stats.stale_phases(d) == ["p1 One is done with a, b not done"]


class TestFollowupsAndCounts:
    def test_undecided_followups_are_the_open_ones_in_scope(self):
        d = doc(
            followups=[
                {"id": "f1", "text": "check disk", "done": False},
                {"id": "f2", "text": "done one", "done": True},
                {"id": "f3", "text": "dropped", "done": False, "out_of_scope": True},
            ]
        )
        assert ledger_stats.undecided_followups(d) == ["check disk"]

    def test_counts_open_claimed_and_pr_tasks_in_scope(self):
        d = doc(
            tasks=[
                task("a", "open"),
                task("b", "open"),
                task("c", "claimed"),
                task("d", "pr"),
                task("e", "done"),
                task("f", "open", out_of_scope=True),
            ]
        )
        assert ledger_stats.task_counts(d) == {"open": 2, "claimed": 1, "pr": 1}


class TestRate:
    def test_counts_distinct_tasks_closed_in_the_last_hour_that_are_still_done(self):
        d = doc(tasks=[task("a", "done"), task("b", "done"), task("c", "open"), task("old", "done")])
        events = [
            event("task done", "a", NOW - 10 * MINUTE),
            event("task done", "a", NOW - 5 * MINUTE),
            event("task done", "b", NOW - 59 * MINUTE),
            event("task done", "c", NOW - 20 * MINUTE),
            event("task done", "old", NOW - 61 * MINUTE),
        ]
        assert ledger_stats.closed_last_hour(d, events, NOW) == ["a", "b"]

    def test_a_close_exactly_one_hour_ago_counts(self):
        d = doc(tasks=[task("a", "done")])
        assert ledger_stats.closed_last_hour(d, [event("task done", "a", NOW - HOUR)], NOW) == ["a"]

    def test_mean_minutes_runs_from_the_last_claim_to_the_last_done(self):
        events = [
            event("task claimed", "a", NOW - 90 * MINUTE),
            event("task claimed", "a", NOW - 40 * MINUTE),
            event("task done", "a", NOW - 10 * MINUTE),
            event("task claimed", "b", NOW - 60 * MINUTE),
            event("task done", "b", NOW - 50 * MINUTE),
            event("task done", "c", NOW - 5 * MINUTE),
        ]
        assert ledger_stats.mean_minutes(events, ["a", "b", "c"]) == 20

    def test_mean_minutes_is_none_without_a_claimed_close(self):
        assert ledger_stats.mean_minutes([event("task done", "c", NOW)], ["c"]) is None

    def test_mean_minutes_skips_a_task_with_no_events_and_keeps_the_rest(self):
        events = [event("task claimed", "b", NOW - 30 * MINUTE), event("task done", "b", NOW)]
        assert ledger_stats.mean_minutes(events, ["a", "b"]) == 30


class TestChain:
    def test_longest_chain_counts_unfinished_tasks_through_their_dependencies(self):
        d = doc(
            tasks=[
                task("a", "done"),
                task("b", "open", depends_on=["a"]),
                task("c", "open", depends_on=["b"]),
                task("d", "open", depends_on=["c", "missing"]),
                task("e", "open"),
                task("x", "open", depends_on=["d"], out_of_scope=True),
            ]
        )
        assert ledger_stats.chain_length(d) == 3

    def test_a_dependency_cycle_ends_the_chain(self):
        d = doc(tasks=[task("a", "open", depends_on=["b"]), task("b", "open", depends_on=["a"])])
        assert ledger_stats.chain_length(d) == 2

    def test_a_task_without_dependencies_and_an_empty_ledger(self):
        assert ledger_stats.chain_length(doc(tasks=[{"id": "a", "state": "open"}])) == 1
        assert ledger_stats.chain_length(doc()) == 0


class TestTimeLeft:
    def test_throughput_bounds_time_left(self):
        assert ledger_stats.time_left(remaining=10, rate=4, chain=1, mean=10) == 150

    def test_the_chain_bounds_time_left_when_it_is_longer(self):
        assert ledger_stats.time_left(remaining=4, rate=8, chain=3, mean=25) == 75

    def test_nothing_remaining_is_zero_and_no_rate_is_unknown(self):
        assert ledger_stats.time_left(remaining=0, rate=0, chain=0, mean=None) == 0
        assert ledger_stats.time_left(remaining=3, rate=0, chain=1, mean=None) is None

    def test_time_left_rounds_up(self):
        assert ledger_stats.time_left(remaining=1, rate=7, chain=1, mean=None) == 9

    def test_stale_when_the_page_is_unset_or_off_by_more_than_a_quarter_and_fifteen_minutes(self):
        assert ledger_stats.is_stale(None, 60) is True
        assert ledger_stats.is_stale(180, 60) is True
        assert ledger_stats.is_stale(75, 60) is False
        assert ledger_stats.is_stale(76, 60) is True
        assert ledger_stats.is_stale(300, 240) is False
        assert ledger_stats.is_stale(301, 240) is True
        assert ledger_stats.is_stale(120, None) is False


class TestReview:
    def ledger(self, time_left):
        return doc(
            phases=[
                {"id": "p1", "title": "One", "done": False},
                {"id": "p2", "title": "Two", "done": False},
            ],
            followups=[{"id": "f1", "text": "check disk", "done": False}],
            tasks=[
                task("a", "done"),
                task("b", "done", phase="p2"),
                task("c", "open", phase="p2"),
                task("d", "claimed", phase="p2", depends_on=["c"]),
            ],
            time_left_minutes=time_left,
        )

    def meta(self):
        return {
            "events": [
                event("task claimed", "a", NOW - 50 * MINUTE),
                event("task done", "a", NOW - 30 * MINUTE),
                event("task claimed", "b", NOW - 40 * MINUTE),
                event("task done", "b", NOW - 20 * MINUTE),
            ]
        }

    def test_the_review_names_every_computed_finding(self):
        assert ledger_stats.review(self.ledger(180), self.meta(), NOW) == (
            "Operator stats check, computed now. "
            "Stale phases: p1 One is open with every task done. "
            "Undecided follow-ups: check disk. "
            "Tasks: 1 open, 1 claimed, 0 pr. "
            "Close rate: 2 tasks in the last hour. "
            "Time left: the page shows 3h 0m, computed 1h 0m from 2 remaining at 2 an hour and a chain of 2 "
            "at 20m a task, stale. "
            "Judge each line against the real work, fix what is stale, then ack."
        )

    def test_a_clean_ledger_says_none_and_keeps_a_matching_time_left(self):
        d = self.ledger(60)
        d["phases"][0]["done"] = True
        d["followups"][0]["done"] = True
        assert ledger_stats.review(d, self.meta(), NOW) == (
            "Operator stats check, computed now. "
            "Stale phases: none. "
            "Undecided follow-ups: none. "
            "Tasks: 1 open, 1 claimed, 0 pr. "
            "Close rate: 2 tasks in the last hour. "
            "Time left: the page shows 1h 0m, computed 1h 0m from 2 remaining at 2 an hour and a chain of 2 "
            "at 20m a task, current. "
            "Judge each line against the real work, fix what is stale, then ack."
        )

    def test_no_close_in_the_last_hour_leaves_time_left_to_the_master(self):
        assert ledger_stats.review(self.ledger(None), {"events": []}, NOW).split(". ")[4:6] == [
            "Close rate: 0 tasks in the last hour",
            "Time left: the page shows not set, no task closed in the last hour so code cannot compute it",
        ]


class TestReviewBounds:
    def closes(self, claimed):
        events = []
        for n, tid in enumerate(("a", "b", "c", "d")):
            if claimed:
                events.append(event("task claimed", tid, NOW - (40 + n) * MINUTE))
            events.append(event("task done", tid, NOW - (10 + n) * MINUTE))
        return {"events": events}

    def ledger(self, time_left):
        done = [task(tid, "done") for tid in ("a", "b", "c", "d")]
        return doc(
            phases=[{"id": "p1", "title": "One", "done": False}],
            followups=[{"id": "f1", "text": "check disk.", "done": False}, {"id": "f2", "text": "rotate logs"}],
            tasks=[*done, task("e", "open"), task("f", "open", depends_on=["e"])],
            time_left_minutes=time_left,
        )

    def test_the_chain_bounds_the_review_when_it_is_longer(self):
        assert ledger_stats.review(self.ledger(60), self.closes(True), NOW).split(". ")[2:6] == [
            "Undecided follow-ups: check disk; rotate logs",
            "Tasks: 2 open, 0 claimed, 0 pr",
            "Close rate: 4 tasks in the last hour",
            "Time left: the page shows 1h 0m, computed 1h 0m from 2 remaining at 4 an hour and a chain of 2 "
            "at 30m a task, current",
        ]

    def test_closes_without_claims_leave_only_the_throughput_bound(self):
        assert ledger_stats.review(self.ledger(60), self.closes(False), NOW).split(". ")[5] == (
            "Time left: the page shows 1h 0m, computed 0h 30m from 2 remaining at 4 an hour and a chain of 2, stale"
        )

    def test_no_close_keeps_the_page_value_in_the_line(self):
        assert ledger_stats.review(self.ledger(120), {"events": []}, NOW).split(". ")[5] == (
            "Time left: the page shows 2h 0m, no task closed in the last hour so code cannot compute it"
        )

    def test_an_empty_ledger_reviews_to_nothing_left(self):
        assert ledger_stats.review({}, {}, NOW) == (
            "Operator stats check, computed now. "
            "Stale phases: none. "
            "Undecided follow-ups: none. "
            "Tasks: 0 open, 0 claimed, 0 pr. "
            "Close rate: 0 tasks in the last hour. "
            "Time left: the page shows not set, computed 0h 0m from 0 remaining at 0 an hour and a chain of 0, stale. "
            "Judge each line against the real work, fix what is stale, then ack."
        )


class TestStatsSyncEvent:
    def ctx(self):
        return ledger_core.Context({"rev": 0, "stamps": {}, "events": [], "members": {}}, NOW)

    def test_the_stats_sync_event_carries_the_computed_review(self):
        d = doc(phases=[{"id": "p1", "title": "One", "done": False}], tasks=[task("a", "done")])
        ctx = self.ctx()
        assert ledger_core.record_sync(d, {"op": "stats_sync", "id": "s1"}, ctx) is True
        assert [(e["kind"], e["id"], e["text"]) for e in ctx.events] == [
            ("stats sync requested", "s1", ledger_stats.review(d, ctx.meta, NOW))
        ]
        assert "Stale phases: p1 One is open with every task done. " in ctx.events[0]["text"]

    def test_the_crew_sync_keeps_its_own_summary(self):
        ctx = self.ctx()
        assert ledger_core.record_sync(doc(), {"op": "sync", "id": "s2"}, ctx) is True
        assert ctx.events[0]["kind"] == "sync requested"
        assert ctx.events[0]["text"].startswith("Operator sync. ")
