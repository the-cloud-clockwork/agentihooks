import json

import pytest

from scripts.gates import log as gate_log
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import dispatcher, grouping, metrics, priority_sweep
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.store import MASTER, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "sw"
NOW = 1_800_000_000_000


def task(task_id, phase="p1", state="open", lane="eng", **fields):
    return {
        "id": task_id,
        "title": f"title {task_id}",
        "phase": phase,
        "state": state,
        "lane": lane,
        "depends_on": [],
        **fields,
    }


def doc(tasks, phases=None, **extra):
    phases = phases or [{"id": "p1", "depends_on": []}, {"id": "p2", "depends_on": ["p1"]}]
    return {"overview": "o", "tasks": list(tasks), "phases": phases, **extra}


class Ledger:
    def __init__(self, stored=None, refused=()):
        self.ranks, self.comments = [], []
        self.stored, self.refused = stored or {}, set(refused)

    def rank_task(self, slug, task_id, rank, by, if_unranked=False):
        assert slug == SLUG and if_unranked is True
        if task_id in self.refused:
            raise LedgerRefused(f"ledger {slug} refused")
        self.ranks.append((task_id, rank, by))
        return {"id": task_id, "rank": self.stored.get(task_id, rank)}

    def comment(self, slug, task_id, text, by):
        assert slug == SLUG
        if f"comment:{task_id}" in self.refused:
            raise LedgerRefused(f"ledger {slug} refused")
        self.comments.append((task_id, text, by))


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=0, max_ci=0))
    return saved


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def shipped(monkeypatch):
    rows = []
    monkeypatch.setattr(metrics, "record", lambda table, found, now_ms: rows.append((table, found, now_ms)) or [])
    return rows


def chain():
    return [
        task("a"),
        task("b", depends_on=["a"]),
        task("c", phase="p2"),
        task("d", phase="p2", state="done", depends_on=["a"]),
        task("e", phase="p2", depends_on=["c"]),
    ]


def test_leverage_counts_open_waiters_through_dependencies_and_phase_barriers():
    assert dispatcher.leverage(doc(chain())) == {"a": 3, "b": 2, "c": 1, "e": 0}


def test_leverage_ends_on_a_dependency_loop():
    looped = [task("a", depends_on=["b"]), task("b", depends_on=["a"])]
    assert dispatcher.leverage(doc(looped)) == {"a": 1, "b": 1}


def test_leverage_order_breaks_ties_by_the_bottleneck_lane_then_the_focus():
    tasks = [
        task("x", phase=""),
        task("y", phase="", lane="ci"),
        task("z", phase=""),
        task("w", phase="", depends_on=["x", "y", "z"]),
        task("v", phase="", depends_on=["x", "y", "z"]),
    ]
    focus = [{"id": "f1", "verb": "focus", "target": "tasks/z"}, {"id": "f2", "verb": "freeze", "target": "tasks/x"}]
    found = doc(tasks, phases=[], freezes=focus)
    assert dispatcher.ordered(found, "ci") == ["y", "z", "x"]
    assert dispatcher.ordered(found, "engineering") == ["z", "x", "y"]
    assert dispatcher.ordered(found, "review") == ["z", "x", "y"]
    assert dispatcher.ordered(doc(tasks, phases=[]), "") == ["x", "y", "z"]


def test_ordered_uses_the_scores_it_is_given():
    found = doc([task("x", phase=""), task("y", phase=""), task("w", phase="", depends_on=["x"])], phases=[])
    assert dispatcher.ordered(found, "") == ["x"]
    assert dispatcher.ordered(found, "", {"x": 1, "y": 2, "w": 0}) == ["y", "x"]


@pytest.mark.parametrize(
    ("target", "first"),
    [("phases/p1", "y"), ("plans/pl", "y"), ("lane:ci", "y"), ("kind:ops", "y"), ("tasks/x", "x"), ("lane:plan", "x")],
)
def test_focus_on_an_ancestor_or_a_selector_covers_its_tasks(target, first):
    tasks = [task("x", phase=""), task("y", lane="ci", kind="ops"), task("w", phase="", depends_on=["x", "y"])]
    phases = [{"id": "p1", "depends_on": [], "plan": "plans/pl"}]
    found = doc(tasks, phases=phases, plans=[{"id": "pl"}], freezes=[{"verb": "focus", "target": target}])
    assert dispatcher.ordered(found, "")[0] == first


def test_below_delegate_a_high_leverage_task_yields_one_master_proposal_and_no_rank(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    ledger, found = Ledger(), doc(chain())
    first = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW)
    again = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW + 60_000)
    items = InboxStore(store.redis).pending_items(seat_address(SLUG, MASTER))
    assert ledger.ranks == [] and again == []
    assert [i.text for i in items] == [
        dispatcher.PROPOSE.format(slug=SLUG, task="a", title="title a", work="3 open tasks"),
        dispatcher.PROPOSE.format(slug=SLUG, task="b", title="title b", work="2 open tasks"),
        dispatcher.PROPOSE.format(slug=SLUG, task="c", title="title c", work="1 open task"),
    ]
    assert first == [
        "proposed rank high for task a to the master: it unblocks 3 open tasks",
        "proposed rank high for task b to the master: it unblocks 2 open tasks",
        "proposed rank high for task c to the master: it unblocks 1 open task",
    ]
    assert ledger.comments[0] == ("a", dispatcher.PROPOSED.format(work="3 open tasks"), dispatcher.AUTHOR)
    rows = gate_log.recent(SLUG, None, home)
    assert [(r["gate"], r["kind"], r["agent"], r["task"], r["tool"]) for r in rows] == [
        ("dispatcher", "propose", "dispatcher", t, "leverage-rank") for t in "abc"
    ]
    assert [r["rule"] for _, found_rows, _ in shipped for r in found_rows] == ["leverage-rank"] * 3


def test_at_delegate_the_top_leverage_tasks_are_ranked_high_with_a_log_row(store, home, shipped):
    store.update(SLUG, autonomy="delegate")
    ledger = Ledger()
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(chain()), NOW)
    assert ledger.ranks == [("a", "high", "dispatcher"), ("b", "high", "dispatcher"), ("c", "high", "dispatcher")]
    assert actions[0] == "ranked task a high: it unblocks 3 open tasks"
    assert ledger.comments[0] == ("a", dispatcher.RAISED.format(work="3 open tasks"), "dispatcher")
    assert InboxStore(store.redis).pending_items(seat_address(SLUG, MASTER)) == []
    (row, *_) = gate_log.recent(SLUG, None, home)
    assert (row["gate"], row["kind"], row["task"], row["tool"], row["at"]) == (
        "dispatcher",
        "apply",
        "a",
        "leverage-rank",
        NOW,
    )
    table, rows, at = shipped[0]
    assert (table, at) == (dispatcher.TABLE, NOW)
    assert {k: rows[0][k] for k in ("ledger", "ts_ms", "phase", "task", "rule", "mode", "action")} == {
        "ledger": SLUG,
        "ts_ms": NOW,
        "phase": "p1",
        "task": "a",
        "rule": "leverage-rank",
        "mode": "apply",
        "action": "ranked task a high: it unblocks 3 open tasks",
    }
    assert len({r["event_id"] for r in rows}) == 3


def test_an_operator_ranked_task_is_never_changed(store, home, shipped):
    store.update(SLUG, autonomy="full")
    ledger, tasks = Ledger(), chain()
    tasks[0]["rank"] = "low"
    tasks[1]["rank"] = "normal"
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(tasks), NOW)
    assert ledger.ranks == [("c", "high", "dispatcher")]


def test_a_rank_set_after_the_snapshot_wins_and_logs_nothing(store, home, shipped):
    store.update(SLUG, autonomy="delegate")
    ledger = Ledger(stored={"a": "low"})
    tasks = chain()
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(tasks), NOW)
    assert actions == ["ranked task b high: it unblocks 2 open tasks", "ranked task c high: it unblocks 1 open task"]
    assert [c[0] for c in ledger.comments] == ["b", "c"]
    assert [r["task"] for r in gate_log.recent(SLUG, None, home)] == ["b", "c"]
    assert ("rank" not in tasks[0], tasks[1]["rank"], tasks[2]["rank"]) == (True, "high", "high")


def test_a_refused_rank_write_skips_that_task_and_a_refused_comment_still_logs(store, home, shipped):
    store.update(SLUG, autonomy="delegate")
    ledger = Ledger(refused={"a", "comment:b"})
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(chain()), NOW)
    assert actions == ["ranked task b high: it unblocks 2 open tasks", "ranked task c high: it unblocks 1 open task"]
    assert ledger.comments == [("c", dispatcher.RAISED.format(work="1 open task"), "dispatcher")]


def test_a_refused_proposal_comment_still_logs_the_proposal(store, home, shipped):
    store.update(SLUG, autonomy="manual")
    ledger = Ledger(refused={"comment:a"})
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(chain()), NOW)
    assert actions[0] == "proposed rank high for task a to the master: it unblocks 3 open tasks"
    assert [c[0] for c in ledger.comments] == ["b", "c"]


def test_only_open_tasks_that_unblock_work_are_raised_and_at_most_the_top_count(store, home, shipped):
    store.update(SLUG, autonomy="delegate")
    tasks = [task(f"t{i}", phase="", depends_on=[]) for i in range(5)]
    tasks += [task("w", phase="", depends_on=[f"t{i}" for i in range(5)])]
    tasks[0]["state"] = "claimed"
    tasks[1]["out_of_scope"] = True
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(tasks, phases=[]), NOW)
    assert [r[0] for r in ledger.ranks] == ["t2", "t3", "t4"][: dispatcher.TOP]


def test_nothing_to_rank_writes_nothing(store, home, shipped):
    store.update(SLUG, autonomy="delegate")
    ledger = Ledger()
    assert dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc([task("a")]), NOW) == []
    assert (ledger.ranks, ledger.comments, shipped, gate_log.recent(SLUG, None, home)) == ([], [], [], [])


def test_the_named_bottleneck_orders_the_raise(store, home, shipped):
    store.update(SLUG, autonomy="delegate")
    store.redis.set(store.key(SLUG, "bottleneck"), json.dumps({"bottleneck": "ci"}))
    tasks = [task(f"e{i}", phase="") for i in range(3)] + [task("c0", phase="", lane="ci")]
    tasks.append(task("w", phase="", depends_on=[t["id"] for t in tasks]))
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(tasks, phases=[]), NOW)
    assert [r[0] for r in ledger.ranks] == ["c0", "e0", "e1"]


def test_grouping_runs_unchanged_and_logs_its_actions(store, home, shipped, monkeypatch):
    calls = []
    monkeypatch.setattr(grouping, "group_pass", lambda *a: calls.append(a) or ["grouped tasks b under a"])
    config = store.config(SLUG)
    found = doc([task("a")])
    assert dispatcher.group(SLUG, config, store, "L", found, NOW) == ["grouped tasks b under a"]
    assert calls == [(SLUG, config, store, "L", found)]
    (row,) = gate_log.recent(SLUG, None, home)
    assert (row["gate"], row["tool"], row["reason"], row["task"]) == (
        "dispatcher",
        "grouping",
        "grouped tasks b under a",
        "",
    )
    assert [(r["rule"], r["action"]) for r in shipped[0][1]] == [("grouping", "grouped tasks b under a")]


def test_a_refused_grouping_is_returned_but_not_logged_as_an_action(store, home, shipped, monkeypatch):
    skipped = "skipped grouping under task a: the ledger refused its write"
    monkeypatch.setattr(grouping, "group_pass", lambda *a: [skipped, "grouped tasks d under c"])
    assert dispatcher.group(SLUG, store.config(SLUG), store, "L", doc([]), NOW) == [skipped, "grouped tasks d under c"]
    assert [r["reason"] for r in gate_log.recent(SLUG, None, home)] == ["grouped tasks d under c"]
    assert [r["action"] for r in shipped[0][1]] == ["grouped tasks d under c"]


def test_the_priority_sweep_runs_unchanged_and_logs_its_actions(store, home, shipped, monkeypatch):
    calls = []
    monkeypatch.setattr(priority_sweep, "priority_pass", lambda *a: calls.append(a) or ["cleared one"])
    found = doc([])
    assert dispatcher.priorities(store, SLUG, found, "L", "V", NOW) == ["cleared one"]
    assert calls == [(store, SLUG, found, "L", None, "V")]
    assert [(r["rule"], r["mode"]) for r in shipped[0][1]] == [("priority-sweep", "apply")]


def test_a_failed_metrics_write_is_reported_as_an_action(store, home, monkeypatch):
    monkeypatch.setattr(grouping, "group_pass", lambda *a: ["grouped"])
    monkeypatch.setattr(metrics, "record", lambda table, rows, now_ms: ["metrics outbox failed: boom"])
    assert dispatcher.group(SLUG, store.config(SLUG), store, "L", doc([]), NOW) == [
        "grouped",
        "metrics outbox failed: boom",
    ]


def test_the_tick_runs_the_dispatcher_steps(monkeypatch):
    import inspect

    from scripts.swarm import cli, tick

    source = inspect.getsource(tick.tick)
    assert "grouping.group_pass" not in source and "dispatcher.group" in source
    placing = source[source.index("with PLACING:") :]
    assert placing.index("dispatcher.rank_pass") < placing.index("_spawn,")
    run = inspect.getsource(cli.run_tick)
    assert "priority_sweep.priority_pass" not in run and "dispatcher.priorities" in run


def test_the_ledger_client_ranks_a_task_as_its_author(monkeypatch):
    from scripts.swarm.ledger_client import LedgerClient

    sent = []
    state = {"tasks": [{"id": "z"}, {"id": "a", "rank": "high"}]}
    monkeypatch.setattr(LedgerClient, "_call", lambda self, slug, ops=None: sent.append((slug, ops)) or state)
    assert LedgerClient().rank_task(SLUG, "a", "high", "dispatcher", if_unranked=True) == state["tasks"][1]
    assert LedgerClient().rank_task(SLUG, "q", "low", "master@abcdef-0001") == {}
    ((_, (guarded,)), (slug, (plain,))) = sent
    assert slug == SLUG
    assert {k: guarded[k] for k in ("op", "by", "item", "rank", "if_unranked")} == {
        "op": "task_rank",
        "by": "dispatcher",
        "item": "tasks/a",
        "rank": "high",
        "if_unranked": True,
    }
    assert "if_unranked" not in plain and (plain["item"], plain["rank"]) == ("tasks/q", "low")
