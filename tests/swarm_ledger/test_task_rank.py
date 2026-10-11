import pytest

from scripts.swarm_ledger import ledger, ledger_rank, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "taskrank-2026-01-01"
MASTER = "master@abcdef-0001"
ENGINEER = "engineer@abcdef-0002"
PLANNER = "planner@abcdef-0003"
DISPATCHER = "dispatcher@abcdef-0005"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_rank", ledger_rank)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG, ops=[{"op": "task_add", "id": "seed", "by": "swarm", "task": "t1", "title": "a", "lane": "eng"}])


def rank_of(state, task_id="t1"):
    return next(t for t in state["tasks"] if t["id"] == task_id).get("rank")


def update(by, rank, n=1):
    op = {"op": "task_update", "id": f"rank-{n}", "by": by, "item": "tasks/t1", "fields": {"rank": rank}}
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


def test_an_existing_task_has_no_rank_and_counts_as_normal():
    state, _ = core.sync(SLUG)
    assert rank_of(state) is None
    assert ledger_rank.order(state["tasks"][0]) == ledger_rank.RANKS.index("normal")


def test_task_add_stores_the_rank_it_is_given():
    op = {"op": "task_add", "id": "add-2", "by": MASTER, "task": "t2", "title": "b", "lane": "eng", "rank": "high"}
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    assert (rejected, rank_of(state, "t2")) == ([], "high")


def test_the_master_sets_a_rank_and_next_stores_urgent():
    state, rejected = update(MASTER, "low")
    assert (rejected, rank_of(state)) == ([], "low")
    state, rejected = update(MASTER, "next", 2)
    assert (rejected, rank_of(state)) == ([], "urgent")


def test_an_engineer_cannot_rank_a_task():
    state, rejected = update(ENGINEER, "urgent")
    assert rejected == ["rank-1"] and rank_of(state) is None
    assert "cannot set a task rank" in state["_meta"]["warnings"][0]


def test_an_engineer_cannot_add_a_ranked_task_either():
    op = {"op": "task_add", "id": "add-3", "by": ENGINEER, "task": "t3", "title": "c", "lane": "eng", "rank": "urgent"}
    state, rejected = core.sync(SLUG, ops=[op])
    assert rejected == ["add-3"] and "t3" not in [t["id"] for t in state["tasks"]]


def test_a_ranked_task_add_outside_the_allowlist_is_refused_and_an_unranked_one_lands():
    ranked = {"op": "task_add", "id": "add-4", "by": "someone", "task": "t4", "title": "d", "lane": "eng"}
    state, rejected = core.sync(SLUG, ops=[{**ranked, "rank": "high"}, {**ranked, "id": "add-5", "task": "t5"}])
    assert rejected == ["add-4"] and [t["id"] for t in state["tasks"]] == ["t1", "t5"]
    assert state["_meta"]["warnings"][0] == f"someone cannot set a task rank: {ledger_tasks.PROPOSE}"


@pytest.mark.parametrize("bad", ["top", "", "URGENT", 1, None])
def test_an_unknown_rank_is_refused(bad):
    with pytest.raises(ValueError, match="rank must be one of|as strings"):
        core.check_op({"op": "task_update", "id": "x", "by": MASTER, "item": "tasks/t1", "fields": {"rank": bad}})
    with pytest.raises(ValueError, match="rank must be one of"):
        core.check_op({"op": "task_rank", "id": "y", "item": "tasks/t1", "rank": bad})


def test_the_operator_ranks_a_task_from_the_page():
    op = {"op": "task_rank", "id": "page-1", "item": "tasks/t1", "rank": "next"}
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    assert (rejected, rank_of(state)) == ([], "urgent")
    assert state["_meta"]["events"][-1]["by"] == "operator"


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "task_rank", "id": "p", "item": "tasks/t1", "rank": "high", "by": MASTER, "extra": 1},
        {"op": "task_rank", "id": "p", "item": "tasks/t1", "rank": "high", "by": ""},
        {"op": "task_rank", "id": "p", "item": "phases/p1", "rank": "high"},
        {"op": "task_rank", "id": "p", "item": "tasks/t1"},
    ],
)
def test_the_page_rank_op_is_the_operators_and_names_a_task(bad):
    with pytest.raises(ValueError):
        core.check_op(bad)


@pytest.mark.parametrize("by", [MASTER, "dispatcher", DISPATCHER, "operator", "swarm"])
def test_task_rank_takes_an_allowed_author_and_records_it(by):
    op = {"op": "task_rank", "id": "auth-1", "item": "tasks/t1", "rank": "high", "by": by}
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    assert (rejected, rank_of(state)) == ([], "high")
    assert (state["_meta"]["events"][-1]["by"], state["_meta"]["stamps"]["tasks/t1/rank"]["by"]) == (by, by)


@pytest.mark.parametrize("by", [ENGINEER, PLANNER, "ci@abcdef-0004", "session-0a1b2c3d", "someone"])
def test_task_rank_refuses_an_author_outside_the_allowlist(by):
    op = {"op": "task_rank", "id": "auth-2", "item": "tasks/t1", "rank": "urgent", "by": by}
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    assert rejected == ["auth-2"] and rank_of(state) is None
    assert state["_meta"]["warnings"][0] == f"{by} cannot set a task rank: {ledger_tasks.PROPOSE}"


@pytest.mark.parametrize("by", [PLANNER, "session-0a1b2c3d", "someone"])
def test_a_task_update_rank_outside_the_allowlist_is_refused(by):
    state, rejected = update(by, "high")
    assert rejected == ["rank-1"] and rank_of(state) is None
    assert state["_meta"]["warnings"][0] == f"{by} cannot set a task rank: {ledger_tasks.PROPOSE}"


@pytest.mark.parametrize("by", [MASTER, "dispatcher", DISPATCHER, "swarm"])
def test_a_task_update_rank_by_an_allowed_author_applies(by):
    state, rejected = update(by, "high")
    assert (rejected, rank_of(state)) == ([], "high")


def test_a_difficulty_change_keeps_the_worker_lane_refusal_only():
    assert ledger_tasks.rank_refusal(ENGINEER, "difficulty") == (
        f"{ENGINEER} works in the eng lane and cannot set a task difficulty: {ledger_tasks.PROPOSE}"
    )
    assert ledger_tasks.rank_refusal("someone", "difficulty") == ""


def test_ranking_an_unknown_task_is_rejected():
    state, rejected = core.sync(SLUG, ops=[{"op": "task_rank", "id": "page-2", "item": "tasks/t9", "rank": "low"}])
    assert rejected == ["page-2"]


def test_task_cli_sends_the_rank(monkeypatch):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **f: sent.append((kind, f)))
    for argv in (
        ["task", "add", "t3", "b", "--rank", "urgent"],
        ["task", "add", "t4", "c"],
        ["task", "set", "t4", "rank=next"],
    ):
        ledger.cmd_task(ledger.build_parser().parse_args(["--slug", SLUG, "--as", MASTER, *argv]))
    assert [f.get("rank") for _, f in sent[:2]] == ["urgent", None]
    assert sent[2][1]["fields"] == {"rank": "next"}


@pytest.mark.parametrize(("rank", "shown"), [("urgent", "urgent"), (None, "normal"), ("bogus", "normal")])
def test_a_task_row_shows_its_rank_in_a_select(rank, shown):
    from tests.swarm_ledger.test_task_proof_page import PLAIN, nodes, render

    task = {**PLAIN, **({"rank": rank} if rank else {})}
    (pick,) = nodes(render(task)["tree"], f"rank-pick rank-{shown}")
    assert [option[2] for option in pick[3]] == list(ledger_rank.RANKS)


@pytest.mark.parametrize(
    ("task", "position"),
    [({"rank": "urgent"}, 0), ({"rank": "high"}, 1), ({}, 2), ({"rank": "x"}, 2), ({"rank": "low"}, 3)],
)
def test_order_puts_each_rank_in_its_place(task, position):
    assert ledger_rank.order(task) == position


@pytest.mark.parametrize(
    "extra",
    [
        {"by": ""},
        {"by": 7},
        {"by": None},
        {"if_owned": False},
        {"if_owned": 1},
        {"if_unranked": True},
        {"if_owned": True, "if_unranked": True},
    ],
)
def test_the_page_rank_op_refusal_names_its_shape(extra):
    with pytest.raises(ValueError) as refused:
        ledger_rank.check({"op": "task_rank", "id": "p", "item": "tasks/t1", "rank": "high", **extra})
    assert str(refused.value) == (
        "task_rank takes an id, an item tasks/<id>, a rank and an optional author by and if_owned"
    )


def owned_rank(rank, n, by="dispatcher"):
    op = {"op": "task_rank", "id": f"o-{n}", "item": "tasks/t1", "rank": rank, "by": by, "if_owned": True}
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


def test_an_if_owned_rank_lands_on_an_unranked_task_and_reorders_its_own_rank():
    state, rejected = owned_rank("high", 1)
    assert (rejected, rank_of(state)) == ([], "high")
    state, rejected = owned_rank("normal", 2)
    assert (rejected, rank_of(state), state["_meta"]["stamps"]["tasks/t1/rank"]["by"]) == ([], "normal", "dispatcher")


def test_an_if_owned_rank_on_an_operator_rank_is_refused_and_names_him():
    owned_rank("high", 1)
    core.sync(SLUG, ops=[{"op": "task_rank", "id": "g-1", "item": "tasks/t1", "rank": "urgent"}])
    state, rejected = owned_rank("normal", 2)
    assert (rejected, rank_of(state)) == (["o-2"], "urgent")
    assert state["_meta"]["warnings"][0] == (
        "task t1 has a rank the operator set: relay his words with task set t1 rank=<rank> --quote"
    )


@pytest.mark.parametrize("kind", ["task_update", "task_rank"])
def test_a_master_cannot_overwrite_an_operator_rank_without_his_words(kind):
    core.sync(SLUG, ops=[{"op": "task_rank", "id": "g-1", "item": "tasks/t1", "rank": "urgent"}])
    op = {"op": kind, "id": "m-1", "by": MASTER, "item": "tasks/t1"}
    op |= {"fields": {"rank": "low"}} if kind == "task_update" else {"rank": "low"}
    state, rejected = core.sync(SLUG, ops=[op])
    assert (rejected, rank_of(state), state["_meta"]["stamps"]["tasks/t1/rank"]["by"]) == (
        ["m-1"],
        "urgent",
        "operator",
    )
    assert state["_meta"]["warnings"][0].startswith("task t1 has a rank the operator set")
    state, rejected = core.sync(SLUG, ops=[{"op": "task_rank", "id": "g-2", "item": "tasks/t1", "rank": "low"}])
    assert (rejected, rank_of(state)) == ([], "low")


@pytest.mark.parametrize("by", [MASTER, "swarm", DISPATCHER])
def test_an_if_owned_rank_reorders_a_rank_a_master_or_the_swarm_set(by):
    core.sync(SLUG, ops=[{"op": "task_rank", "id": "g-1", "item": "tasks/t1", "rank": "urgent", "by": by}])
    state, rejected = owned_rank("normal", 2)
    assert (rejected, rank_of(state), state["_meta"]["stamps"]["tasks/t1/rank"]["by"]) == ([], "normal", "dispatcher")


def test_task_add_stamps_its_rank_so_the_dispatcher_may_reorder_a_master_rank():
    op = {"op": "task_add", "id": "add-9", "by": MASTER, "task": "t9", "title": "z", "lane": "eng", "rank": "low"}
    state, _ = core.sync(SLUG, ops=[op])
    assert state["_meta"]["stamps"]["tasks/t9/rank"]["by"] == MASTER
    assert "tasks/t1/rank" not in state["_meta"]["stamps"]
    guarded = {"op": "task_rank", "id": "o-9", "item": "tasks/t9", "rank": "high", "by": "dispatcher"}
    state, rejected = core.sync(SLUG, ops=[{**guarded, "if_owned": True}])
    assert (rejected, rank_of(state, "t9")) == ([], "high")


def test_an_if_owned_rank_on_a_rank_with_no_recorded_author_lands():
    doc, ctx = {"tasks": [{"id": "t1", "rank": "low"}]}, FakeContext()
    ctx.refused = []
    assert ledger_rank.apply(doc, {**page_rank("high"), "by": "dispatcher", "if_owned": True}, ctx) is True
    assert (doc["tasks"][0]["rank"], ctx.stamps, ctx.refused) == ("high", {"tasks/t1/rank": {"by": "dispatcher"}}, [])


def test_the_master_setting_the_same_rank_takes_it_from_the_dispatcher_who_may_reorder_it_again():
    owned_rank("high", 1)
    state, rejected = update(MASTER, "high")
    assert (rejected, state["_meta"]["stamps"]["tasks/t1/rank"]["by"]) == ([], MASTER)
    held = state["_meta"]["stamps"]["tasks/t1/rank"]
    state, rejected = update(MASTER, "high", 2)
    assert (rejected, state["_meta"]["stamps"]["tasks/t1/rank"]) == ([], held)
    state, rejected = owned_rank("normal", 2)
    assert (rejected, rank_of(state), state["_meta"]["stamps"]["tasks/t1/rank"]["by"]) == ([], "normal", "dispatcher")


def relayed(quote, n=1, by=MASTER):
    op = {"op": "task_update", "id": f"rel-{n}", "by": by, "item": "tasks/t1", "fields": {"rank": "high"}}
    core.check_op({**op, "quote": quote})
    return core.sync(SLUG, ops=[{**op, "quote": quote}])


def test_a_rank_the_master_relays_with_the_operators_words_is_his_and_stays_fixed():
    from hooks.context import operator_words

    operator_words.record(MASTER, "Rank the login fix high, it blocks everything")
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": MASTER, "role": "orchestrator"}])
    update(MASTER, "low")
    _, rejected = relayed("rank the login fix high")
    state, _ = core.sync(SLUG)
    assert rejected == []
    assert (rank_of(state), state["_meta"]["stamps"]["tasks/t1/rank"]["by"]) == ("high", "operator")
    event = state["_meta"]["events"][-1]
    assert {k: event[k] for k in ("by", "kind", "target", "text", "relayed_by", "relayed_from", "quote")} == {
        "by": "operator",
        "kind": "rank set",
        "target": "tasks/t1",
        "text": "high",
        "relayed_by": MASTER,
        "relayed_from": "master pane",
        "quote": "Rank the login fix high",
    }
    state, rejected = owned_rank("normal", 2)
    assert (rejected, rank_of(state)) == (["o-2"], "high")
    state, rejected = update(MASTER, "low", 3)
    assert (rejected, rank_of(state)) == (["rank-3"], "high")
    scoped = {"op": "task_update", "id": "s-1", "by": MASTER, "item": "tasks/t1", "fields": {"territory": ["docs"]}}
    state, rejected = core.sync(SLUG, ops=[scoped])
    assert (rejected, rank_of(state), next(t for t in state["tasks"] if t["id"] == "t1")["territory"]) == (
        [],
        "high",
        ["docs"],
    )


def test_a_relay_on_a_missing_task_is_refused():
    from hooks.context import operator_words

    operator_words.record(MASTER, "Rank the login fix high, it blocks everything")
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": MASTER, "role": "orchestrator"}])
    op = {"op": "task_update", "id": "rel-9", "by": MASTER, "item": "tasks/nope", "fields": {"rank": "high"}}
    state, rejected = core.sync(SLUG, ops=[{**op, "quote": "rank the login fix high"}])
    assert (rejected, state["_meta"]["warnings"][0]) == (["rel-9"], ledger_tasks.UNRELAYED)


@pytest.mark.parametrize("meta", [None, {}, {"stamps": {}}])
def test_an_unstamped_rank_is_never_pinned(meta):
    assert ledger_tasks.pinned_refusal({"item": "tasks/t1", "fields": {"rank": "high"}}, meta) == ""


def test_the_task_help_names_the_quote(capsys):
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["task", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--quote QUOTE task set rank=...: the operator's words, so the rank stays his" in help_text


@pytest.mark.parametrize("member", [None, "member"])
def test_a_relayed_rank_without_recorded_words_or_from_a_non_orchestrator_is_refused(member):
    from hooks.context import operator_words

    operator_words.record(MASTER, "Rank the login fix high")
    if member:
        core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": MASTER, "role": member}])
    state, rejected = relayed("rank the login fix high")
    assert (rejected, rank_of(state)) == (["rel-1"], None)
    assert state["_meta"]["warnings"][0] == ledger_tasks.UNRELAYED
    core.sync(SLUG, ops=[{"op": "join", "id": "j2", "by": MASTER, "role": "orchestrator"}])
    state, rejected = relayed("words he never said", 2)
    assert (rejected, rank_of(state)) == (["rel-2"], None)


@pytest.mark.parametrize("quote", ["high", "the login fix", "Rank the login"])
def test_a_relayed_rank_needs_three_of_his_words_naming_the_rank(quote):
    from hooks.context import operator_words

    operator_words.record(MASTER, "Rank the login fix high, it blocks everything")
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": MASTER, "role": "orchestrator"}])
    state, rejected = relayed(quote)
    assert (rejected, rank_of(state), state["_meta"]["warnings"][0]) == (["rel-1"], None, ledger_tasks.UNRELAYED)


@pytest.mark.parametrize(
    ("rank", "words", "named"),
    [
        ("urgent", "do this one next please", True),
        ("urgent", "make it urgent now", True),
        ("high", "make it highest now", False),
        ("low", "low", False),
        ("normal", "keep it normal", True),
        ("normal", "keep normal", False),
    ],
)
def test_quoted_needs_three_words_and_the_rank_or_its_alias(rank, words, named):
    assert ledger_rank.quoted(rank, words) is named


@pytest.mark.parametrize(
    ("quote", "fields"), [("", {"rank": "high"}), (7, {"rank": "high"}), ("his words", {"state": "open"})]
)
def test_a_task_update_quote_needs_words_and_a_rank(quote, fields):
    op = {"op": "task_update", "id": "q", "by": MASTER, "item": "tasks/t1", "fields": fields, "quote": quote}
    with pytest.raises(ValueError) as refused:
        ledger_tasks.check(op)
    assert str(refused.value) == "task_update takes quote, the operator's words, only with a rank"


def test_task_set_passes_the_operators_quote_with_the_rank(monkeypatch):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **f: sent.append((kind, f)))
    argv = ["--slug", SLUG, "--as", MASTER, "task", "set", "t1", "rank=high", "--quote", "his words"]
    ledger.cmd_task(ledger.build_parser().parse_args(argv))
    assert sent == [("task_update", {"item": "tasks/t1", "fields": {"rank": "high"}, "quote": "his words"})]


@pytest.mark.parametrize("action", [["add", "t5", "b", "--rank", "high"], ["group", "t1", "t2"]])
def test_only_task_set_takes_a_quote(monkeypatch, action):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **f: sent.append((kind, f)))
    argv = ["--slug", SLUG, "--as", MASTER, "task", *action, "--quote", "his words"]
    with pytest.raises(SystemExit) as refused:
        ledger.cmd_task(ledger.build_parser().parse_args(argv))
    assert str(refused.value) == (
        f"task {action[0]} takes no --quote: run task set <id> rank=<rank> --quote <his words> after it"
    )
    assert sent == []


def test_the_rank_op_by_a_refused_author_changes_nothing_and_names_why():
    doc, ctx = {"tasks": [{"id": "t1"}]}, FakeContext()
    ctx.refused = []
    assert ledger_rank.apply(doc, {**page_rank("high"), "by": ENGINEER}, ctx) is False
    assert (doc["tasks"][0], ctx.stamps, ctx.events, ctx.dirty) == ({"id": "t1"}, {}, [], False)
    assert ctx.refused == [f"{ENGINEER} cannot set a task rank: {ledger_tasks.PROPOSE}"]


class FakeContext:
    def __init__(self, stamps=None):
        self.stamps, self.events, self.dirty = dict(stamps or {}), [], False

    def stamp(self, path, by):
        self.stamps[path] = {"by": by}

    def record(self, by, kind, target, **extra):
        self.events.append((by, kind, target, extra))


def page_rank(rank):
    return {"op": "task_rank", "id": "p", "item": "tasks/t1", "rank": rank}


def test_the_page_rank_op_records_the_operator_and_marks_the_ledger_changed():
    doc, ctx = {"tasks": [{"id": "t0"}, {"id": "t1"}]}, FakeContext()
    assert ledger_rank.apply(doc, page_rank("next"), ctx) is True
    assert doc["tasks"][1] == {"id": "t1", "rank": "urgent"} and doc["tasks"][0] == {"id": "t0"}
    assert ctx.stamps == {"tasks/t1/rank": {"by": "operator"}}
    assert ctx.events == [("operator", "rank set", "tasks/t1", {"text": "urgent"})]
    assert ctx.dirty is True


@pytest.mark.parametrize(
    ("task", "rank"), [({"id": "t1", "rank": "normal"}, "normal"), ({"id": "t1", "rank": "low"}, "low")]
)
def test_setting_the_rank_a_task_already_has_from_the_same_author_changes_nothing(task, rank):
    held = {"tasks/t1/rank": {"by": "operator"}}
    doc, ctx = {"tasks": [task]}, FakeContext(held)
    assert ledger_rank.apply(doc, page_rank(rank), ctx) is True
    assert (doc["tasks"][0], ctx.stamps, ctx.events, ctx.dirty) == (dict(task), held, [], False)


@pytest.mark.parametrize("held", [{}, {"tasks/t1/rank": {"by": "dispatcher"}}])
def test_setting_the_same_rank_as_another_author_takes_the_rank_over(held):
    doc, ctx = {"tasks": [{"id": "t1", "rank": "high"}]}, FakeContext(held)
    assert ledger_rank.apply(doc, page_rank("high"), ctx) is True
    assert (doc["tasks"][0], ctx.stamps, ctx.dirty) == (
        {"id": "t1", "rank": "high"},
        {"tasks/t1/rank": {"by": "operator"}},
        True,
    )


def test_an_explicit_normal_on_an_unranked_task_is_stored_so_the_dispatcher_leaves_it():
    doc, ctx = {"tasks": [{"id": "t1"}]}, FakeContext()
    assert ledger_rank.apply(doc, page_rank("normal"), ctx) is True
    assert (doc["tasks"][0], ctx.stamps, ctx.dirty) == (
        {"id": "t1", "rank": "normal"},
        {"tasks/t1/rank": {"by": "operator"}},
        True,
    )


def test_the_page_rank_op_on_a_ledger_without_tasks_is_rejected():
    assert ledger_rank.apply({}, page_rank("low"), FakeContext()) is False


def test_task_add_help_names_the_ranks(capsys):
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["task", "--help"])
    assert "--rank RANK queue rank: urgent, high, normal (default) or low; next means urgent --plan" in " ".join(
        capsys.readouterr().out.split()
    )
