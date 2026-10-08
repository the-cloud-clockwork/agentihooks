import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_core, ledger_stats  # noqa: E402

HOUR = 3_600_000
NOW = 10 * HOUR
MINUTE = 60_000
TIERS = {"S": {"minutes": 10, "samples": 0}, "M": {"minutes": 25, "samples": 0}, "L": {"minutes": 40, "samples": 0}}


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


class TestTimeLeft:
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
            ],
            "time_left": {"inputs": {"slots": 2, "ci_minutes": 5}},
        }

    def test_the_review_names_every_computed_finding(self):
        assert ledger_stats.review(self.ledger(180), self.meta(), NOW) == (
            "Operator stats check, computed now. "
            "Stale phases: p1 One is open with every task done. "
            "Undecided follow-ups: check disk. "
            "Tasks: 1 open, 1 claimed, 0 pr. "
            "Close rate: 2 tasks in the last hour. "
            "Time left: the page shows 3h 0m, computed 1h 0m as the larger of a 60m chain and 60m of work over 2 slots, "
            "for 2 remaining tasks at S 10m, M 25m, L 40m and 5m of CI, stale. "
            "Judge stale phases and undecided follow-ups against the real work, then ack."
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
            "Time left: the page shows 1h 0m, computed 1h 0m as the larger of a 60m chain and 60m of work over 2 slots, "
            "for 2 remaining tasks at S 10m, M 25m, L 40m and 5m of CI, current. "
            "Judge stale phases and undecided follow-ups against the real work, then ack."
        )

    def test_no_close_in_the_last_hour_leaves_time_left_to_the_master(self):
        assert ledger_stats.review(self.ledger(None), {"events": []}, NOW).split(". ")[4:6] == [
            "Close rate: 0 tasks in the last hour",
            "Time left: the page shows not set, code cannot compute it: live capacity has not been observed",
        ]


class TestReviewBounds:
    def closes(self, claimed):
        events = []
        for n, tid in enumerate(("a", "b", "c", "d")):
            if claimed:
                events.append(event("task claimed", tid, NOW - (40 + n) * MINUTE))
            events.append(event("task done", tid, NOW - (10 + n) * MINUTE))
        return {"events": events, "time_left": {"inputs": {"slots": 4, "ci_minutes": 5}}}

    def ledger(self, time_left):
        done = [task(tid, "done") for tid in ("a", "b", "c", "d")]
        return doc(
            phases=[{"id": "p1", "title": "One", "done": False}],
            followups=[
                {"id": "f1", "text": "check disk.", "done": False},
                {"id": "f2", "text": "rotate logs on host X"},
            ],
            tasks=[*done, task("e", "open"), task("f", "open", depends_on=["e"])],
            time_left_minutes=time_left,
        )

    def test_the_chain_bounds_the_review_when_it_is_longer(self):
        assert ledger_stats.review(self.ledger(60), self.closes(True), NOW).split(". ")[2:6] == [
            "Undecided follow-ups: check disk; rotate logs on host X",
            "Tasks: 2 open, 0 claimed, 0 pr",
            "Close rate: 4 tasks in the last hour",
            "Time left: the page shows 1h 0m, computed 1h 0m as the larger of a 60m chain and 60m of work over 4 "
            "slots, for 2 remaining tasks at S 10m, M 25m, L 40m and 5m of CI, current",
        ]

    def test_no_close_keeps_the_page_value_in_the_line(self):
        assert ledger_stats.review(self.ledger(120), {"events": []}, NOW).split(". ")[5] == (
            "Time left: the page shows 2h 0m, code cannot compute it: live capacity has not been observed"
        )

    def test_an_empty_ledger_reviews_to_nothing_left(self):
        assert ledger_stats.review({}, {}, NOW) == (
            "Operator stats check, computed now. "
            "Stale phases: none. "
            "Undecided follow-ups: none. "
            "Tasks: 0 open, 0 claimed, 0 pr. "
            "Close rate: 0 tasks in the last hour. "
            "Time left: the page shows not set, computed 0h 0m with no task remaining, stale. "
            "Judge stale phases and undecided follow-ups against the real work, then ack."
        )


class TestStatsSyncEvent:
    def ctx(self, events=(), slots=None):
        meta = {"rev": 0, "stamps": {}, "events": list(events), "members": {}}
        if slots is not None:
            meta["time_left"] = {"inputs": {"slots": slots, "ci_minutes": 35}}
        return ledger_core.Context(meta, NOW)

    def test_the_stats_sync_event_carries_the_computed_review(self):
        d = doc(phases=[{"id": "p1", "title": "One", "done": False}], tasks=[task("a", "done"), task("b", "open")])
        ctx = self.ctx([event("task claimed", "a", NOW - 30 * MINUTE), event("task done", "a", NOW - 10 * MINUTE)])
        text = ledger_stats.review(d, ctx.meta, NOW)
        assert ledger_core.record_sync(d, {"op": "stats_sync", "id": "s1"}, ctx) is True
        sent = [e for e in ctx.events if e["kind"] == "stats sync requested"]
        assert [{k: e[k] for k in ("by", "kind", "target", "id", "text")} for e in sent] == [
            {"by": "operator", "kind": "stats sync requested", "target": "", "id": "s1", "text": text}
        ]
        assert "Stale phases: p1 One is open with every task done. " not in text
        assert "Close rate: 1 task in the last hour. " in text

    def test_the_crew_sync_keeps_its_own_summary(self):
        ctx = self.ctx()
        assert ledger_core.record_sync(doc(), {"op": "sync", "id": "s2"}, ctx) is True
        assert ctx.events[0]["kind"] == "sync requested"
        assert ctx.events[0]["text"].startswith("Operator sync. ")

    def test_busy_master_refresh_persists_calculated_stats_without_ack(self):
        d = doc(
            phases=[{"id": "p1", "title": "One", "done": False}],
            tasks=[task("a", "done"), task("b", "open")],
            followups=[{"id": "f1", "text": "Judge this", "done": False}],
            time_left_minutes=400,
        )
        ctx = self.ctx([event("task claimed", "a", NOW - 30 * MINUTE), event("task done", "a", NOW - 10 * MINUTE)], 1)
        assert ledger_core.record_sync(d, {"op": "stats_sync", "id": "busy"}, ctx)
        assert d["time_left_minutes"] == 60
        refresh = ctx.meta["stats_refresh"]
        assert refresh["state"] == "refreshed"
        assert refresh["id"] == "busy"
        assert refresh["at"] == NOW
        assert refresh["rev"] == ctx.rev
        assert refresh["completed_at"] == NOW
        assert refresh["calculation"] == {
            "minutes": 60,
            "remaining": 1,
            "work": 60,
            "chain": 60,
            "throughput": 60,
            "slots": 1,
            "ci_minutes": 35,
            "tiers": TIERS,
            "stale": True,
            "gap": "",
        }
        assert ctx.stamps["time_left_minutes"] == {"at": NOW, "rev": ctx.rev, "by": "stats"}
        assert ctx.events[0] == {
            "rev": ctx.rev,
            "at": NOW,
            "by": "stats",
            "kind": "stats refreshed",
            "target": "",
            "id": "busy",
        }
        assert refresh["counts"] == {
            "phases": {"done": 0, "total": 1},
            "tasks": {"done": 1, "total": 2},
            "followups": {"done": 0, "total": 1},
        }
        assert refresh["calculation"]["stale"] is True
        assert d["followups"][0]["done"] is False
        assert not any(e["kind"] == "stats check answered" for e in ctx.events)

    def test_failed_refresh_keeps_prior_value_and_allows_immediate_retry(self, monkeypatch):
        d = doc(time_left_minutes=400)
        ctx = self.ctx()
        with monkeypatch.context() as control:

            def fail(*args):
                raise ValueError("synthetic calculation failure")

            control.setattr("ledger_stats.calculate", fail)
            assert ledger_core.record_sync(d, {"op": "stats_sync", "id": "failed"}, ctx)
        assert d["time_left_minutes"] == 400
        assert ctx.meta["stats_refresh"]["state"] == "failed"
        assert ctx.meta["stats_refresh"]["error"] == "Stats calculation failed: ValueError"
        assert ctx.meta["stats_refresh"] == {
            "id": "failed",
            "rev": ctx.rev,
            "at": NOW,
            "state": "failed",
            "completed_at": NOW,
            "error": "Stats calculation failed: ValueError",
        }
        assert ctx.events[0] == {
            "rev": ctx.rev,
            "at": NOW,
            "by": "stats",
            "kind": "stats refresh failed",
            "target": "",
            "id": "failed",
        }
        assert ctx.events[1]["text"] == (
            "Stats calculation failed: ValueError. Prior estimate retained; retry the refresh. Judge open follow-ups separately."
        )
        ctx.meta["events"] += ctx.events
        ctx.events = []
        assert ledger_core.record_sync(d, {"op": "stats_sync", "id": "retry"}, ctx)
        assert ctx.meta["stats_refresh"]["state"] == "refreshed"
        assert d["time_left_minutes"] == 0

    def test_unknown_estimate_retains_prior_value_and_names_evidence_gap(self):
        d = doc(tasks=[task("a", "open")], time_left_minutes=400)
        ctx = self.ctx()
        assert ledger_core.record_sync(d, {"op": "stats_sync", "id": "unknown"}, ctx)
        assert d["time_left_minutes"] == 400
        assert ctx.meta["stats_refresh"]["calculation"]["minutes"] is None
        assert ctx.meta["stats_refresh"]["calculation"]["gap"] == "live capacity has not been observed"
        assert ctx.meta["stats_refresh"]["state"] == "refreshed"

    def test_refresh_counts_scope_completed_phases_and_followups(self):
        d = doc(
            phases=[{"id": "p", "done": True}, {"id": "ignored", "out_of_scope": True}],
            followups=[{"id": "f", "done": True}, {"id": "ignored", "out_of_scope": True}],
            tasks=[task("a", "done"), task("b", "open", out_of_scope=True)],
        )
        ctx = self.ctx()
        assert ledger_core.record_sync(d, {"op": "stats_sync", "id": "counts"}, ctx)
        assert ctx.meta["stats_refresh"]["counts"] == {
            "phases": {"done": 1, "total": 1},
            "tasks": {"done": 1, "total": 1},
            "followups": {"done": 1, "total": 1},
        }
        assert d["time_left_minutes"] == 0

    def test_refresh_result_and_events_are_complete(self):
        d = doc(
            tasks=[task("a", "done"), task("b", "open")],
            time_left_minutes=400,
        )
        ctx = self.ctx([event("task claimed", "a", NOW - 30 * MINUTE), event("task done", "a", NOW - 10 * MINUTE)], 1)
        text = ledger_stats.refresh(d, ctx, "computed")
        assert ctx.meta["stats_refresh"] == {
            "id": "computed",
            "rev": 1,
            "at": NOW,
            "state": "refreshed",
            "completed_at": NOW,
            "counts": {
                "phases": {"done": 0, "total": 0},
                "tasks": {"done": 1, "total": 2},
                "followups": {"done": 0, "total": 0},
            },
            "calculation": {
                "minutes": 60,
                "remaining": 1,
                "work": 60,
                "chain": 60,
                "throughput": 60,
                "slots": 1,
                "ci_minutes": 35,
                "tiers": TIERS,
                "stale": True,
                "gap": "",
            },
        }
        assert d["time_left_minutes"] == 60
        assert ctx.events == [
            {"rev": 1, "at": NOW, "by": "stats", "kind": "stats refreshed", "target": "", "id": "computed"}
        ]
        assert ctx.stamps == {"time_left_minutes": {"rev": 1, "at": NOW, "by": "stats"}}
        assert "computed 1h 0m" in text

    def test_refresh_helper_failure_retains_estimate_and_records_failure(self, monkeypatch):
        d = doc(time_left_minutes=400)
        ctx = self.ctx()

        def fail(*args):
            raise ValueError("synthetic failure")

        monkeypatch.setattr(ledger_stats, "calculate", fail)
        text = ledger_stats.refresh(d, ctx, "failure")
        assert d["time_left_minutes"] == 400
        assert ctx.stamps == {}
        assert ctx.meta["stats_refresh"] == {
            "id": "failure",
            "rev": 1,
            "at": NOW,
            "state": "failed",
            "completed_at": NOW,
            "error": "Stats calculation failed: ValueError",
        }
        assert ctx.events == [
            {"rev": 1, "at": NOW, "by": "stats", "kind": "stats refresh failed", "target": "", "id": "failure"}
        ]
        assert (
            text
            == "Stats calculation failed: ValueError. Prior estimate retained; retry the refresh. Judge open follow-ups separately."
        )

    def test_calculation_names_unobserved_capacity(self):
        result = ledger_stats.calculate(doc(tasks=[task("a", "open")]), [], NOW)
        assert result["gap"] == "live capacity has not been observed"
        assert result["minutes"] is None

    def test_review_uses_the_supplied_calculation(self):
        d = doc(time_left_minutes=400)
        result = {
            "minutes": 75,
            "remaining": 3,
            "work": 150,
            "chain": 75,
            "throughput": 75,
            "slots": 2,
            "ci_minutes": 12.5,
            "tiers": TIERS,
            "stale": False,
            "gap": "",
        }
        text = ledger_stats.review(d, {"events": []}, NOW, result)
        assert (
            "computed 1h 15m as the larger of a 75m chain and 150m of work over 2 slots, for 3 remaining tasks "
            "at S 10m, M 25m, L 40m and 12.5m of CI, current"
        ) in text

    def test_successful_stats_refresh_keeps_its_cooldown(self):
        ctx = self.ctx([{"kind": "stats sync requested", "at": NOW - 1}])
        assert ledger_core.record_sync(doc(), {"op": "stats_sync", "id": "early"}, ctx) is False
        assert ctx.events == []
        ctx.at += ledger_core.SYNC_COOLDOWN_MS - 1
        assert ledger_core.record_sync(doc(), {"op": "stats_sync", "id": "boundary"}, ctx) is True

    def test_failed_stats_refresh_does_not_bypass_crew_sync_cooldown(self):
        ctx = self.ctx([{"kind": "sync requested", "at": NOW - 1}])
        ctx.meta["stats_refresh"] = {"state": "failed"}
        assert ledger_core.record_sync(doc(), {"op": "sync", "id": "crew"}, ctx) is False
        assert ctx.events == []
