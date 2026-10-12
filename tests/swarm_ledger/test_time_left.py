import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_core, ledger_stats, ledger_time_left  # noqa: E402

HOUR = 3_600_000
MINUTE = 60_000
NOW = 30 * HOUR


def task(tid, state="open", difficulty=None, depends_on=(), **extra):
    row = {"id": tid, "state": state, "done": state == "done", "phase": "p1", "depends_on": list(depends_on), **extra}
    if difficulty is not None:
        row["difficulty"] = difficulty
    return row


def event(kind, tid, at):
    return {"kind": kind, "target": f"tasks/{tid}", "at": at, "by": "eng", "rev": 1}


def doc(*tasks, time_left=None):
    return {"phases": [], "followups": [], "tasks": list(tasks), "time_left_minutes": time_left}


def spans(tier, minutes, end=NOW):
    rows, events = [], []
    for n, length in enumerate(minutes):
        tid = f"{tier}{n}"
        rows.append(task(tid, "done", tier))
        pr = end - (n + 1) * 10 * MINUTE
        events += [event("task claimed", tid, pr - length * MINUTE), event("task pr", tid, pr)]
    return rows, events


class TestAgentMinutes:
    def test_fewer_than_five_samples_keep_the_operator_defaults(self):
        rows, events = spans("S", [4, 4, 4, 4])
        assert ledger_stats.agent_minutes(doc(*rows), events, NOW) == {
            "S": {"minutes": 10, "samples": 4},
            "M": {"minutes": 25, "samples": 0},
            "L": {"minutes": 40, "samples": 0},
        }

    def test_five_samples_replace_the_default_with_their_median(self):
        rows, events = spans("L", [4, 6, 8, 10, 30])
        assert ledger_stats.agent_minutes(doc(*rows), events, NOW)["L"] == {"minutes": 8, "samples": 5}

    def test_an_even_sample_count_takes_the_mean_of_the_middle_pair_to_one_decimal(self):
        rows, events = spans("M", [4, 6, 7, 10, 30, 40])
        assert ledger_stats.agent_minutes(doc(*rows), events, NOW)["M"] == {"minutes": 8.5, "samples": 6}

    def test_the_defaults_are_the_operator_numbers(self):
        assert ledger_stats.DEFAULT_MINUTES == {"S": 10, "M": 25, "L": 40}
        assert ledger_stats.SAMPLES == 5
        assert ledger_stats.SAMPLE_WINDOW_MS == 24 * HOUR


class TestClaimToPr:
    def test_spans_run_from_the_latest_claim_to_the_pull_request(self):
        rows = [task("a", "done", "S")]
        events = [
            event("task claimed", "a", NOW - 50 * MINUTE),
            event("task claimed", "a", NOW - 30 * MINUTE),
            event("task pr", "a", NOW - 10 * MINUTE),
        ]
        assert ledger_stats.claim_to_pr(doc(*rows), events, NOW) == {"S": [20.0], "M": [], "L": []}

    def test_a_task_without_difficulty_counts_as_medium(self):
        events = [event("task claimed", "a", NOW - 15 * MINUTE), event("task pr", "a", NOW)]
        assert ledger_stats.claim_to_pr(doc(task("a", "pr")), events, NOW)["M"] == [15.0]
        assert ledger_stats.difficulty({"difficulty": "X"}) == "M"
        assert ledger_stats.difficulty({"difficulty": "L"}) == "L"

    def test_unclaimed_repeated_unknown_and_out_of_scope_pull_requests_are_skipped(self):
        rows = [task("a", "done", "S"), task("x", "done", "S", out_of_scope=True), task("b", "done", "S")]
        events = [
            event("task pr", "b", NOW - 40 * MINUTE),
            event("task claimed", "a", NOW - 30 * MINUTE),
            event("task pr", "a", NOW - 20 * MINUTE),
            event("task pr", "a", NOW - 10 * MINUTE),
            event("task claimed", "x", NOW - 30 * MINUTE),
            event("task pr", "x", NOW - 10 * MINUTE),
            event("task claimed", "gone", NOW - 30 * MINUTE),
            event("task pr", "gone", NOW - 10 * MINUTE),
        ]
        assert ledger_stats.claim_to_pr(doc(*rows), events, NOW) == {"S": [10.0], "M": [], "L": []}

    def test_only_pull_requests_inside_the_last_day_count(self):
        rows = [task("old", "done", "S"), task("edge", "done", "S")]
        events = [
            event("task claimed", "old", NOW - 25 * HOUR),
            event("task pr", "old", NOW - 24 * HOUR - 1),
            event("task claimed", "edge", NOW - 25 * HOUR),
            event("task pr", "edge", NOW - 24 * HOUR),
        ]
        assert ledger_stats.claim_to_pr(doc(*rows), events, NOW)["S"] == [60.0]


TIERS = {"S": {"minutes": 10, "samples": 0}, "M": {"minutes": 25, "samples": 0}, "L": {"minutes": 40, "samples": 0}}


class TestRemaining:
    def test_an_open_or_blocked_task_costs_its_agent_minutes_plus_ci(self):
        assert ledger_stats.remaining_minutes(task("a"), TIERS, 12, [], NOW) == 37
        claim = [event("task claimed", "b", NOW - 20 * MINUTE)]
        assert ledger_stats.remaining_minutes(task("b", "blocked", "S"), TIERS, 0, claim, NOW) == 10

    def test_a_claimed_task_counts_only_what_is_left_after_its_last_claim(self):
        events = [event("task claimed", "a", NOW - 90 * MINUTE), event("task claimed", "a", NOW - 30 * MINUTE)]
        assert ledger_stats.remaining_minutes(task("a", "claimed", "L"), TIERS, 10, events, NOW) == 20
        assert ledger_stats.remaining_minutes(task("a", "claimed", "S"), TIERS, 5, events, NOW) == 0

    def test_a_task_in_pr_counts_only_the_ci_left_after_its_last_pull_request(self):
        events = [
            event("task claimed", "a", NOW - 300 * MINUTE),
            event("task pr", "a", NOW - 60 * MINUTE),
            event("task pr", "a", NOW - 4 * MINUTE),
        ]
        assert ledger_stats.remaining_minutes(task("a", "pr", "L"), TIERS, 10, events, NOW) == 6
        assert ledger_stats.remaining_minutes(task("a", "pr", "L"), TIERS, 3, events, NOW) == 0

    def test_a_task_in_flight_without_a_claim_event_counts_in_full(self):
        assert ledger_stats.remaining_minutes(task("a", "claimed", "M"), TIERS, 5, [], NOW) == 30
        claim = [event("task claimed", "a", NOW - 10 * MINUTE)]
        assert ledger_stats.remaining_minutes(task("a", "pr", "M"), TIERS, 5, claim, NOW) == 5


class TestChainMinutes:
    def test_the_chain_sums_remaining_minutes_along_the_longest_dependency_path(self):
        unfinished = {
            "a": task("a"),
            "b": task("b", depends_on=["a", "done", "missing"]),
            "c": task("c", depends_on=["b"]),
            "d": task("d"),
        }
        left = {"a": 10, "b": 20, "c": 5, "d": 34}
        assert ledger_stats.chain_minutes(unfinished, left) == 35

    def test_a_cycle_ends_the_chain_and_an_empty_ledger_is_zero(self):
        unfinished = {"a": task("a", depends_on=["b"]), "b": task("b", depends_on=["a"])}
        assert ledger_stats.chain_minutes(unfinished, {"a": 10, "b": 15}) == 25
        assert ledger_stats.chain_minutes({}, {}) == 0


class TestCalculate:
    def ledger(self):
        return doc(
            task("done", "done", "L"),
            task("a", "open", "S"),
            task("b", "open", "M", depends_on=["a"]),
            task("c", "claimed", "L"),
            task("d", "open", "S", out_of_scope=True),
        )

    def events(self):
        return [event("task claimed", "c", NOW - 30 * MINUTE)]

    def test_the_throughput_bound_wins_when_slots_are_few(self):
        result = ledger_stats.calculate(self.ledger(), self.events(), NOW, {"slots": 1, "ci_minutes": 5})
        assert result == {
            "minutes": 60,
            "remaining": 3,
            "work": 60,
            "chain": 45,
            "throughput": 60,
            "slots": 1,
            "ci_minutes": 5,
            "tiers": TIERS,
            "stale": True,
            "gap": "",
            "frozen": 0,
            "frozen_work": 0,
        }

    def test_the_chain_bound_wins_when_slots_are_many_and_rounds_up(self):
        result = ledger_stats.calculate(self.ledger(), self.events(), NOW, {"slots": 3, "ci_minutes": 5.3})
        assert (result["minutes"], result["chain"], result["work"], result["throughput"]) == (46, 45.6, 60.9, 20.3)

    def test_no_ci_median_counts_no_ci_time(self):
        result = ledger_stats.calculate(self.ledger(), self.events(), NOW, {"slots": 1, "ci_minutes": None})
        assert (result["minutes"], result["ci_minutes"]) == (45, None)

    def test_unobserved_capacity_and_no_slot_leave_the_estimate_unknown(self):
        unseen = ledger_stats.calculate(self.ledger(), self.events(), NOW)
        assert (unseen["minutes"], unseen["throughput"], unseen["slots"]) == (None, None, None)
        assert unseen["gap"] == "live capacity has not been observed"
        none = ledger_stats.calculate(self.ledger(), self.events(), NOW, {"slots": 0, "ci_minutes": 5})
        assert (none["minutes"], none["throughput"], none["gap"]) == (None, None, "the quota allows no agent slot now")
        assert none["work"] == 60

    def test_nothing_left_is_zero_whatever_the_capacity(self):
        result = ledger_stats.calculate(doc(task("a", "done")), [], NOW)
        assert (result["minutes"], result["remaining"], result["gap"], result["stale"]) == (0, 0, "", True)

    def test_stale_compares_with_the_page_value(self):
        d = self.ledger()
        d["time_left_minutes"] = 60
        assert ledger_stats.calculate(d, self.events(), NOW, {"slots": 1, "ci_minutes": 5})["stale"] is False


class TestFrozenSplit:
    def ledger(self, *freezes):
        d = doc(
            task("a", "open", "S"),
            task("b", "open", "M", depends_on=["a"]),
            task("c", "open", "L", depends_on=["b"]),
            task("d", "claimed", "S"),
            task("e", "open", "S"),
        )
        d["freezes"] = [{"verb": verb, "target": target} for verb, target in freezes]
        return d

    def test_a_frozen_task_and_its_dependents_leave_time_left_and_are_counted_apart(self):
        result = ledger_stats.calculate(self.ledger(("freeze", "tasks/a")), [], NOW, {"slots": 1, "ci_minutes": 5})
        assert (result["minutes"], result["remaining"], result["work"], result["chain"]) == (30, 2, 30, 15)
        assert (result["frozen"], result["frozen_work"]) == (3, 90)

    def test_a_held_chain_no_longer_sets_time_left(self):
        inputs = {"slots": 10, "ci_minutes": 5}
        free = ledger_stats.calculate(self.ledger(), [], NOW, inputs)
        assert (free["minutes"], free["frozen"], free["frozen_work"]) == (90, 0, 0)
        held = ledger_stats.calculate(self.ledger(("freeze", "tasks/b")), [], NOW, inputs)
        assert (held["minutes"], held["chain"], held["remaining"]) == (15, 15, 3)
        assert (held["frozen"], held["frozen_work"]) == (2, 75)

    def test_a_focus_holds_open_work_outside_it_but_not_urgent_or_in_flight_tasks(self):
        d = self.ledger(("focus", "tasks/e"))
        d["tasks"][0]["rank"] = "urgent"
        result = ledger_stats.calculate(d, [], NOW, {"slots": 1, "ci_minutes": 5})
        assert (result["remaining"], result["work"], result["frozen"], result["frozen_work"]) == (3, 45, 2, 75)

    def test_a_phase_freeze_holds_every_open_task_under_it(self):
        result = ledger_stats.calculate(self.ledger(("freeze", "phases/p1")), [], NOW, {"slots": 1, "ci_minutes": 5})
        assert (result["minutes"], result["remaining"], result["frozen"], result["frozen_work"]) == (15, 1, 4, 105)

    def test_the_tick_fix_phase_stays_claimable_under_a_focus(self):
        inputs = {"slots": 1, "ci_minutes": 5, "fix_phase": "p1"}
        result = ledger_stats.calculate(self.ledger(("focus", "tasks/e")), [], NOW, inputs)
        assert (result["remaining"], result["frozen"]) == (5, 0)

    def test_a_watched_swarm_focus_holds_open_work_outside_the_fix_phase(self):
        inputs = {
            "slots": 1,
            "ci_minutes": 5,
            "fix_phase": "p9",
            "watched": ["the focus on task x in the watched swarm"],
        }
        result = ledger_stats.calculate(self.ledger(), [], NOW, inputs)
        assert (result["remaining"], result["frozen"], result["frozen_work"]) == (1, 4, 105)

    def test_a_member_merged_into_a_group_is_not_counted_twice(self):
        d = doc(task("g", "open", "S", group_members=["m"]), task("m", "open", "S", merged_into="g"))
        result = ledger_stats.calculate(d, [], NOW, {"slots": 1, "ci_minutes": 5})
        assert (result["remaining"], result["work"], result["frozen"]) == (1, 15, 0)

    def test_frozen_work_rounds_to_one_decimal(self):
        result = ledger_stats.calculate(self.ledger(("freeze", "tasks/e")), [], NOW, {"slots": 1, "ci_minutes": 5.37})
        assert result["frozen_work"] == 15.4

    def test_only_frozen_work_left_reads_zero_with_the_frozen_remainder_named(self):
        d = doc(task("a", "open", "S"), time_left=30)
        d["freezes"] = [{"verb": "freeze", "target": "tasks/a"}]
        result = ledger_stats.calculate(d, [], NOW)
        assert (result["minutes"], result["remaining"], result["gap"]) == (0, 0, "")
        assert (result["frozen"], result["frozen_work"]) == (1, 10)
        assert ledger_stats.time_left_line(d, [], NOW, result) == (
            "the page shows 0h 30m, computed 0h 0m with no task remaining, "
            "with 1 frozen task holding 10m of work left out, stale"
        )

    def test_the_line_names_the_frozen_remainder_beside_the_live_estimate(self):
        d = self.ledger(("freeze", "tasks/a"))
        result = ledger_stats.calculate(d, [], NOW, {"slots": 1, "ci_minutes": 5})
        assert ledger_stats.time_left_line(d, [], NOW, result) == (
            "the page shows not set, computed 0h 30m as the larger of a 15m chain and 30m of work over 1 slots, "
            "for 2 remaining tasks at S 10m, M 25m, L 40m and 5m of CI, "
            "with 3 frozen tasks holding 90m of work left out, stale"
        )


class TestOutageHour:
    """The tick was down for the last hour: no claims or pull requests were recorded inside it."""

    def backlog(self):
        rows = [task(f"o{n}", "open", "M") for n in range(20)]
        rows += [task(f"c{n}", "claimed", "M") for n in range(4)]
        return doc(*rows, *(task(f"m{n}", "done", "M") for n in range(30)))

    def history(self, until):
        events = []
        for n in range(30):
            pr = until - (n + 1) * 40 * MINUTE
            events += [
                event("task claimed", f"m{n}", pr - 20 * MINUTE),
                event("task pr", f"m{n}", pr),
                event("task done", f"m{n}", pr + 5 * MINUTE),
            ]
        return events

    def test_an_outage_hour_stays_within_a_quarter_of_steady_state(self):
        inputs = {"slots": 4, "ci_minutes": 10}
        steady = self.history(NOW) + [event("task claimed", f"c{n}", NOW - 10 * MINUTE) for n in range(4)]
        outage = self.history(NOW - HOUR) + [event("task claimed", f"c{n}", NOW - 70 * MINUTE) for n in range(4)]
        assert ledger_stats.closed_last_hour(self.backlog(), steady, NOW) != []
        assert ledger_stats.closed_last_hour(self.backlog(), outage, NOW) == []
        assert not [e for e in outage if NOW - e["at"] < HOUR]
        before = ledger_stats.calculate(self.backlog(), steady, NOW, inputs)["minutes"]
        during = ledger_stats.calculate(self.backlog(), outage, NOW, inputs)["minutes"]
        assert (before, during) == (170, 150)
        assert abs(during - before) <= before / 4


class TestTimeLeftLine:
    def test_a_computed_line_names_every_input(self):
        result = {
            "minutes": 70,
            "remaining": 3,
            "work": 70,
            "chain": 45.5,
            "throughput": 70,
            "slots": 1,
            "ci_minutes": 5,
            "tiers": TIERS,
            "stale": True,
            "gap": "",
        }
        assert ledger_stats.time_left_line(doc(time_left=60), [], NOW, result) == (
            "the page shows 1h 0m, computed 1h 10m as the larger of a 45.5m chain and 70m of work over 1 slots, "
            "for 3 remaining tasks at S 10m, M 25m, L 40m and 5m of CI, stale"
        )

    def test_a_missing_ci_median_and_a_current_value_read_plainly(self):
        result = ledger_stats.calculate(
            doc(task("a", "open", "S"), time_left=10), [], NOW, {"slots": 2, "ci_minutes": None}
        )
        assert ledger_stats.time_left_line(doc(time_left=10), [], NOW, result) == (
            "the page shows 0h 10m, computed 0h 10m as the larger of a 10m chain and 10m of work over 2 slots, "
            "for 1 remaining tasks at S 10m, M 25m, L 40m and no CI median yet, current"
        )

    def test_an_unknown_line_names_its_gap(self):
        assert ledger_stats.time_left_line(doc(task("a"), time_left=90), [], NOW) == (
            "the page shows 1h 30m, code cannot compute it: live capacity has not been observed"
        )


class TestTimeLeftOp:
    def ctx(self, events=(), meta=None):
        return ledger_core.Context({"rev": 0, "stamps": {}, "events": list(events), "members": {}, **(meta or {})}, NOW)

    def op(self, slots=1, ci_minutes=5):
        return {"op": "time_left", "id": "t1", "by": "swarm", "slots": slots, "ci_minutes": ci_minutes}

    def test_the_op_is_registered_with_the_core_and_the_api_schema(self):
        from scripts.swarm_ledger.api import schemas

        assert ledger_core.EXTENSION_OPS["time_left"].OPS == ("time_left",)
        assert schemas.FIELDS["time_left"] == "by slots ci_minutes fix_phase watched"
        assert schemas.TYPES["watched"] == {
            "type": "array",
            "maxItems": 100,
            "items": {"type": "string", "maxLength": 2000},
        }
        assert schemas.TYPES["slots"] == {"type": ["integer", "null"], "minimum": 0}
        assert schemas.TYPES["ci_minutes"] == {"type": ["number", "null"], "minimum": 0}

    def test_check_accepts_the_swarm_write(self):
        assert ledger_time_left.check(self.op()) is None
        assert ledger_time_left.check(self.op(0, None)) is None
        assert ledger_time_left.check(self.op(2, 7)) is None
        assert ledger_time_left.check(self.op(None, 7)) is None

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"by": "eng"}, "time_left takes id, by swarm, slots and ci_minutes, and optionally fix_phase and watched"),
            ({"extra": 1}, "time_left takes id, by swarm, slots and ci_minutes, and optionally fix_phase and watched"),
            ({"slots": -1}, "slots must be a nonnegative integer or null"),
            ({"slots": True}, "slots must be a nonnegative integer or null"),
            ({"slots": 1.0}, "slots must be a nonnegative integer or null"),
            ({"ci_minutes": -0.5}, "ci_minutes must be a nonnegative number or null"),
            ({"ci_minutes": "5"}, "ci_minutes must be a nonnegative number or null"),
            ({"ci_minutes": False}, "ci_minutes must be a nonnegative number or null"),
        ],
    )
    def test_check_refuses_anything_else(self, change, message):
        with pytest.raises(ValueError) as refused:
            ledger_time_left.check({**self.op(), **change})
        assert str(refused.value) == message

    def test_check_refuses_a_missing_field(self):
        op = self.op()
        del op["ci_minutes"]
        with pytest.raises(ValueError) as refused:
            ledger_time_left.check(op)
        assert (
            str(refused.value)
            == "time_left takes id, by swarm, slots and ci_minutes, and optionally fix_phase and watched"
        )

    def test_check_takes_the_tick_focus_and_refuses_a_malformed_one(self):
        assert ledger_time_left.check({**self.op(), "fix_phase": "p1", "watched": ["the focus on x"]}) is None
        for change, message in (
            ({"fix_phase": 3}, "fix_phase must be text"),
            ({"watched": "x"}, "watched must be a list of text"),
            ({"watched": [1]}, "watched must be a list of text"),
        ):
            with pytest.raises(ValueError) as refused:
                ledger_time_left.check({**self.op(), **change})
            assert str(refused.value) == message

    def test_apply_keeps_the_tick_focus_in_the_inputs(self):
        d = doc(task("a", "open", "S"), task("b", "open", "S", phase="p2"))
        ctx = self.ctx()
        ledger_time_left.apply(d, {**self.op(), "fix_phase": "p2", "watched": ["the focus on x"]}, ctx)
        assert ctx.meta["time_left"]["inputs"] == {
            "slots": 1,
            "ci_minutes": 5,
            "fix_phase": "p2",
            "watched": ["the focus on x"],
        }
        assert (ctx.meta["time_left"]["calculation"]["remaining"], ctx.meta["time_left"]["calculation"]["frozen"]) == (
            1,
            1,
        )

    def test_apply_writes_the_minutes_and_keeps_every_input_beside_them(self):
        d = doc(task("a", "open", "S"), time_left=400)
        ctx = self.ctx()
        assert ledger_time_left.apply(d, self.op(), ctx) is True
        assert d["time_left_minutes"] == 15
        assert ctx.dirty is True
        assert ctx.stamps == {"time_left_minutes": {"at": NOW, "rev": 1, "by": "stats"}}
        assert ctx.events == []
        assert ctx.meta["time_left"] == {
            "at": NOW,
            "inputs": {"slots": 1, "ci_minutes": 5},
            "calculation": ledger_stats.calculate(
                doc(task("a", "open", "S"), time_left=400), [], NOW, {"slots": 1, "ci_minutes": 5}
            ),
        }

    def test_an_unchanged_calculation_still_stamps_its_refresh_time(self):
        d = doc(task("a", "open", "S"), time_left=15)
        result = ledger_stats.calculate(d, [], NOW, {"slots": 1, "ci_minutes": 5})
        ctx = self.ctx(meta={"time_left": {"at": 1, "inputs": {"slots": 1, "ci_minutes": 5}, "calculation": result}})
        assert ledger_time_left.apply(d, self.op(), ctx) is True
        assert ctx.dirty is True
        assert ctx.stamps == {}
        assert ctx.meta["time_left"]["at"] == NOW

    def test_every_tick_write_stamps_its_own_refresh_time(self):
        d = doc(task("a", "open", "S"), time_left=15)
        meta = {"rev": 0, "stamps": {}, "events": [], "members": {}}
        for tick in range(3):
            at = NOW + tick * MINUTE
            ledger_time_left.apply(d, self.op(), ledger_core.Context(meta, at))
            assert meta["time_left"]["at"] == at

    def test_an_unknown_estimate_keeps_the_prior_value(self):
        d = doc(task("a", "open", "S"), time_left=400)
        ctx = self.ctx()
        assert ledger_time_left.apply(d, self.op(slots=0), ctx) is True
        assert d["time_left_minutes"] == 400
        assert ctx.stamps == {}
        assert ctx.dirty is True
        assert ctx.meta["time_left"]["calculation"]["gap"] == "the quota allows no agent slot now"

    def test_the_calculation_sees_events_recorded_earlier_in_the_same_write(self):
        d = doc(task("a", "claimed", "L"))
        ctx = self.ctx()
        ctx.events.append(event("task claimed", "a", NOW - 10 * MINUTE))
        ledger_time_left.apply(d, self.op(ci_minutes=0), ctx)
        assert d["time_left_minutes"] == 30

    def test_the_stats_refresh_reuses_the_last_tick_inputs(self):
        d = doc(task("a", "open", "M"), time_left=400)
        ctx = self.ctx(meta={"time_left": {"at": 1, "inputs": {"slots": 2, "ci_minutes": 5}, "calculation": {}}})
        text = ledger_stats.refresh(d, ctx, "s1")
        assert d["time_left_minutes"] == 30
        assert ctx.meta["stats_refresh"]["calculation"]["slots"] == 2
        assert "computed 0h 30m" in text


class TestPageInputs:
    def run(self, body):
        import subprocess

        from tests.swarm_ledger.test_fold import function_source

        script = function_source("timeLeftInputs") + '\nconst assert = require("node:assert/strict");\n' + body
        subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)

    def test_the_hover_names_every_input_of_the_calculation(self):
        self.run(
            "const calc = {chain: 45.5, work: 60, slots: 2, remaining: 3, ci_minutes: 5,"
            " tiers: {S: {minutes: 10, samples: 4}, M: {minutes: 22.5, samples: 6}, L: {minutes: 40, samples: 0}}};\n"
            "assert.equal(timeLeftInputs(calc), 'Larger of a 45.5m chain and 60m of work over 2 slots · 3 tasks left"
            " · S 10m from 4 samples, M 22.5m from 6 samples, L 40m from 0 samples · CI 5m a task');\n"
        )

    def test_missing_capacity_and_ci_read_plainly_and_no_calculation_has_no_hover(self):
        self.run(
            "const calc = {chain: 0, work: 10, slots: null, remaining: 1, ci_minutes: null,"
            " tiers: {S: {minutes: 10, samples: 0}}};\n"
            "assert.equal(timeLeftInputs(calc), 'Larger of a 0m chain and 10m of work over unobserved slots · 1 tasks left"
            " · S 10m from 0 samples · no CI median yet');\n"
            "assert.equal(timeLeftInputs(undefined), undefined);\n"
            "assert.equal(timeLeftInputs({gap: 'x'}), undefined);\n"
        )

    def test_the_hover_names_the_frozen_remainder_left_out(self):
        self.run(
            "const calc = {chain: 15, work: 30, slots: 1, remaining: 2, ci_minutes: 5, frozen: 3, frozen_work: 90,"
            " tiers: {S: {minutes: 10, samples: 0}}};\n"
            "assert.equal(timeLeftInputs(calc), 'Larger of a 15m chain and 30m of work over 1 slots · 2 tasks left"
            " · S 10m from 0 samples · CI 5m a task · 3 frozen tasks with 90m of work left out');\n"
        )


class TestPrecision:
    def test_measured_minutes_round_to_one_decimal(self):
        rows, events = spans("S", [10.37] * 5)
        assert ledger_stats.agent_minutes(doc(*rows), events, NOW)["S"] == {"minutes": 10.4, "samples": 5}

    def test_work_chain_and_throughput_round_to_one_decimal(self):
        result = ledger_stats.calculate(doc(task("a", "open", "S")), [], NOW, {"slots": 3, "ci_minutes": 5.37})
        assert (result["work"], result["chain"], result["throughput"], result["minutes"]) == (15.4, 15.4, 5.1, 16)

    def test_check_accepts_zero_and_fractional_ci_minutes(self):
        op = {"op": "time_left", "id": "t1", "by": "swarm", "slots": 1}
        assert ledger_time_left.check({**op, "ci_minutes": 0}) is None
        assert ledger_time_left.check({**op, "ci_minutes": 0.5}) is None


class TestClockUse:
    def in_flight(self):
        return doc(task("a", "claimed", "S"), time_left=30)

    def claim(self):
        return [event("task claimed", "a", NOW - 10 * MINUTE)]

    def test_the_review_counts_in_flight_time_against_now(self):
        meta = {"events": self.claim(), "time_left": {"inputs": {"slots": 1, "ci_minutes": 4}}}
        assert "computed 0h 4m as the larger of a 4m chain" in ledger_stats.review(self.in_flight(), meta, NOW)

    def test_the_stats_refresh_counts_in_flight_time_against_its_clock(self):
        d = self.in_flight()
        meta = {"rev": 0, "stamps": {}, "events": self.claim(), "members": {}}
        meta["time_left"] = {"inputs": {"slots": 1, "ci_minutes": 4}}
        ctx = ledger_core.Context(meta, NOW)
        ledger_stats.refresh(d, ctx, "clock")
        assert ctx.meta["stats_refresh"]["state"] == "refreshed"
        assert d["time_left_minutes"] == 4

    def test_a_finished_ledger_names_the_page_value(self):
        assert ledger_stats.time_left_line(doc(task("a", "done"), time_left=30), [], NOW) == (
            "the page shows 0h 30m, computed 0h 0m with no task remaining, stale"
        )
