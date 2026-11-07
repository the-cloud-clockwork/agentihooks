"""The moved reap watch helpers judge an agent's pane through the tick seams, one branch per test."""

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import automated_gc, elapsed, tick
from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import MASTER, AgentRecord
from scripts.tick.steps import agents

pytestmark = pytest.mark.unit

NOW = 10_000_000
QUIET = 5_000


class Calls(list[tuple[Any, ...]]):
    """Every fake call in order, as its name and arguments."""

    def fn(self, name: str, result: Any = None) -> Callable[..., Any]:
        """Return a fake that records its call under ``name`` and answers ``result``."""

        def call(*args: Any, **kwargs: Any) -> Any:
            """Record the call and answer the fixed result."""
            self.append((name, args, kwargs) if kwargs else (name, args))
            return result

        return call

    def names(self) -> list[str]:
        """Return the names of the recorded calls in order."""
        return [call[0] for call in self]


def _agent(**fields: Any) -> AgentRecord:
    """Return an engineer agent holding task t1 with ``fields`` replaced."""
    base: dict[str, Any] = {"name": "eng@1-1", "lane": "eng", "task": "t1"}
    return AgentRecord(**(base | fields))


def _store(calls: Calls) -> Any:
    """Return a store whose writes are recorded."""
    redis = SimpleNamespace(set=calls.fn("redis.set"), hexists=lambda key, field: False)
    return SimpleNamespace(
        redis=redis, key=lambda slug, *parts: "/".join((slug, *parts)), put_agent=calls.fn("put_agent")
    )


def _idle_state(calls: Calls, verdict: str = "idle", last: int | None = None) -> Any:
    """Return an idle state module answering ``verdict`` and the last prompt time ``last``."""
    return SimpleNamespace(
        WORKING="working",
        WAITING="waiting",
        IDLE="idle",
        BEAT_TTL_S=77,
        verdict=calls.fn("verdict", verdict),
        heartbeat=calls.fn("heartbeat", "beat"),
        wait=calls.fn("wait", "waited"),
        last_prompt=calls.fn("last_prompt", last),
    )


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(
    ("typed", "last", "held"),
    [("ls", None, True), ("", None, False), ("", NOW - QUIET + 1, True), ("", NOW - QUIET, False)],
)
def test_the_operator_holds_the_pane_while_typing_or_inside_the_quiet_window(
    monkeypatch: pytest.MonkeyPatch, typed: str, last: int | None, held: bool
) -> None:
    calls = Calls()
    monkeypatch.setattr(tick, "idle_state", _idle_state(calls, last=last))
    monkeypatch.setattr(tick, "wake", SimpleNamespace(quiet_ms=calls.fn("quiet_ms", QUIET)))
    store = _store(calls)
    observed = PaneObservation("idle", typed=typed)
    assert agents._operator_at_pane("sw", store, _agent(), observed, NOW) is held
    assert calls[0] == ("last_prompt", (store.redis, "sw", "eng@1-1"))
    assert calls[1:] in ([], [("quiet_ms", (tick.os.environ,))])
    assert len(calls) == (1 if typed or last is None else 2)


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(
    ("refusal", "rows", "at_pane", "repeated", "due"),
    [
        (False, {"t1": {"claimed_by": "eng@1-1"}}, False, True, False),
        (True, {}, False, True, False),
        (True, {"t1": {}}, False, True, False),
        (True, {"t1": {"claimed_by": "other@1-2"}}, False, True, False),
        (True, {"t1": {"claimed_by": "eng@1-1"}}, True, True, False),
        (True, {"t1": {"claimed_by": "eng@1-1"}}, False, True, True),
        (True, {"t1": {"claimed_by": "eng@1-1"}}, False, False, False),
    ],
)
def test_a_content_refusal_is_due_only_for_the_claimant_away_from_the_operator(
    monkeypatch: pytest.MonkeyPatch,
    refusal: bool,
    rows: dict[str, dict[str, Any]],
    at_pane: bool,
    repeated: bool,
    due: bool,
) -> None:
    calls = Calls()
    monkeypatch.setattr(tick, "_operator_at_pane", calls.fn("at_pane", at_pane))
    monkeypatch.setattr(tick, "content_refusal", SimpleNamespace(repeated=calls.fn("repeated", repeated)))
    agent, observed = _agent(), PaneObservation("idle", content_refusal=refusal)
    assert agents._refusal_due("sw", "store", rows, agent, observed, NOW) is due
    claimant = refusal and rows.get("t1", {}).get("claimed_by") == "eng@1-1"
    expected: list[tuple[Any, ...]] = [("at_pane", ("sw", "store", agent, observed, NOW))] if claimant else []
    if claimant and not at_pane:
        expected.append(("repeated", ("store", "sw", agent, observed)))
    assert calls == expected


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(
    "case",
    [
        (MASTER, "API Error: 529", False, True, "idle", True, False),
        ("eng", "", False, True, "idle", True, False),
        ("eng", "API Error: 529", True, True, "idle", True, False),
        ("eng", "API Error: 529", False, True, "working", False, True),
        ("eng", "API Error: 529", False, False, "idle", True, True),
        ("eng", "API Error: 529", False, False, "idle", False, False),
        ("eng", "API Error: 529", False, False, "working", True, False),
    ],
)
def test_an_agent_is_stuck_on_its_provider_when_recovery_is_spent_or_the_idle_outage_ran_past_its_limit(
    monkeypatch: pytest.MonkeyPatch, case: tuple[str, str, bool, bool, str, bool, bool]
) -> None:
    lane, error, at_pane, exhausted, verdict, past, stuck = case
    calls = Calls()
    monkeypatch.setattr(tick, "_operator_at_pane", calls.fn("at_pane", at_pane))
    monkeypatch.setattr(tick, "idle_state", _idle_state(calls, verdict=verdict))
    monkeypatch.setattr(tick, "outage", SimpleNamespace(held_past_limit=calls.fn("past", past)))
    store, agent = _store(calls), _agent(lane=lane)
    observed = PaneObservation("waiting", provider_error=error)
    assert agents._stuck_on_provider("sw", store, agent, observed, exhausted, NOW) is stuck
    asked = [("at_pane", ("sw", store, agent, observed, NOW))] if lane != MASTER and error else []
    assert [call for call in calls if call[0] == "at_pane"] == asked
    judged = lane != MASTER and bool(error) and not at_pane and not exhausted
    if judged:
        expected: list[tuple[Any, ...]] = [
            ("heartbeat", (store.redis, "sw", "eng@1-1")),
            ("wait", (store.redis, "sw", "eng@1-1")),
            ("verdict", ("waiting", "beat", "waited", NOW)),
        ]
        expected += [("past", (store, "sw", "eng@1-1", NOW))] if verdict == "idle" else []
        assert calls[-len(expected) :] == expected
    assert ("verdict" in calls.names()) is judged
    assert ("past" in calls.names()) is (judged and verdict == "idle")


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(
    "case",
    [
        (_agent(lane=MASTER), False, False, False, False, False, False),
        (_agent(task=""), False, False, False, False, False, False),
        (_agent(), True, False, False, False, False, False),
        (_agent(), False, True, False, False, False, False),
        (_agent(), False, False, True, False, False, False),
        (_agent(), False, False, False, True, False, False),
        (_agent(), False, False, False, False, True, False),
        (_agent(), False, False, False, False, False, True),
    ],
)
def test_a_stale_claimant_is_nudged_only_outside_outages_retirement_and_the_operator(
    monkeypatch: pytest.MonkeyPatch, case: tuple[AgentRecord, bool, bool, bool, bool, bool, bool]
) -> None:
    agent, pending, outage, due, at_pane, holding, nudged = case
    calls = Calls()
    monkeypatch.setattr(tick, "outage", SimpleNamespace(open_records=calls.fn("open_records", outage)))
    monkeypatch.setattr(tick, "elapsed", SimpleNamespace(IDLE_SINCE=elapsed.IDLE_SINCE, due=calls.fn("due", due)))
    monkeypatch.setattr(tick, "_operator_at_pane", calls.fn("at_pane", at_pane))
    monkeypatch.setattr(tick, "ledger_probe", SimpleNamespace(holding=calls.fn("holding", holding)))
    monkeypatch.setattr(tick, "InboxStore", lambda redis: ("inbox", redis))
    monkeypatch.setattr(tick, "stale_nudge", SimpleNamespace(nudge=calls.fn("nudge", ["nudged eng@1-1"])))
    store = _store(calls)
    observed = PaneObservation("idle", provider_error="boom")
    said = agents._stale_nudge("sw", store, agent, observed, pending, NOW)
    assert said == (["nudged eng@1-1"] if nudged else [])
    if nudged:
        assert calls == [
            ("open_records", ("sw",)),
            ("due", (store, "sw", elapsed.IDLE_SINCE, "eng@1-1", NOW, 0, tick.IDLE_KILL_MS)),
            ("at_pane", ("sw", store, agent, observed, NOW)),
            ("holding", (store, "sw")),
            ("nudge", (("inbox", store.redis), "sw", agent, "boom", NOW)),
        ]
    if due:
        assert calls.names() == ["open_records", "due"]


def _watch(
    monkeypatch: pytest.MonkeyPatch,
    observed: PaneObservation,
    **answers: Any,
) -> tuple[Calls, Any]:
    """Patch every seam the idle watch reads, answering from ``answers`` and recording each call."""
    calls = Calls()

    def answer(name: str, default: Any) -> Any:
        """Return the scenario's answer for ``name``, else ``default``."""
        return answers.get(name, default)

    monkeypatch.setattr(tick, "_observe", calls.fn("observe", observed))
    monkeypatch.setattr(tick, "_refusal_due", calls.fn("refusal_due", answer("refusal_due", False)))
    monkeypatch.setattr(tick, "_free_refused", calls.fn("free_refused", ["freed refused"]))
    monkeypatch.setattr(tick, "_stuck_on_provider", calls.fn("stuck", answer("stuck", False)))
    monkeypatch.setattr(tick, "_free_stuck", calls.fn("free_stuck", ["freed stuck"]))
    monkeypatch.setattr(tick, "_stale_nudge", calls.fn("stale_nudge", answer("nudged", [])))
    monkeypatch.setattr(tick, "_operator_at_pane", calls.fn("at_pane", answer("at_pane", False)))
    monkeypatch.setattr(tick, "_drop", calls.fn("drop", ", task reopened"))
    monkeypatch.setattr(tick, "IDLE_KILL_TICKS", 3)
    monkeypatch.setattr(
        tick,
        "outage",
        SimpleNamespace(mark_pane=calls.fn("mark_pane"), held=calls.fn("held", answer("held", False))),
    )
    monkeypatch.setattr(
        tick,
        "input_wait",
        SimpleNamespace(mark=calls.fn("mark", answer("began", NOW)), clear=calls.fn("clear")),
    )
    monkeypatch.setattr(
        tick,
        "master_questions",
        SimpleNamespace(dismiss=calls.fn("dismiss", ["dismissed"]), drop=calls.fn("drop_questions")),
    )
    monkeypatch.setattr(
        tick,
        "resume",
        SimpleNamespace(
            recovery_exhausted=calls.fn("exhausted", answer("exhausted", False)),
            recovery_pending=calls.fn("pending", answer("pending", False)),
            recovery_holds=calls.fn("recovery_holds", answer("recovery_holds", False)),
            recovery_blocker=calls.fn("blocker", "no account"),
        ),
    )
    monkeypatch.setattr(tick, "idle_state", _idle_state(calls, verdict=answer("verdict", "idle")))
    monkeypatch.setattr(
        tick,
        "elapsed",
        SimpleNamespace(
            IDLE_SINCE=elapsed.IDLE_SINCE,
            passes=elapsed.passes,
            hold=calls.fn("hold"),
            start=calls.fn("start", answer("since", NOW)),
        ),
    )
    monkeypatch.setattr(tick, "ledger_probe", SimpleNamespace(holding=calls.fn("holding", answer("holding", False))))
    monkeypatch.setattr(automated_gc, "owned", calls.fn("gc_owned", answer("gc_owned", False)))
    monkeypatch.setattr(
        tick,
        "gate_log",
        SimpleNamespace(append=calls.fn("gate_log"), Row=SimpleNamespace(of=lambda *a, **k: ("row", a, k))),
    )
    monkeypatch.setattr(tick, "reaper", SimpleNamespace(scratch_homes=lambda slug, task: (slug, task, "homes")))
    runtime = SimpleNamespace(retire=calls.fn("retire", answer("retired", True)))
    return calls, runtime


def _puts(calls: Calls) -> list[AgentRecord]:
    """Return the agent records the watch saved, in order."""
    return [call[1][1] for call in calls if call[0] == "put_agent"]


ROWS: dict[str, dict[str, Any]] = {"t1": {"claimed_by": "eng@1-1"}}


@pytest.mark.feature("swarm-tick-agents-reap")
def test_a_due_refusal_frees_the_agent_before_the_pane_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = PaneObservation("idle", content_refusal=True)
    calls, runtime = _watch(monkeypatch, observed, refusal_due=True)
    store, agent = _store(calls), _agent()
    assert agents._watch_idle("sw", store, "ledger", runtime, ROWS, agent, NOW) == ["freed refused"]
    assert calls == [
        ("observe", (runtime, agent)),
        ("refusal_due", ("sw", store, ROWS, agent, observed, NOW)),
        ("free_refused", ("sw", store, "ledger", runtime, ROWS, agent, NOW)),
    ]


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(
    ("title", "prompt", "ticks", "mark_ticks"),
    [("Allow edit?", "Allow edit?", 4, 4), ("Allow edit?", "Other", 4, 0), ("", "", 2, 2)],
)
def test_a_waiting_pane_records_its_prompt_and_input_wait(
    monkeypatch: pytest.MonkeyPatch, title: str, prompt: str, ticks: int, mark_ticks: int
) -> None:
    observed = PaneObservation("waiting", prompt_title=title)
    began = NOW - 3 * elapsed.PASS_MS
    calls, runtime = _watch(monkeypatch, observed, began=began)
    store = _store(calls)
    agent = _agent(input_prompt=prompt or "Waiting on input", input_ticks=ticks, idle_ticks=5)
    assert agents._watch_idle("sw", store, "ledger", runtime, ROWS, agent, NOW) == []
    named = title or "Waiting on input"
    assert ("redis.set", ("sw/pane-state/eng@1-1", "waiting"), {"ex": 77}) in calls
    assert ("mark_pane", (store, "sw", "eng@1-1", "", NOW)) in calls
    assert ("mark", (store, "sw", "eng@1-1", NOW, mark_ticks)) in calls
    assert _puts(calls) == [replace(agent, input_prompt=named, input_ticks=elapsed.passes(NOW - began), idle_ticks=0)]
    assert calls.names()[-1] == "put_agent"


@pytest.mark.feature("swarm-tick-agents-reap")
def test_a_waiting_master_dismisses_its_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = PaneObservation("waiting", prompt_title="Pick one")
    calls, runtime = _watch(monkeypatch, observed)
    store, agent = _store(calls), _agent(lane=MASTER)
    assert agents._watch_idle("sw", store, "ledger", runtime, ROWS, agent, NOW) == ["dismissed"]
    waiting = _puts(calls)[0]
    assert calls[-1] == ("dismiss", ("sw", store, "ledger", runtime, waiting, NOW))


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(("title", "typed", "waits"), [("", "", False), ("Allow?", "", True), ("", "y", True)])
def test_a_provider_error_reads_an_untouched_pane_as_idle(
    monkeypatch: pytest.MonkeyPatch, title: str, typed: str, waits: bool
) -> None:
    observed = PaneObservation("waiting", prompt_title=title, typed=typed, provider_error="API Error: 529")
    calls, runtime = _watch(monkeypatch, observed)
    store = _store(calls)
    agents._watch_idle("sw", store, "ledger", runtime, {}, _agent(), NOW)
    assert ("mark" in calls.names()) is waits
    assert ("mark_pane", (store, "sw", "eng@1-1", "API Error: 529", NOW)) in calls
    if not waits:
        stuck = next(call for call in calls if call[0] == "stuck")
        assert stuck[1][3] == PaneObservation("idle", provider_error="API Error: 529")


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(("lane", "dropped"), [("eng", False), (MASTER, True)])
def test_a_pane_that_stopped_waiting_clears_the_input_wait(
    monkeypatch: pytest.MonkeyPatch, lane: str, dropped: bool
) -> None:
    calls, runtime = _watch(monkeypatch, PaneObservation("working"), verdict="working")
    store = _store(calls)
    agent = _agent(lane=lane, input_prompt="Allow?", input_ticks=3, idle_ticks=2)
    agents._watch_idle("sw", store, "ledger", runtime, {}, agent, NOW)
    assert ("clear", (store, "sw", "eng@1-1")) in calls
    assert (("drop_questions", (store, "sw", "eng@1-1")) in calls) is dropped
    assert all(put.input_prompt == "" and put.input_ticks == 0 for put in _puts(calls))


@pytest.mark.feature("swarm-tick-agents-reap")
def test_a_pane_never_waiting_keeps_its_input_wait_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, runtime = _watch(monkeypatch, PaneObservation("working"), verdict="working")
    agents._watch_idle("sw", _store(calls), "ledger", runtime, {}, _agent(lane=MASTER), NOW)
    assert "clear" not in calls.names()
    assert "drop_questions" not in calls.names()


@pytest.mark.feature("swarm-tick-agents-reap")
def test_an_agent_stuck_on_its_provider_is_freed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, runtime = _watch(monkeypatch, PaneObservation("idle"), stuck=True, exhausted="spent", pending="queued")
    store, agent = _store(calls), _agent()
    assert agents._watch_idle("sw", store, "ledger", runtime, ROWS, agent, NOW) == ["freed stuck"]
    assert calls[-5:] == [
        ("held", (store, "sw", "eng@1-1")),
        ("exhausted", (store, "sw", agent)),
        ("pending", (store, "sw", agent)),
        ("stuck", ("sw", store, agent, PaneObservation("idle"), "spent", NOW)),
        ("free_stuck", ("sw", store, "ledger", runtime, ROWS, agent, NOW)),
    ]
    assert next(call for call in calls if call[0] == "stuck")[1][4] == "spent"


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(
    ("lane", "held", "exhausted", "holds"),
    [
        (MASTER, False, False, True),
        ("eng", True, False, True),
        ("eng", True, True, False),
        ("eng", False, False, False),
    ],
)
def test_a_master_or_an_outage_held_agent_keeps_its_idle_count(
    monkeypatch: pytest.MonkeyPatch, lane: str, held: bool, exhausted: bool, holds: bool
) -> None:
    calls, runtime = _watch(
        monkeypatch, PaneObservation("idle"), held=held, exhausted=exhausted, nudged=["nudged"], verdict="working"
    )
    store, agent = _store(calls), _agent(lane=lane, idle_ticks=4)
    assert agents._watch_idle("sw", store, "ledger", runtime, ROWS, agent, NOW) == ["nudged"]
    assert ("stale_nudge", ("sw", store, agent, PaneObservation("idle"), False, NOW)) in calls
    hold = ("hold", (store, "sw", elapsed.IDLE_SINCE, "eng@1-1", 4))
    assert (hold in calls) is holds
    assert ("verdict" in calls.names()) is not holds
    assert _puts(calls) == [agent if holds else replace(agent, idle_ticks=0)]


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(("rows", "nudged"), [({}, False), ({"t1": {"claimed_by": "other@1-2"}}, False), (ROWS, True)])
def test_only_the_claimant_is_nudged(
    monkeypatch: pytest.MonkeyPatch, rows: dict[str, dict[str, Any]], nudged: bool
) -> None:
    calls, runtime = _watch(monkeypatch, PaneObservation("idle"), nudged=["nudged"], verdict="working")
    said = agents._watch_idle("sw", _store(calls), "ledger", runtime, rows, _agent(), NOW)
    assert said == (["nudged"] if nudged else [])
    assert ("stale_nudge" in calls.names()) is nudged


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize("verdict", ["working", "waiting"])
def test_a_working_or_waiting_verdict_resets_the_idle_count(monkeypatch: pytest.MonkeyPatch, verdict: str) -> None:
    calls, runtime = _watch(monkeypatch, PaneObservation("idle"), verdict=verdict, pending="queued")
    store, agent = _store(calls), _agent(idle_ticks=6)
    assert agents._watch_idle("sw", store, "ledger", runtime, {}, agent, NOW) == []
    assert ("verdict", ("idle", "beat", "waited", NOW)) in calls
    assert ("heartbeat", (store.redis, "sw", "eng@1-1")) in calls
    assert ("wait", (store.redis, "sw", "eng@1-1")) in calls
    assert _puts(calls) == [replace(agent, idle_ticks=0)]
    assert "start" not in calls.names()


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize("holder", ["at_pane", "holding", "gc_owned"])
def test_the_operator_a_ledger_probe_or_gc_holds_the_idle_count(monkeypatch: pytest.MonkeyPatch, holder: str) -> None:
    calls, runtime = _watch(monkeypatch, PaneObservation("idle"), **{holder: True})
    store, agent = _store(calls), _agent(idle_ticks=2)
    assert agents._watch_idle("sw", store, "ledger", runtime, {}, agent, NOW) == []
    assert calls[-2:] == [("hold", (store, "sw", elapsed.IDLE_SINCE, "eng@1-1", 2)), ("put_agent", ("sw", agent))]
    assert "start" not in calls.names()
    if holder == "at_pane":
        assert ("at_pane", ("sw", store, agent, PaneObservation("idle"), NOW)) in calls
    if holder == "holding":
        assert ("holding", (store, "sw")) in calls
    if holder == "gc_owned":
        assert ("gc_owned", ("sw", agent)) in calls


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(("recovering", "count"), [(True, 1), (False, 2)])
def test_an_idle_agent_counts_its_idle_ticks_and_logs_each_one(
    monkeypatch: pytest.MonkeyPatch, recovering: bool, count: int
) -> None:
    since = NOW - (count - 1) * elapsed.PASS_MS
    calls, runtime = _watch(monkeypatch, PaneObservation("idle"), since=since, recovery_holds=recovering)
    store, agent = _store(calls), _agent(idle_ticks=1)
    assert agents._watch_idle("sw", store, "ledger", runtime, {}, agent, NOW) == []
    ticks = elapsed.passes(NOW - since)
    assert ("start", (store, "sw", elapsed.IDLE_SINCE, "eng@1-1", NOW, 1)) in calls
    assert _puts(calls) == [replace(agent, idle_ticks=ticks)]
    logged = next(call for call in calls if call[0] == "gate_log")
    who = tick.Who(name="eng@1-1", task="t1")
    assert logged[1] == (
        "sw",
        ("row", (tick.IDLE_TICKS, "count", who), {"reason": f"idle tick {ticks}", "now_ms": NOW}),
    )
    assert ("recovery_holds", (store, "sw", agent, NOW)) in calls
    assert "retire" not in calls.names()


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(
    ("ticks", "retired", "pending", "said"),
    [
        (3, True, False, ["nudged", "stalled eng@1-1, task reopened"]),
        (3, True, True, ["nudged", "stalled eng@1-1, task reopened; outage recovery could not proceed: no account"]),
        (3, False, False, ["nudged"]),
        (2, True, False, ["nudged"]),
    ],
)
def test_an_agent_idle_past_the_kill_count_is_retired_and_its_task_dropped(
    monkeypatch: pytest.MonkeyPatch, ticks: int, retired: bool, pending: bool, said: list[str]
) -> None:
    since = NOW - (ticks - 1) * elapsed.PASS_MS
    calls, runtime = _watch(
        monkeypatch, PaneObservation("idle"), since=since, retired=retired, pending=pending, nudged=["nudged"]
    )
    store, agent = _store(calls), _agent()
    assert agents._watch_idle("sw", store, "ledger", runtime, ROWS, agent, NOW) == said
    idle = replace(agent, idle_ticks=elapsed.passes(NOW - since))
    assert (("retire", (idle,), {"homes": ("sw", "t1", "homes")}) in calls) is (ticks >= 3)
    assert (("drop", ("sw", store, "ledger", ROWS, idle), {"refund": True}) in calls) is (ticks >= 3 and retired)
    assert (("blocker", (store, "sw", agent)) in calls) is (ticks >= 3 and retired and pending)


OPEN_ROW: dict[str, Any] = {
    "state": "open",
    "claimed_by": "old@1-2",
    "claim_generation": 4,
    "awaiting": "checks",
    "pr_url": "https://example.invalid/pull/1",
}


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(
    ("ended", "current", "claimed"),
    [(True, True, True), (False, False, True), (False, True, False), (False, True, True)],
)
def test_an_owner_is_restored_only_for_a_live_current_agent_that_holds_its_claim(
    monkeypatch: pytest.MonkeyPatch, ended: bool, current: bool, claimed: bool
) -> None:
    calls = Calls()
    monkeypatch.setattr(tick, "_ended", calls.fn("ended", ended))
    monkeypatch.setattr(tick, "_current_owner", calls.fn("current", current))
    store = SimpleNamespace(ensure_claim=calls.fn("ensure_claim", claimed))
    ledger = SimpleNamespace(update_task=calls.fn("update", {"state": "pr"}))
    row = dict(OPEN_ROW)
    rows: dict[str, Any] = {"t1": row}
    agent = _agent()
    agents._restore_owner("sw", store, ledger, rows, agent)
    expected: list[tuple[Any, ...]] = [("ended", (agent, rows))]
    if not ended:
        expected.append(("current", (agent, row)))
    if not ended and current:
        expected.append(("ensure_claim", ("sw", agent, tick.LEASE_MS)))
    restored = not ended and current and claimed
    if restored:
        match = {"claimed_by": "old@1-2", "claim_generation": 4, "awaiting": "checks"}
        fields = {"claimed_by": "eng@1-1", "state": "pr"}
        expected.append(("update", ("sw", "t1", fields), {"if_state": ("open",), "if_match": match}))
    assert calls == expected
    assert rows["t1"] == ({"state": "pr"} if restored else OPEN_ROW)


@pytest.mark.feature("swarm-tick-agents-reap")
def test_a_refused_retire_is_recorded_with_its_refusal_and_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    monkeypatch.setattr(tick, "master_retire", SimpleNamespace(forget=calls.fn("forget")))
    reports = SimpleNamespace(removed=calls.fn("removed", False))
    monkeypatch.setattr(tick, "reports", SimpleNamespace(Reports=lambda redis: reports))
    monkeypatch.setattr(tick, "reaper", SimpleNamespace(scratch_homes=lambda slug, task: (slug, task, "homes")))
    monkeypatch.setattr(
        tick, "retire_watch", SimpleNamespace(failed=calls.fn("failed"), reported_gone=calls.fn("reported_gone", False))
    )
    runtime = SimpleNamespace(retire=calls.fn("retire", False), refusal=calls.fn("refusal", "pane busy"))
    store, agent = _store(calls), _agent()
    actions = agents._retire_ended("sw", store, "ledger", runtime, {}, agent, NOW)
    assert actions == ["could not retire eng@1-1, retrying next tick"]
    assert calls[-2:] == [("refusal", (agent,)), ("failed", (store, "sw", "eng@1-1", "pane busy", NOW))]


def _reaping(monkeypatch: pytest.MonkeyPatch, calls: Calls, live: set[str], **answers: Any) -> Any:
    """Patch the seams one reap judgement reads and return the pass it judges against."""
    monkeypatch.setattr(tick, "_acknowledging", calls.fn("acknowledging", answers.get("acknowledging", False)))
    monkeypatch.setattr(tick, "_remote_gone_reason", calls.fn("gone", ""))
    monkeypatch.setattr(tick, "_ended", calls.fn("ended", False))
    monkeypatch.setattr(tick, "_elsewhere", calls.fn("elsewhere", False))
    monkeypatch.setattr(tick, "reports", SimpleNamespace(host=calls.fn("host", "")))
    monkeypatch.setattr(tick, "_restore_owner", calls.fn("restore_owner"))
    monkeypatch.setattr(tick, "_watch_agent", calls.fn("watch_agent", ["watched"]))
    monkeypatch.setattr(tick, "resume", SimpleNamespace(recovery_pending=calls.fn("pending", answers.get("pending"))))
    store = SimpleNamespace(refresh=calls.fn("refresh"))
    return agents._Reaping("sw", store, "ledger", "runtime", {}, live, NOW)  # pyright: ignore[reportArgumentType]


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(
    ("state", "lane", "skipped"),
    [
        ("awaiting-decision", "eng", True),
        ("starting", MASTER, True),
        ("starting", "eng", False),
        ("working", MASTER, False),
    ],
)
def test_an_agent_awaiting_a_decision_or_a_starting_master_is_not_judged(
    monkeypatch: pytest.MonkeyPatch, state: str, lane: str, skipped: bool
) -> None:
    calls = Calls()
    judging = _reaping(monkeypatch, calls, set(), acknowledging=True)
    agent = _agent(state=state, lane=lane)
    assert agents._reap_agent(judging, agent) == []
    assert calls == ([] if skipped else [("acknowledging", ("sw", judging.store, agent))])


@pytest.mark.feature("swarm-tick-agents-reap")
def test_a_live_local_agent_has_its_owner_restored_then_is_watched(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    judging = _reaping(monkeypatch, calls, {"eng@1-1"})
    agent = _agent()
    assert agents._reap_agent(judging, agent) == ["watched"]
    restore = ("restore_owner", ("sw", judging.store, "ledger", judging.rows, agent))
    watch = ("watch_agent", ("sw", judging.store, "ledger", "runtime", judging.rows, agent, NOW))
    assert calls[-3:] == [restore, ("ended", (agent, judging.rows)), watch]


@pytest.mark.feature("swarm-tick-agents-reap")
def test_a_missing_agent_with_a_pending_recovery_keeps_its_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    judging = _reaping(monkeypatch, calls, set(), pending=True)
    agent = _agent()
    assert agents._reap_agent(judging, agent) == []
    assert calls[-2:] == [
        ("pending", (judging.store, "sw", agent)),
        ("refresh", ("sw", "t1", "eng@1-1", tick.LEASE_MS)),
    ]


@pytest.mark.feature("swarm-tick-agents-reap")
def test_a_suspect_remote_agent_that_answers_again_is_watched_as_working(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    monkeypatch.setattr(tick, "_watch_agent", calls.fn("watch_agent", ["watched"]))
    store = SimpleNamespace(refresh=calls.fn("refresh"), put_agent=calls.fn("put_agent"))
    runtime = SimpleNamespace(observe=lambda agent: PaneObservation("idle"))
    agent = _agent(state=tick.SUSPECT)
    working = replace(agent, state="working")
    assert agents._watch_remote("sw", store, "ledger", runtime, {}, agent, NOW) == ["watched"]
    assert calls == [
        ("refresh", ("sw", "t1", "eng@1-1", tick.LEASE_MS)),
        ("put_agent", ("sw", working)),
        ("watch_agent", ("sw", store, "ledger", runtime, {}, working, NOW)),
    ]


def _freeing(monkeypatch: pytest.MonkeyPatch, calls: Calls, retired: bool) -> tuple[Any, Any]:
    """Patch the seams freeing an agent reads and return its store and runtime."""
    monkeypatch.setattr(tick, "reaper", SimpleNamespace(scratch_homes=lambda slug, task: (slug, task, "homes")))
    monkeypatch.setattr(tick, "_drop", calls.fn("drop", ", task reopened"))
    monkeypatch.setattr(tick, "content_refusal", SimpleNamespace(handoff=calls.fn("handoff")))
    monkeypatch.setattr(tick, "resume", SimpleNamespace(free_seat=calls.fn("free_seat")))
    monkeypatch.setattr(tick, "outage", SimpleNamespace(clear_pane=calls.fn("clear_pane")))
    return _store(calls), SimpleNamespace(retire=calls.fn("retire", retired))


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize("retired", [True, False])
def test_an_agent_refusing_content_is_handed_off_and_its_task_dropped(
    monkeypatch: pytest.MonkeyPatch, retired: bool
) -> None:
    calls = Calls()
    store, runtime = _freeing(monkeypatch, calls, retired)
    agent, rows = _agent(), dict(ROWS)
    actions = agents._free_refused("sw", store, "ledger", runtime, rows, agent, NOW)
    expected: list[tuple[Any, ...]] = [("retire", (agent,), {"homes": ("sw", "t1", "homes")})]
    if retired:
        assert actions == ["freed eng@1-1 after repeated content refusals, task reopened"]
        expected += [
            ("handoff", (store, "sw", agent, NOW)),
            ("drop", ("sw", store, "ledger", rows, agent), {"refund": True}),
        ]
    else:
        assert actions == ["could not retire eng@1-1 after repeated content refusals, retrying next tick"]
    assert calls == expected


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize("retired", [True, False])
def test_an_agent_stuck_on_its_provider_frees_its_seat_and_clears_its_pane(
    monkeypatch: pytest.MonkeyPatch, retired: bool
) -> None:
    calls = Calls()
    store, runtime = _freeing(monkeypatch, calls, retired)
    agent, rows = _agent(), dict(ROWS)
    actions = agents._free_stuck("sw", store, "ledger", runtime, rows, agent, NOW)
    expected: list[tuple[Any, ...]] = [("retire", (agent,), {"homes": ("sw", "t1", "homes")})]
    if retired:
        assert actions == ["freed the seat of eng@1-1, stuck on a provider error, task reopened"]
        expected += [
            ("free_seat", (store, "sw", agent, NOW)),
            ("clear_pane", (store, "sw", "eng@1-1")),
            ("drop", ("sw", store, "ledger", rows, agent), {"refund": True}),
        ]
    else:
        assert actions == ["could not retire eng@1-1, stuck on a provider error, retrying next tick"]
        expected.append(("put_agent", ("sw", agent)))
    assert calls == expected


@pytest.mark.feature("swarm-tick-agents-reap")
@pytest.mark.parametrize(
    ("free", "said"),
    [
        (agents._free_refused, " after repeated content refusals"),
        (agents._free_revoked, " on revoked claude account a1"),
        (agents._free_stuck, ", stuck on a provider error"),
    ],
)
def test_a_freeing_retire_asks_the_hive_report_with_its_store_agent_homes_and_time(
    monkeypatch: pytest.MonkeyPatch, free: Callable[..., list[str]], said: str
) -> None:
    calls = Calls()
    monkeypatch.setattr(tick, "reaper", SimpleNamespace(scratch_homes=lambda slug, task: (slug, task, "homes")))
    monkeypatch.setattr(tick, "retire_watch", SimpleNamespace(retire_unless_gone=calls.fn("retire_unless_gone", False)))
    store, agent, runtime = _store(calls), _agent(harness="claude", account="a1"), SimpleNamespace()
    actions = free("sw", store, "ledger", runtime, {}, agent, NOW)
    assert actions == [f"could not retire eng@1-1{said}, retrying next tick"]
    assert calls[0] == ("retire_unless_gone", (store, runtime, agent, ("sw", "t1", "homes"), NOW))
