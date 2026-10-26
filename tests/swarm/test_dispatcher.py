import json

import pytest

from scripts.gates import log as gate_log
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import capacity, dispatcher, grouping, metrics, priority_sweep
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.store import MASTER, RedisStore, SwarmConfig, SwarmError

SLUG = "sw"
NOW = 1_800_000_000_000
MASTER_NAME = "master@abcdef-0001"


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
    def __init__(self, stored=None, refused=(), broken=()):
        self.ranks, self.comments = [], []
        self.stored, self.refused, self.broken = stored or {}, set(refused), set(broken)

    def rank_task(self, slug, task_id, rank, by, if_owned=False):
        assert slug == SLUG and if_owned is True
        if task_id in self.broken:
            raise SwarmError(f"ledger {slug}: server not answering")
        if task_id in self.refused or task_id in self.stored:
            raise LedgerRefused(f"ledger {slug} refused")
        self.ranks.append((task_id, rank, by))

    def comment(self, slug, task_id, text, by):
        assert slug == SLUG
        if f"comment:{task_id}" in self.broken:
            raise SwarmError(f"ledger {slug}: server not answering")
        if f"crash:{task_id}" in self.broken:
            raise RuntimeError("boom")
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


def ladder():
    return [
        task("a"),
        task("b"),
        task("c"),
        task("x", depends_on=["a", "b"]),
        task("y", depends_on=["a"]),
        task("z", depends_on=["a", "b", "c"]),
    ]


def merging():
    return [
        task("a"),
        task("b"),
        task("c"),
        task("p", depends_on=["a"]),
        task("q", depends_on=["a"]),
        *(task(f"p{i}", depends_on=["p"]) for i in range(3)),
        *(task(f"q{i}", depends_on=["q"]) for i in range(2)),
        *(task(f"b{i}", depends_on=["b"]) for i in range(2)),
        task("c0", depends_on=["c"]),
    ]


def stamped(tasks, owners, rank="high"):
    for found in tasks:
        if found["id"] in owners:
            found["rank"] = rank
    stamps = {f"tasks/{task_id}/rank": {"by": by} for task_id, by in owners.items() if by}
    return doc(tasks, phases=[{"id": "p1", "depends_on": []}], _meta={"stamps": stamps})


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
    assert dispatcher.ordered(found, "ci", dispatcher.leverage(found)) == ["y", "z", "x", "w", "v"]
    assert dispatcher.ordered(found, "engineering", dispatcher.leverage(found)) == ["z", "x", "y", "w", "v"]
    assert dispatcher.ordered(found, "review", dispatcher.leverage(found)) == ["z", "x", "y", "w", "v"]
    plain = doc(tasks, phases=[])
    assert dispatcher.ordered(plain, "", dispatcher.leverage(plain)) == ["x", "y", "z", "w", "v"]


def test_ordered_uses_the_scores_it_is_given_below_the_chain():
    tasks = [task(t, phase="") for t in "xy"] + [task("w", phase="", depends_on=["x"]), task("v", phase="")]
    tasks.append(task("u", phase="", depends_on=["y"]))
    found = doc(tasks, phases=[])
    assert dispatcher.ordered(found, "", dispatcher.leverage(found)) == ["x", "y", "w", "v", "u"]
    assert dispatcher.ordered(found, "", {"x": 1, "y": 2}) == ["y", "x", "w", "v", "u"]
    assert dispatcher.ordered(found, "", {"x": 1, "y": 2, "v": 9}) == ["y", "x", "v", "w", "u"]
    assert dispatcher.ordered(found, "", {"x": 1, "y": 2, "u": 1}) == ["y", "x", "u", "w", "v"]


def test_chains_count_the_longest_open_path_of_waiters_behind_each_task():
    diamond = [task("a"), task("b", depends_on=["a"]), task("c", depends_on=["a"]), task("d", depends_on=["b", "c"])]
    assert dispatcher.chains(doc(diamond)) == {"a": 2, "b": 1, "c": 1, "d": 0}
    assert dispatcher.leverage(doc(diamond)) == {"a": 3, "b": 1, "c": 1, "d": 0}
    assert dispatcher.chains(doc(chain())) == {"a": 3, "b": 2, "c": 1, "e": 0}
    looped = [task("a", depends_on=["b"]), task("b", depends_on=["a"])]
    assert dispatcher.chains(doc(looped)) == {"a": 1, "b": 1}
    tangled = [task("a"), task("b", depends_on=["a", "c"]), task("c", depends_on=["a", "b"]), task("d")]
    tangled.append(task("e", depends_on=["c", "d"]))
    assert dispatcher.chains(doc(tangled)) == {"a": 3, "b": 2, "c": 2, "d": 1, "e": 0}
    assert dispatcher.ordered(doc(tangled), "", dispatcher.leverage(doc(tangled))) == ["a", "b", "c", "d", "e"]
    knot = {"t0": ["t3"], "t1": ["t0", "t2", "t3", "t4"], "t2": ["t0", "t1", "t3", "t4"], "t3": ["t0", "t2"]}
    knot["t4"] = ["t1", "t2", "t3"]
    knotted = doc([task(t, depends_on=deps) for t, deps in knot.items()] + [task("t5", depends_on=["t4"])])
    assert dispatcher.chains(knotted) == {**dict.fromkeys(knot, 5), "t5": 0}
    long = [task("c0", phase="")] + [task(f"c{i}", phase="", depends_on=[f"c{i - 1}"]) for i in range(1, 1500)]
    assert dispatcher.chains(doc(long, phases=[]))["c0"] == 1499


def deep_and_wide():
    return [
        task("l1", phase=""),
        task("s", phase=""),
        *(task(f"s{i}", phase="", depends_on=["s"]) for i in range(6)),
        task("h1", phase=""),
        *(task(f"h{i}", phase="", depends_on=[f"h{i - 1}"]) for i in range(2, 6)),
        task("l2", phase=""),
    ]


def test_a_five_deep_chain_head_is_ordered_before_a_wide_shallow_task():
    found = doc(deep_and_wide(), phases=[])
    assert dispatcher.leverage(found)["s"] > dispatcher.leverage(found)["h1"]
    assert dispatcher.ordered(found, "", dispatcher.leverage(found)) == [
        "h1",
        "h2",
        "h3",
        "s",
        "h4",
        "l1",
        *(f"s{i}" for i in range(6)),
        "h5",
        "l2",
    ]


def test_claimable_leaves_are_ordered_by_the_bottleneck_lane_then_the_focus_order_then_the_ledger():
    tasks = [task("l1", phase=""), task("l2", phase="", lane="ci"), task("l3", phase=""), task("l4", phase="")]
    focus = [{"verb": "focus", "target": "tasks/l4"}, {"verb": "focus", "target": "tasks/l3"}]
    found = doc(tasks, phases=[], freezes=focus)
    assert dispatcher.ordered(found, "ci", dispatcher.leverage(found)) == ["l2", "l4", "l3", "l1"]
    assert dispatcher.ordered(found, "", dispatcher.leverage(found)) == ["l4", "l3", "l1", "l2"]
    covered = doc([task("p", phase=""), task("q", phase="")], phases=[])
    covered["freezes"] = [{"verb": "focus", "target": "tasks/q"}, {"verb": "focus", "target": "lane:eng"}]
    assert dispatcher.ordered(covered, "", dispatcher.leverage(covered)) == ["q", "p"]


def proposed(actions):
    return [a.split()[5] for a in actions if a.startswith("proposed rank high for task ")]


def entered(task_id, why, lane="eng"):
    return (task_id, dispatcher.ENTERED.format(lane=lane, why=why), dispatcher.AUTHOR)


def left(task_id, why, lane="eng"):
    return (task_id, dispatcher.LEFT.format(lane=lane, why=why), dispatcher.AUTHOR)


def leads(chain, work):
    return dispatcher.LEADS.format(chain=chain, work=work)


OUTRANKED_BY_TWO = (
    "it was outranked by 2 claimable tasks in focus with longer chains, more unblocked work or an operator rank"
)


def test_the_order_and_the_queue_head_hold_across_ticks_with_no_input_change(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=2)
    found = doc(deep_and_wide(), phases=[])
    first = dispatcher.ordered(found, "", dispatcher.leverage(found))
    assert all(dispatcher.ordered(found, "", dispatcher.leverage(found)) == first for _ in range(3))
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW)
    assert ledger.comments == [
        entered("h1", leads("4 open tasks", "4 open tasks")),
        entered("s", leads("1 open task", "6 open tasks")),
    ]
    for tick in range(1, 4):
        again = Ledger()
        assert dispatcher.rank_pass(SLUG, store.config(SLUG), store, again, found, NOW + tick * 60_000) == []
        assert (again.ranks, again.comments) == ([], [])
    assert dispatcher.ordered(found, "", dispatcher.leverage(found)) == first


@pytest.mark.parametrize(
    "target", ["phases/p1", "plans/pl", "lane:ci", "kind:ops", "tasks/y", "tasks/x", "lane:eng", "kind:code"]
)
def test_focus_on_an_ancestor_or_a_selector_lifts_its_task_above_ledger_order(target):
    x, y = task("x", phase=""), task("y", lane="ci", kind="ops")
    focused, other = (x, y) if target in ("tasks/x", "lane:eng", "kind:code") else (y, x)
    tasks = [other, focused, task("w", phase="", depends_on=["x", "y"])]
    phases = [{"id": "p1", "depends_on": [], "plan": "plans/pl"}]
    found = doc(tasks, phases=phases, plans=[{"id": "pl"}], freezes=[{"verb": "focus", "target": target}])
    assert dispatcher.ordered(found, "", dispatcher.leverage(found)) == [focused["id"], other["id"], "w"]


def test_focus_order_leads_with_focused_tasks_when_no_cap_applies():
    tasks = [task("a", phase="", kind="hot"), task("b", phase="")]
    found = doc(tasks, phases=[], freezes=[{"verb": "focus", "target": "kind:hot"}])
    order = dispatcher._focus_order(found)
    assert order["a"] < order["b"]


def test_focus_weight_caps_focused_claims_at_a_share_of_the_lane_cap():
    holding = [task(f"r{i}", phase="", state="claimed", kind="hot") for i in range(5)]
    holding += [task(f"n{i}", phase="", state="claimed", kind="code") for i in range(5)]
    waiting_focused = task("open-hot", phase="", kind="hot")
    waiting_plain = task("open-plain", phase="", kind="code")
    tasks = [*holding, waiting_focused, waiting_plain]
    found = doc(tasks, phases=[], freezes=[{"verb": "focus", "target": "kind:hot"}])
    order = dispatcher._focus_order(found, {"eng": 10}, 50)
    assert order["open-hot"] == order["open-plain"]


def test_focus_weight_below_the_cap_still_leads_with_the_focused_task():
    holding = [task(f"r{i}", phase="", state="claimed", kind="hot") for i in range(4)]
    holding += [task(f"n{i}", phase="", state="claimed", kind="code") for i in range(6)]
    waiting_focused = task("open-hot", phase="", kind="hot")
    waiting_plain = task("open-plain", phase="", kind="code")
    tasks = [*holding, waiting_focused, waiting_plain]
    found = doc(tasks, phases=[], freezes=[{"verb": "focus", "target": "kind:hot"}])
    order = dispatcher._focus_order(found, {"eng": 10}, 50)
    assert order["open-hot"] < order["open-plain"]


def test_below_delegate_a_high_leverage_task_yields_one_master_proposal_and_no_rank(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    ledger, found = Ledger(), doc(ladder())
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


def test_at_delegate_a_task_entering_the_queue_head_gets_one_comment_and_a_log_row(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=3)
    ledger = Ledger()
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(ladder()), NOW)
    assert ledger.ranks == []
    why = leads("1 open task", "3 open tasks")
    assert actions[0] == f"put task a at the head of the eng claim queue: {why}"
    assert ledger.comments[0][1] == (
        "The dispatcher put this task at the head of the eng claim queue: "
        "it leads a chain of 1 open task and unblocks 3 open tasks."
    )
    assert ledger.comments == [
        entered("a", why),
        entered("b", leads("1 open task", "2 open tasks")),
        entered("c", leads("1 open task", "1 open task")),
    ]
    assert InboxStore(store.redis).pending_items(seat_address(SLUG, MASTER)) == []
    (row, *_) = gate_log.recent(SLUG, None, home)
    assert (row["gate"], row["kind"], row["task"], row["tool"], row["at"]) == (
        "dispatcher",
        "apply",
        "a",
        "queue-head",
        NOW,
    )
    table, rows, at = shipped[0]
    assert (table, at) == (dispatcher.TABLE, NOW)
    assert {k: rows[0][k] for k in ("ledger", "ts_ms", "phase", "task", "rule", "mode", "action")} == {
        "ledger": SLUG,
        "ts_ms": NOW,
        "phase": "p1",
        "task": "a",
        "rule": "queue-head",
        "mode": "apply",
        "action": actions[0],
    }
    assert len({r["event_id"] for r in rows}) == 3


def test_the_rank_pass_reads_releases_from_the_swarm_repository(store, home, shipped, monkeypatch):
    from scripts.swarm import release_wait

    repos = []
    monkeypatch.setattr(release_wait, "checker", lambda repo: repos.append(repo) or (lambda task: True))
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), doc(ladder()), NOW)
    assert repos == [store.config(SLUG).repo]


def test_the_rank_pass_holds_tasks_the_applied_check_refuses(store, home, shipped, monkeypatch):
    from scripts.swarm import apply_wait

    seen = []

    def checker(config, rows):
        seen.append((config, sorted(rows)))
        return lambda task: False

    monkeypatch.setattr(apply_wait, "checker", checker)
    store.update(SLUG, autonomy="delegate", max_eng=3)
    ledger = Ledger()
    found = doc(ladder())
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW)
    assert seen == [(store.config(SLUG), sorted(t["id"] for t in found["tasks"]))]
    assert [a for a in actions if "head of" in a] == [] and ledger.comments == []


def test_the_queue_head_follows_claims_operator_ranks_and_freezes_with_one_comment_per_change(store, home, shipped):
    store.update(SLUG, autonomy="full", max_eng=2)
    tasks = [*deep_and_wide(), task("w1", phase="", depends_on=["l1"])]
    found = doc(tasks, phases=[])
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), found, NOW)
    tasks[2 + 6]["state"] = "claimed"
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW + 60_000)
    assert ledger.comments == [entered("l1", leads("1 open task", "1 open task"))]
    assert json.loads(store.redis.get(store.key(SLUG, "dispatch-head")))["eng"] == ["s", "l1"]
    tasks[-2]["rank"] = "high"
    found["_meta"] = {"stamps": {"tasks/l2/rank": {"by": "operator"}}}
    ledger = Ledger()
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW + 120_000)
    assert ledger.comments == [
        left("l1", OUTRANKED_BY_TWO),
        entered("l2", "the operator ranked it high"),
    ]
    assert actions[0] == f"moved task l1 out of the head of the eng claim queue: {OUTRANKED_BY_TWO}"
    assert (
        ledger.comments[0][1]
        == f"The dispatcher moved this task out of the head of the eng claim queue: {OUTRANKED_BY_TWO}."
    )
    found["freezes"] = [{"verb": "freeze", "target": "tasks/s"}]
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW + 180_000)
    unready = "it is no longer a claimable task in focus"
    assert ledger.comments == [left("s", unready), entered("l1", leads("1 open task", "1 open task"))]
    assert ledger.ranks == []


def test_the_queue_head_takes_the_seats_quota_leaves_each_lane(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=4, max_ci=1)
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps({"effective": {"eng": 1, "ci": 1, "plan": 0}}))
    tasks = [*ladder(), task("k", lane="ci"), task("j", lane="ci", depends_on=["k"])]
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(tasks), NOW)
    assert ledger.comments == [
        entered("a", leads("1 open task", "3 open tasks")),
        entered("k", leads("1 open task", "1 open task"), "ci"),
    ]
    assert json.loads(store.redis.get(store.key(SLUG, "dispatch-head"))) == {"eng": ["a"], "ci": ["k"], "plan": []}


def test_a_lane_with_no_seats_keeps_its_head_and_writes_nothing(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=2)
    found = doc(ladder())
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), found, NOW)
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps({"effective": {"eng": 0, "ci": 0, "plan": 0}}))
    ledger = Ledger()
    assert dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW + 60_000) == []
    assert ledger.comments == []
    assert json.loads(store.redis.get(store.key(SLUG, "dispatch-head")))["eng"] == ["a", "b"]


def test_a_task_gone_from_the_ledger_leaves_the_head_without_a_comment(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=1)
    store.redis.set(store.key(SLUG, "dispatch-head"), json.dumps({"eng": ["gone"]}))
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc([task("a"), task("w", depends_on=["a"])]), NOW)
    assert ledger.comments == [entered("a", leads("1 open task", "1 open task"))]


def test_a_step_that_raises_saves_only_the_head_changes_that_ran(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=2)
    ledger, found = Ledger(broken={"crash:b"}), doc(ladder())
    with pytest.raises(RuntimeError):
        dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW)
    assert json.loads(store.redis.get(store.key(SLUG, "dispatch-head")))["eng"] == ["a"]
    again = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, again, found, NOW + 60_000)
    assert again.comments == [entered("b", leads("1 open task", "2 open tasks"))]


def test_one_seat_names_the_single_task_that_outranked_the_one_leaving(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=1)
    tasks = [task("a"), task("b"), task("w", depends_on=["a"])]
    found = doc(tasks)
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), found, NOW)
    tasks[1]["rank"] = "urgent"
    found["_meta"] = {"stamps": {"tasks/b/rank": {"by": "operator"}}}
    ledger = Ledger()
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW + 60_000)
    outranked = (
        "it was outranked by 1 claimable task in focus with longer chains, more unblocked work or an operator rank"
    )
    assert ledger.comments == [left("a", outranked), entered("b", "the operator ranked it urgent")]
    assert actions[0] == f"moved task a out of the head of the eng claim queue: {outranked}"
    assert (
        ledger.comments[0][1] == f"The dispatcher moved this task out of the head of the eng claim queue: {outranked}."
    )


def test_a_head_task_whose_waiters_finish_leaves_the_head_naming_no_open_work(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=2)
    tasks = [task("a"), task("w", depends_on=["a"]), task("b"), task("v", depends_on=["b"])]
    found = doc(tasks)
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), found, NOW)
    tasks[1]["state"] = "done"
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW + 60_000)
    assert ledger.comments == [left("a", dispatcher.IDLE)]
    assert dispatcher.IDLE == "it unblocks no open work"
    assert json.loads(store.redis.get(store.key(SLUG, "dispatch-head")))["eng"] == ["b"]


def test_the_default_caps_are_the_lane_maximums_without_a_quota_decision():
    config = SwarmConfig(SLUG, "/repo", max_eng=5, max_ci=3, max_plan=2)
    assert capacity.caps({}, config) == {"eng": 5, "ci": 3, "plan": 2}
    assert capacity.caps({"effective": {"eng": 1}}, config) == {"eng": 1}


def test_a_refused_head_comment_still_logs_the_change(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=2)
    ledger = Ledger(refused={"comment:a"}, broken={"comment:b"})
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(ladder()), NOW)
    assert [a.endswith(dispatcher.UNCOMMENTED) for a in actions] == [True, True]
    assert [r["reason"] for r in gate_log.recent(SLUG, None, home)] == actions
    assert json.loads(store.redis.get(store.key(SLUG, "dispatch-head")))["eng"] == ["a", "b"]


def test_an_operator_ranked_task_is_never_proposed(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    ledger, tasks = Ledger(), ladder()
    found = stamped(tasks, dict.fromkeys("ab", "operator"))
    tasks[0]["rank"] = "low"
    tasks[1]["rank"] = "normal"
    assert proposed(dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW)) == ["c"]


def test_an_operator_low_rank_gives_up_its_place_in_the_window(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    tasks = [task(f"t{i}", phase="") for i in range(4)]
    tasks.append(task("w", phase="", depends_on=["t0", "t1", "t2", "t3"]))
    tasks.append(task("v", phase="", depends_on=["t0"]))
    tasks[0]["rank"], tasks[1]["rank"] = "low", "high"
    stamps = {f"tasks/t{i}/rank": {"by": "operator"} for i in range(2)}
    found = doc(tasks, phases=[], _meta={"stamps": stamps})
    assert proposed(dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), found, NOW)) == ["t2", "t3"]


def test_only_open_tasks_that_unblock_work_are_proposed_and_at_most_the_top_count(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    tasks = [task(f"t{i}", phase="", depends_on=[]) for i in range(6)]
    tasks += [task("w", phase="", depends_on=[f"t{i}" for i in range(5)])]
    tasks[0]["state"] = "claimed"
    tasks[1]["out_of_scope"] = True
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), doc(tasks, phases=[]), NOW)
    assert proposed(actions) == ["t2", "t3", "t4"][: dispatcher.TOP]


def test_nothing_to_rank_writes_nothing(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    ledger = Ledger()
    assert dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc([task("a")]), NOW) == []
    assert (ledger.ranks, ledger.comments, shipped, gate_log.recent(SLUG, None, home)) == ([], [], [], [])


def test_the_named_bottleneck_orders_the_proposals(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    store.redis.set(store.key(SLUG, "bottleneck"), json.dumps({"bottleneck": "ci"}))
    tasks = [task(f"e{i}", phase="") for i in range(3)] + [task("c0", phase="", lane="ci")]
    tasks.append(task("w", phase="", depends_on=[t["id"] for t in tasks]))
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), doc(tasks, phases=[]), NOW)
    assert proposed(actions) == ["c0", "e0", "e1"]


def test_a_blocked_task_is_never_proposed_however_much_it_unblocks(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    tasks = [task("a"), task("b", depends_on=["a"]), *(task(f"w{i}", depends_on=["b"]) for i in range(4))]
    assert proposed(dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), doc(tasks), NOW)) == ["a"]


def test_a_doctor_fix_phase_task_outside_the_focus_is_still_proposed(store, home, shipped):
    from scripts.doctor import loop, priming

    store.update(SLUG, autonomy="assist", template=priming.TEMPLATE)
    tasks = [task("f", phase=loop.FIX_PHASE), task("w", phase=loop.FIX_PHASE, depends_on=["f"])]
    found = doc(
        tasks, phases=[{"id": loop.FIX_PHASE, "depends_on": []}], freezes=[{"verb": "focus", "target": "lane:ci"}]
    )
    assert proposed(dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), found, NOW)) == ["f"]


def queue_ledger(store, autonomy, **fields):
    from tests.swarm.test_tick import FakeLedger

    config = store.update(SLUG, autonomy=autonomy, max_eng=4, **fields)
    tasks = [*deep_and_wide(), task("l3", phase="", rank="high"), task("l4", phase="", rank="high")]
    ledger = FakeLedger(tasks)
    found = ledger.state(SLUG)
    found["_meta"]["stamps"] = {"tasks/l3/rank": {"by": MASTER_NAME}, "tasks/l4/rank": {"by": "operator"}}
    return config, ledger, found


def spawned(store, config, ledger, found):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeRuntime

    return [
        (lane, t["id"]) for lane, t in tick._spawn_order(SLUG, config, store, [], ledger.rows, found, FakeRuntime())[0]
    ]


@pytest.mark.parametrize("autonomy", ["delegate", "full"])
def test_a_tick_with_four_free_seats_claims_the_top_four_of_the_dispatcher_queue(store, autonomy):
    config, ledger, found = queue_ledger(store, autonomy)
    assert dispatcher.queue(found, "")[:6] == ["l4", "h1", "h2", "h3", "s", "h4"]
    assert spawned(store, config, ledger, found) == [("eng", t) for t in ("l4", "h1", "s", "l1")]


def test_a_busy_seat_leaves_three_claims_from_the_top_of_the_queue(store):
    from scripts.swarm import tick
    from scripts.swarm.store import AgentRecord
    from tests.swarm.test_tick import FakeRuntime

    config, ledger, found = queue_ledger(store, "delegate")
    ledger.rows["x"] = {"id": "x", "state": "claimed", "lane": "eng", "depends_on": []}
    busy = [AgentRecord("engineer@abcdef-0001", "eng", "x", state="working")]
    claims, _ = tick._spawn_order(SLUG, config, store, busy, ledger.rows, found, FakeRuntime())
    assert [(lane, t["id"]) for lane, t in claims] == [("eng", t) for t in ("l4", "h1", "s")]


def test_a_task_whose_launch_failed_falls_behind_its_band_in_the_dispatcher_queue(store, monkeypatch):
    config, ledger, found = queue_ledger(store, "delegate")
    monkeypatch.setattr(store, "launch_failure", lambda slug, task_id: "boom" if task_id in ("h1", "l4") else None)
    monkeypatch.setattr(store, "launch_failures", lambda slug: {"h1": "boom", "l4": "boom"})
    assert spawned(store, config, ledger, found) == [("eng", t) for t in ("l4", "s", "l1", "l2")]


@pytest.mark.parametrize("autonomy", ["assist", "manual"])
def test_below_delegate_the_tick_keeps_claiming_by_rank(store, autonomy):
    config, ledger, found = queue_ledger(store, autonomy)
    assert spawned(store, config, ledger, found) == [("eng", t) for t in ("l3", "l4", "h1", "s")]


def test_the_claim_key_bands_only_operator_ranks_ahead_of_the_queue_position(store):
    config, _, found = queue_ledger(store, "delegate")
    key = dispatcher.claim_key(config, found)
    rows = {t["id"]: t for t in found["tasks"]}
    assert [key(rows[t]) for t in ("l4", "h1", "l3")] == [(1, 0), (2, 1), (2, 15)]
    assert key({"id": "missing"}) == (2, len(dispatcher.queue(found, "")))
    assert dispatcher.claim_key(store.update(SLUG, autonomy="assist"), found) is None


def chain_merge(store, autonomy="delegate"):
    from tests.swarm.test_tick import FakeLedger

    config = store.update(SLUG, autonomy=autonomy, max_eng=1)
    ledger = FakeLedger(
        [
            task("a", phase="", state="done"),
            task("a2", phase="", state="done"),
            task("b", phase="", depends_on=["a2", "a"]),
            task("c", phase="", depends_on=["b"]),
            task("x", phase="", rank="urgent"),
            task("r", phase="", branch="engineer-abcdef-0009"),
            task("y", phase=""),
        ]
    )
    found = ledger.state(SLUG)
    found["_meta"]["stamps"] = {
        "tasks/a/state": {"by": "engineer@abcdef-0001", "at": NOW - 30_000},
        "tasks/a2/state": {"by": "engineer@abcdef-0002", "at": NOW - 90_000},
        "tasks/x/rank": {"by": "operator"},
    }
    return config, ledger, found


CHAIN_COMMENT = (
    "The dispatcher's chain guard gave this task the next free eng seat 30 seconds after the task before it "
    "on the critical chain merged."
)


def test_a_critical_chain_successor_takes_the_next_seat_ahead_of_operator_urgent_and_resumed_work(store):
    config, ledger, found = chain_merge(store)
    assert dispatcher.guarded(found, "") == {"b"}
    key = dispatcher.claim_key(config, found)
    assert dispatcher.queue(found, "") == ["b", "x", "c", "r", "y"]
    assert [key(ledger.rows[t]) for t in ("b", "x", "r")] == [(-1, 0), (0, 1), (2, 3)]
    assert spawned(store, config, ledger, found) == [("eng", "b")]
    config, ledger, found = chain_merge(store, "assist")
    assert spawned(store, config, ledger, found) == [("eng", "x")]


def documentation_wait(store, autonomy, *waiting, ranked=True):
    from tests.swarm.test_tick import FakeLedger

    config = store.update(SLUG, autonomy=autonomy, max_eng=1)
    low = {"rank": "low"} if ranked else {}
    generated = task("doc-s", phase="", profile="documentation", follow_up=True, depends_on=["s"], **low)
    rows = [*waiting, generated] if ranked else [generated, *waiting]
    ledger = FakeLedger([task("s", phase="", state="done"), *rows])
    found = ledger.state(SLUG)
    found["_meta"]["stamps"] = {
        "tasks/s/state": {"by": "engineer@abcdef-0001", "at": NOW - 30_000},
        **({"tasks/doc-s/rank": {"by": "swarm"}} if ranked else {}),
        **{f"tasks/{t['id']}/rank": {"by": MASTER_NAME} for t in waiting if "rank" in t},
    }
    return config, ledger, found


@pytest.mark.parametrize("autonomy", ["delegate", "full", "assist"])
@pytest.mark.parametrize("rank", ["urgent", "high"])
def test_a_free_seat_takes_urgent_or_high_work_over_a_newer_documentation_task(store, autonomy, rank):
    config, ledger, found = documentation_wait(store, autonomy, task("u", phase="", rank=rank))
    assert spawned(store, config, ledger, found) == [("eng", "u")]
    assert "doc-s" not in dispatcher.guarded(found, "")


@pytest.mark.parametrize("autonomy", ["delegate", "full", "assist"])
def test_an_unranked_older_documentation_task_sorts_low_behind_urgent_work(store, autonomy):
    config, ledger, found = documentation_wait(store, autonomy, task("u", phase="", rank="urgent"), ranked=False)
    assert spawned(store, config, ledger, found) == [("eng", "u")]
    assert "doc-s" not in dispatcher.guarded(found, "")
    assert dispatcher.queue(found, "")[-1] == "doc-s"


def test_a_documentation_task_ranked_above_low_keeps_its_chain_guard_place(store):
    _, ledger, found = documentation_wait(store, "delegate", ranked=False)
    ledger.rows["doc-s"]["rank"] = "high"
    found["_meta"]["stamps"]["tasks/doc-s/rank"] = {"by": MASTER_NAME}
    assert dispatcher.guarded(found, "") == {"doc-s"}


@pytest.mark.parametrize("autonomy", ["delegate", "full", "assist"])
def test_a_documentation_task_takes_the_seat_when_no_urgent_or_high_task_is_claimable(store, autonomy):
    waiting = (task("w", phase="", state="claimed"), task("u", phase="", rank="urgent", depends_on=["w"]))
    config, ledger, found = documentation_wait(store, autonomy, *waiting)
    assert spawned(store, config, ledger, found) == [("eng", "doc-s")]


@pytest.mark.parametrize("autonomy", ["assist", "delegate"])
def test_a_swarm_low_task_that_unblocks_work_is_never_proposed_or_put_at_the_queue_head(store, home, shipped, autonomy):
    store.update(SLUG, autonomy=autonomy, max_eng=3)
    ledger, found = Ledger(), stamped(ladder(), {"a": "swarm"}, rank="low")
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW)
    assert proposed(actions) == (["b", "c"] if autonomy == "assist" else [])
    assert "a" not in {task_id for task_id, _, _ in ledger.comments}
    assert ledger.comments


def chain_of(*ids):
    return [task(task_id, phase="", depends_on=list(ids[index - 1 : index])) for index, task_id in enumerate(ids)]


def test_only_a_merged_predecessors_successor_on_the_critical_chain_is_guarded():
    tasks = [task("a", phase="", state="done"), task("b", phase="", depends_on=["a"]), *chain_of("p", "q", "s")]
    assert dispatcher.guarded(doc(tasks, phases=[]), "") == set()
    tasks.append(task("c", phase="", depends_on=["b"]))
    assert dispatcher.guarded(doc(tasks, phases=[]), "") == {"b"}


def held_fix_chain(freezes=()):
    from scripts.doctor import loop

    fix = loop.FIX_PHASE
    tasks = [
        task("a", state="done"),
        task("b", depends_on=["a"]),
        task("p", phase=fix),
        task("q", phase=fix, depends_on=["p"]),
        task("s", phase=fix, depends_on=["q"]),
    ]
    phases = [{"id": "p1", "depends_on": []}, {"id": fix, "depends_on": []}]
    stamps = {"tasks/a/state": {"by": "engineer@abcdef-0001", "at": NOW - 30_000}}
    return doc(tasks, phases=phases, freezes=list(freezes), _meta={"stamps": stamps})


FOCUS = [{"verb": "focus", "target": "phases/p1"}]


def test_the_critical_chain_counts_only_work_no_focus_holds_unless_the_fix_phase_exempts_it():
    from scripts.doctor import loop

    assert dispatcher.guarded(held_fix_chain(), "") == set()
    assert dispatcher.guarded(held_fix_chain(FOCUS), "") == {"b"}
    assert dispatcher.guarded(held_fix_chain(FOCUS), loop.FIX_PHASE) == set()
    tasks = [
        task("a", state="done"),
        task("t", depends_on=["a"]),
        task("u1", phase="p2", depends_on=["t"]),
        task("u2", phase="p2", depends_on=["u1"]),
        task("u3", phase="p2", depends_on=["u2"]),
        task("a2", state="done"),
        task("c", depends_on=["a2"]),
        task("w", depends_on=["c"]),
    ]
    phases = [{"id": "p1", "depends_on": []}, {"id": "p2", "depends_on": []}]
    assert dispatcher.guarded(doc(tasks, phases=phases, freezes=FOCUS), "") == {"c", "t"}


def test_the_doctor_fix_phase_reaches_the_guard_in_the_claim_key_and_the_chain_report(store, home, shipped):
    from scripts.doctor import priming

    found, ledger = held_fix_chain(FOCUS), Ledger()
    claimed = found["tasks"][1]
    config = store.update(SLUG, autonomy="delegate")
    assert dispatcher.claim_key(config, found)(claimed)[0] == dispatcher.GUARD
    assert dispatcher.chain_claimed(SLUG, config, ledger, found, claimed, "eng", NOW) != []
    config = store.update(SLUG, template=priming.TEMPLATE)
    assert dispatcher.claim_key(config, found)(claimed)[0] != dispatcher.GUARD
    assert dispatcher.chain_claimed(SLUG, config, ledger, found, claimed, "eng", NOW) == []


def test_the_doctor_fix_phase_reaches_the_dispatcher_queue_its_position_and_its_head(store, home, shipped):
    from scripts.doctor import loop, priming

    fix = loop.FIX_PHASE
    found = held_fix_chain(FOCUS)
    found["tasks"] += [task("v", phase=fix, depends_on=["s"]), task("w", depends_on=["b"])]
    claimed = found["tasks"][1]
    assert dispatcher.queue(found, "")[0] == "b"
    assert dispatcher.queue(found, fix)[0] == "p"
    config = store.update(SLUG, autonomy="delegate", template=priming.TEMPLATE, max_eng=1)
    key = dispatcher.claim_key(config, found)
    assert key(claimed)[0] != dispatcher.GUARD
    assert key(claimed)[1] == dispatcher.queue(found, fix).index("b") > 0
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, config, store, ledger, found, NOW)
    assert [comment[0] for comment in ledger.comments] == ["p"]


def test_a_chain_claim_reports_the_seconds_from_the_merge_on_the_task_and_logs_it(store, home, shipped):
    config, ledger, found = chain_merge(store)
    actions = dispatcher.chain_claimed(SLUG, config, ledger, found, ledger.rows["b"], "eng", NOW)
    assert actions == ["chain guard claimed task b 30 seconds after task a merged on the critical chain"]
    assert ledger.comments == [("b", CHAIN_COMMENT, "dispatcher")]
    (row,) = gate_log.recent(SLUG, None, home)
    assert (row["at"], row["gate"], row["kind"], row["task"], row["tool"], row["reason"]) == (
        NOW,
        "dispatcher",
        "apply",
        "b",
        "chain-guard",
        actions[0],
    )
    assert [(r["ts_ms"], r["task"], r["rule"], r["action"]) for _, rows, _ in shipped for r in rows] == [
        (NOW, "b", "chain-guard", actions[0])
    ]


def test_a_refused_chain_comment_still_logs_the_claim(store, home, shipped):
    config, _, found = chain_merge(store)
    ledger = Ledger(refused={"comment:b"})
    claimed = next(t for t in found["tasks"] if t["id"] == "b")
    actions = dispatcher.chain_claimed(SLUG, config, ledger, found, claimed, "eng", NOW)
    assert actions == [
        "chain guard claimed task b 30 seconds after task a merged on the critical chain" + dispatcher.UNCOMMENTED
    ]
    assert [r["reason"] for r in gate_log.recent(SLUG, None, home)] == actions


@pytest.mark.parametrize("autonomy", ["assist", "manual"])
def test_an_unguarded_claim_below_delegate_or_without_a_merge_time_reports_nothing(store, home, shipped, autonomy):
    config, ledger, found = chain_merge(store, autonomy)
    assert dispatcher.chain_claimed(SLUG, config, ledger, found, ledger.rows["b"], "eng", NOW) == []
    config = store.update(SLUG, autonomy="delegate")
    assert dispatcher.chain_claimed(SLUG, config, ledger, found, ledger.rows["y"], "eng", NOW) == []
    off = [task("a", phase="", state="done"), task("b", phase="", depends_on=["a"]), *chain_of("p", "q", "s")]
    stamps = {"tasks/a/state": {"by": "engineer@abcdef-0001", "at": NOW - 30_000}}
    off_chain = doc(off, phases=[], _meta={"stamps": stamps})
    assert dispatcher.chain_claimed(SLUG, config, ledger, off_chain, off[1], "eng", NOW) == []
    del found["_meta"]["stamps"]["tasks/a/state"], found["_meta"]["stamps"]["tasks/a2/state"]
    assert dispatcher.chain_claimed(SLUG, config, ledger, found, ledger.rows["b"], "eng", NOW) == []
    del found["_meta"]["stamps"]
    assert dispatcher.chain_claimed(SLUG, config, ledger, found, ledger.rows["b"], "eng", NOW) == []
    del found["_meta"]
    assert dispatcher.chain_claimed(SLUG, config, ledger, found, ledger.rows["b"], "eng", NOW) == []
    assert (ledger.comments, shipped, gate_log.recent(SLUG, None, home)) == ([], [], [])


def test_a_master_urgent_fix_phase_leaf_outside_the_focus_gets_no_rank_write_and_no_head_comment(store, home, shipped):
    from scripts.doctor import loop, priming

    store.update(SLUG, autonomy="delegate", template=priming.TEMPLATE)
    found = stamped([task("f", phase=loop.FIX_PHASE, rank="urgent")], {"f": MASTER_NAME}, rank="urgent")
    found["phases"] = [{"id": loop.FIX_PHASE, "depends_on": []}]
    found["freezes"] = [{"verb": "focus", "target": "lane:ci"}]
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW)
    assert (ledger.ranks, ledger.comments) == ([], [])


def test_the_dispatcher_writes_no_rank_at_full_over_master_and_operator_ranks(store, home, shipped):
    store.update(SLUG, autonomy="full")
    tasks = [*merging(), task("e", state="claimed"), task("f"), task("g", depends_on=["a"]), task("h", lane="ci")]
    tasks[0]["state"] = "done"
    hands = {t: MASTER_NAME for t in ("b", "q", "c0", "e", "f", "g", "h")} | {"c": "operator"}
    found = stamped(tasks, hands)
    found["freezes"] = [{"verb": "focus", "target": "lane:eng"}]
    rows = {t["id"]: t for t in tasks}
    rows["b"]["rank"], rows["q"]["rank"], rows["p"]["rank"] = "urgent", "normal", "normal"
    rows["g"]["rank"], rows["h"]["rank"] = "urgent", "urgent"
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, found, NOW)
    assert ledger.ranks == []


def test_a_rank_with_no_recorded_author_writes_no_rank_at_delegate(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=3)
    tasks = ladder()
    tasks[2]["rank"], tasks[3]["rank"] = "low", "high"
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(tasks), NOW)
    assert ledger.ranks == []
    assert ledger.comments == [
        entered("a", leads("1 open task", "3 open tasks")),
        entered("b", leads("1 open task", "2 open tasks")),
        entered("c", leads("1 open task", "1 open task")),
    ]


def test_two_operator_high_ranks_hold_two_of_the_top_places(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    tasks = [task(f"t{i}", phase="") for i in range(5)]
    tasks.append(task("w", phase="", depends_on=[f"t{i}" for i in range(5)]))
    tasks.append(task("v", phase="", depends_on=["t0", "t1"]))
    tasks[0]["rank"] = tasks[1]["rank"] = "high"
    stamps = {f"tasks/t{i}/rank": {"by": "operator"} for i in range(2)}
    found = doc(tasks, phases=[], _meta={"stamps": stamps})
    assert proposed(dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), found, NOW)) == ["t2"]


def test_two_operator_high_ranks_take_the_two_head_places_at_delegate(store, home, shipped):
    store.update(SLUG, autonomy="delegate", max_eng=2)
    tasks = [task(f"t{i}", phase="") for i in range(5)]
    tasks.append(task("w", phase="", depends_on=[f"t{i}" for i in range(5)]))
    tasks.append(task("v", phase="", depends_on=["t0", "t1"]))
    tasks[0]["rank"] = tasks[1]["rank"] = "high"
    stamps = {f"tasks/t{i}/rank": {"by": "operator"} for i in range(2)}
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, doc(tasks, phases=[], _meta={"stamps": stamps}), NOW)
    assert ledger.ranks == []
    assert ledger.comments == [entered(t, "the operator ranked it high") for t in ("t0", "t1")]


def test_below_delegate_a_top_task_already_ranked_high_is_not_proposed_again(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    tasks = ladder()
    tasks[0]["rank"] = "high"
    assert proposed(dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), doc(tasks), NOW)) == ["b", "c"]


def test_below_delegate_an_own_rank_is_never_lowered(store, home, shipped):
    store.update(SLUG, autonomy="assist")
    tasks = merging()
    tasks[0]["state"] = "done"
    ledger = Ledger()
    actions = dispatcher.rank_pass(SLUG, store.config(SLUG), store, ledger, stamped(tasks, {"c": "dispatcher"}), NOW)
    assert ledger.ranks == []
    assert [a.split(":")[0] for a in actions] == [f"proposed rank high for task {t} to the master" for t in "pbq"]


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
    monkeypatch.setattr(LedgerClient, "_call", lambda self, slug, ops=None: sent.append((slug, ops)) or {})
    assert LedgerClient().rank_task(SLUG, "a", "high", "dispatcher", if_owned=True) is None
    LedgerClient().rank_task(SLUG, "q", "low", "master@abcdef-0001")
    ((_, (guarded,)), (slug, (plain,))) = sent
    assert slug == SLUG
    assert {k: guarded[k] for k in ("op", "by", "item", "rank", "if_owned")} == {
        "op": "task_rank",
        "by": "dispatcher",
        "item": "tasks/a",
        "rank": "high",
        "if_owned": True,
    }
    assert "if_owned" not in plain and (plain["item"], plain["rank"]) == ("tasks/q", "low")


def test_a_task_merged_into_a_group_lead_is_never_ranked():
    found = doc([task("x", phase=""), task("y", phase="", merged_into="x"), task("w", phase="", depends_on=["x", "y"])])
    assert dispatcher.ordered(found, "", dispatcher.leverage(found)) == ["x", "w"]


def test_the_dispatch_row_carries_the_task_path_or_empty_strings():
    ranked = task("a", plan_url="http://plan", plan_slice="dp-step")
    landed = dispatcher.Action(dispatcher.LEVERAGE, "apply", "ranked", ranked)
    assert dispatcher.dispatch_row(SLUG, 2, landed, NOW) == {
        "event_id": f"dispatch:{SLUG}:leverage-rank:{NOW}:2",
        "ledger": SLUG,
        "ts_ms": NOW,
        "plan": "http://plan",
        "phase": "p1",
        "slice": "dp-step",
        "task": "a",
        "rule": "leverage-rank",
        "mode": "apply",
        "action": "ranked",
    }
    swept = dispatcher.dispatch_row(SLUG, 0, dispatcher.Action(dispatcher.PRIORITIES, "apply", "cleared", {}), NOW)
    assert {k: swept[k] for k in ("plan", "phase", "slice", "task")} == dict.fromkeys(
        ("plan", "phase", "slice", "task"), ""
    )


@pytest.mark.parametrize(
    ("autonomy", "mode"), [("delegate", "apply"), ("full", "apply"), ("assist", "propose"), ("manual", "propose")]
)
def test_grouping_rows_name_the_mode_its_autonomy_gives(store, home, shipped, monkeypatch, autonomy, mode):
    monkeypatch.setattr(grouping, "group_pass", lambda *a: ["grouped tasks b under a"])
    dispatcher.group(SLUG, store.update(SLUG, autonomy=autonomy), store, "L", doc([]), NOW)
    (row,) = gate_log.recent(SLUG, None, home)
    assert (row["kind"], row["at"]) == (mode, NOW)
    assert [(r["mode"], r["ts_ms"]) for r in shipped[0][1]] == [(mode, NOW)]


def test_priority_sweep_rows_carry_each_cleared_text_and_the_tick_time(store, home, shipped, monkeypatch):
    monkeypatch.setattr(priority_sweep, "priority_pass", lambda *a: ["cleared one", "cleared two"])
    dispatcher.priorities(store, SLUG, doc([]), "L", "V", NOW)
    assert [(r["action"], r["ts_ms"]) for r in shipped[0][1]] == [("cleared one", NOW), ("cleared two", NOW)]
    assert [(r["reason"], r["at"]) for r in gate_log.recent(SLUG, None, home)] == [
        ("cleared one", NOW),
        ("cleared two", NOW),
    ]


def test_the_tick_hands_its_clock_to_the_dispatcher_steps(monkeypatch):
    import fakeredis

    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=0, max_ci=0))
    seen = []
    monkeypatch.setattr(dispatcher, "group", lambda *a: seen.append(("group", a[-1])) or [])
    monkeypatch.setattr(dispatcher, "rank_pass", lambda *a: seen.append(("rank", a[-1])) or [])
    tick.tick(SLUG, saved, FakeLedger([{"id": "a"}]), FakeRuntime(), now_ms=4_321)
    assert seen == [("group", 4_321), ("rank", 4_321)]


@pytest.mark.parametrize("author", [MASTER_NAME, "swarm", "operator", dispatcher.AUTHOR])
def test_an_urgent_rank_sorts_in_the_urgent_band_whoever_set_it(store, author):
    config = store.update(SLUG, autonomy="delegate")
    found = stamped([task("u", phase=""), task("h", phase=""), task("m", phase="")], {"u": author}, rank="urgent")
    rows = {t["id"]: t for t in found["tasks"]}
    rows["h"]["rank"], rows["m"]["rank"] = "high", "high"
    found["_meta"]["stamps"]["tasks/h/rank"] = {"by": "operator"}
    found["_meta"]["stamps"]["tasks/m/rank"] = {"by": MASTER_NAME}
    key = dispatcher.claim_key(config, found)
    assert [key(rows[t])[0] for t in ("u", "h", "m")] == [0, 1, 2]
    assert dispatcher.queue(found, "") == ["u", "h", "m"]


def test_a_master_urgent_task_with_no_leverage_heads_its_lane_at_delegate(store, home, shipped):
    config = store.update(SLUG, autonomy="delegate", max_eng=1)
    found = stamped([task("x", phase=""), task("u", phase="")], {"u": MASTER_NAME}, rank="urgent")
    ledger = Ledger()
    dispatcher.rank_pass(SLUG, config, store, ledger, found, NOW)
    assert (ledger.ranks, ledger.comments) == ([], [entered("u", dispatcher.URGENT_RANK)])
