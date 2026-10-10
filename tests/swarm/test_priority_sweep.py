import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.swarm import priority_sweep
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import RedisStore
from tests.doctor.test_doctor_cli import FileLedger, core, new_ledger, state

pytestmark = pytest.mark.xdist_group("fakeredis")
SLUG = "prio-sweep"
ASK = "Pick the port for the demo server."


@pytest.fixture
def env(monkeypatch, tmp_path):
    import fakeredis

    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    content = {
        "title": "Sweep",
        "overview": "o",
        "phases": [{"title": "Build", "description": "d"}],
        "questions": [{"text": "Which database should the demo use?"}],
        "followups": [{"text": "Pick a port for the demo"}],
    }
    assert new_ledger.create(SLUG, content)
    ledger = FileLedger()
    ledger.add_task(SLUG, {"task": "t1", "title": "Ship the demo", "lane": "eng"}, "init-swarm")
    return RedisStore(fakeredis.FakeRedis(decode_responses=True)), ledger


class Judge:
    def __init__(self, yes=0.9, error=None):
        self.yes, self.error, self.asked = yes, error, []

    def __call__(self, state, questions, *, purpose):
        self.asked.append((state, questions, purpose))
        if self.error:
            raise self.error
        return DecisionResult({name: Answer("noul", noul=self.yes) for name in questions}, "stub")


def no_github(url):
    raise AssertionError(f"no pull request lookup expected for {url}")


def run(env, judge=None, github=no_github):
    store, ledger = env
    return priority_sweep.priority_pass(store, SLUG, state(SLUG), ledger, judge=judge or Judge(), github=github)


def item(name):
    return f"{name}/{state(SLUG)[name][0]['id']}"


def raise_priority(env, path, text=ASK):
    env[1].priority(SLUG, path, text)


def priorities():
    return {p["item"]: p for p in state(SLUG)["priorities"]}


def cleared(path):
    return [e for e in state(SLUG)["_meta"]["events"] if e["kind"] == "priority cleared" and e["target"] == path]


def operator_comment(path, text, cid="c-op"):
    core.sync(SLUG, ops=[{"op": "add", "id": cid, "thread": f"{path}/comments", "text": text}])


def agent_comment(path, text, by="engineer@sw-0001", cid="c-ag"):
    core.sync(SLUG, ops=[{"op": "add", "id": cid, "by": by, "thread": f"{path}/comments", "text": text}])


def test_sweep_clears_a_priority_whose_follow_up_is_done(env):
    path = item("followups")
    raise_priority(env, path)
    core.sync(SLUG, ops=[{"op": "set", "id": "s", "by": "boss", "path": f"{path}/done", "value": True}])
    actions = run(env)
    assert path not in priorities()
    assert [e["reason"] for e in cleared(path)] == ["its item is done"]
    assert cleared(path)[0]["by"] == "swarm"
    assert actions == [f"cleared the priority on {path}: its item is done"]


def test_sweep_clears_a_priority_whose_task_is_done(env):
    raise_priority(env, "tasks/t1")
    env[1].update_task(SLUG, "t1", {"state": "done"})
    run(env)
    assert [e["reason"] for e in cleared("tasks/t1")] == ["its item is done"]


def test_sweep_clears_a_priority_whose_item_is_out_of_scope(env):
    path = item("phases")
    raise_priority(env, path)
    core.sync(SLUG, ops=[{"op": "set", "id": "s", "by": "boss", "path": f"{path}/out_of_scope", "value": True}])
    run(env)
    assert [e["reason"] for e in cleared(path)] == ["its item is out of scope"]


def test_sweep_clears_a_priority_whose_pull_request_merged(env):
    url = "https://github.com/o/r/pull/1"
    raise_priority(env, "tasks/t1")
    env[1].update_task(SLUG, "t1", {"state": "pr", "pr_url": url})
    seen = []

    def github(asked):
        seen.append(asked)
        return PullRequest("MERGED", 1, 1, False)

    run(env, github=github)
    assert seen == [url]
    assert [e["reason"] for e in cleared("tasks/t1")] == ["its pull request merged"]


@pytest.mark.parametrize("found", [None, PullRequest("OPEN", None, 1, False)])
def test_sweep_keeps_a_priority_whose_pull_request_is_not_merged(env, found):
    raise_priority(env, "tasks/t1")
    env[1].update_task(SLUG, "t1", {"state": "pr", "pr_url": "https://github.com/o/r/pull/1"})
    run(env, github=lambda url: found)
    assert "tasks/t1" in priorities()


def test_sweep_keeps_open_items_and_leaves_derived_rows_to_the_ledger(env):
    raise_priority(env, item("followups"))
    raise_priority(env, "tasks/t1")
    before = priorities()
    assert before[item("questions")].get("derived")
    assert run(env) == []
    assert priorities() == before


def test_sweep_clears_a_priority_whose_item_is_gone():
    row = {"id": "p1", "item": "tasks/t9", "text": ASK, "by": "swarm", "at": 1}
    assert list(priority_sweep.sweep({"tasks": []}, [row], no_github)) == [(row, "its item is gone")]


def test_first_pass_only_sets_the_cursor(env):
    path = item("followups")
    raise_priority(env, path)
    operator_comment(path, "Use port nine thousand.")
    judge = Judge()
    run(env, judge)
    assert judge.asked == []
    assert path in priorities()


def test_operator_comment_that_resolves_a_follow_up_clears_it_and_marks_it_done(env):
    path = item("followups")
    raise_priority(env, path)
    run(env)
    operator_comment(path, "Use port nine thousand.")
    judge = Judge(yes=0.91)
    actions = run(env, judge)
    assert path not in priorities()
    followup = state(SLUG)["followups"][0]
    assert followup["done"] is True
    reason = "the classifier judged that the comment from the operator resolves it, at probability 0.91"
    assert [e["reason"] for e in cleared(path)] == [reason]
    assert followup["comments"][-1]["by"] == "swarm"
    assert followup["comments"][-1]["text"] == f"Priority cleared by the swarm: {reason}."
    assert actions == [f"cleared the priority on {path}: {reason}"]
    (asked_state, questions, purpose) = judge.asked[0]
    assert purpose == priority_sweep.PURPOSE
    assert asked_state["priority"] == ASK
    assert asked_state["write"] == {"by": "the operator", "kind": "comment", "text": "Use port nine thousand."}
    assert asked_state["item"] == "Pick a port for the demo"
    (question,) = questions.values()
    assert "Pick a port for the demo" in question.instructions and ASK in question.instructions


def test_a_no_leaves_the_priority(env):
    path = item("followups")
    raise_priority(env, path)
    run(env)
    operator_comment(path, "Still thinking about it.")
    judge = Judge(yes=0.2)
    assert run(env, judge) == []
    assert path in priorities()
    assert len(judge.asked) == 1
    assert not state(SLUG)["followups"][0].get("done")


def test_a_down_classifier_leaves_the_priority(env):
    path = item("followups")
    raise_priority(env, path)
    run(env)
    operator_comment(path, "Use port nine thousand.")
    assert run(env, Judge(error=ClassifierUnavailable("down"))) == []
    assert path in priorities()


def test_operator_comment_on_a_question_is_recorded_as_its_answer(env):
    path = item("questions")
    assert priorities()[path].get("derived")
    run(env)
    operator_comment(path, "Use the small database.")
    run(env)
    question = state(SLUG)["questions"][0]
    assert [(a["by"], a["text"]) for a in question["answers"]] == [("operator", "Use the small database.")]
    assert path not in priorities()
    assert len(cleared(path)) == 1


def test_agent_comment_on_a_question_is_not_its_answer_and_leaves_its_priority(env):
    path = item("questions")
    run(env)
    agent_comment(path, "The operator chose the small database in chat.")
    judge = Judge(yes=1.0)
    assert run(env, judge) == []
    assert judge.asked == []
    assert state(SLUG)["questions"][0]["answers"] == []
    assert path in priorities()


APPROVE = "Please approve the merge of the demo, its checks are green."


def await_approval(env):
    env[1].update_task(SLUG, "t1", {"state": "pr", "pr_url": "https://github.com/o/r/pull/1", "awaiting": "approval"})
    assert priorities()["tasks/t1"]["text"].startswith("Approve the merge")
    run(env)


def flag_for_operator(env):
    path = item("followups")
    core.sync(SLUG, ops=[{"op": "set", "id": "s", "by": "boss", "path": f"{path}/needs_operator", "value": True}])
    assert priorities()[path]["text"].startswith("Decide")
    run(env)
    return path


def test_an_agent_comment_asking_for_the_approval_never_clears_it(env):
    await_approval(env)
    agent_comment("tasks/t1", APPROVE)
    judge = Judge(yes=1.0)
    assert run(env, judge) == []
    assert judge.asked == []
    assert "tasks/t1" in priorities()
    assert cleared("tasks/t1") == []


def test_an_agent_chat_line_naming_an_approval_never_clears_it(env):
    await_approval(env)
    env[1].say(SLUG, "Approval for t1 is in, merging now.", by="engineer@sw-0001")
    judge = Judge(yes=1.0)
    assert run(env, judge) == []
    assert judge.asked == []
    assert "tasks/t1" in priorities()


def test_an_agent_comment_never_clears_a_follow_up_flagged_for_the_operator(env):
    path = flag_for_operator(env)
    agent_comment(path, "The operator decided this one already.", by="master@sw-0001")
    judge = Judge(yes=1.0)
    assert run(env, judge) == []
    assert judge.asked == []
    assert path in priorities()
    assert not state(SLUG)["followups"][0].get("done")


def test_an_operator_comment_clears_a_merge_approval(env):
    await_approval(env)
    operator_comment("tasks/t1", "Approved, merge it.")
    judge = Judge(yes=0.95)
    run(env, judge)
    assert judge.asked[0][0]["write"]["by"] == "the operator"
    assert "tasks/t1" not in priorities()
    assert len(cleared("tasks/t1")) == 1


def test_a_master_relay_with_a_verified_operator_quote_clears_a_merge_approval(env):
    from hooks.context import operator_words

    master = "master@sw-0001"
    core.sync(SLUG, ops=[{"op": "join", "id": "j-m", "by": master, "role": "orchestrator"}])
    operator_words.record(master, "yes approve the demo merge")
    await_approval(env)
    relay = {"op": "relay", "id": "rl-1", "by": master, "item": "tasks/t1", "text": "Approved, merge it."}
    core.sync(SLUG, ops=[relay | {"quote": "approve the demo merge"}])
    judge = Judge(yes=0.95)
    run(env, judge)
    assert judge.asked[0][0]["write"]["by"] == "the operator"
    assert "tasks/t1" not in priorities()
    assert len(cleared("tasks/t1")) == 1


@pytest.mark.parametrize(
    "path, item, decides",
    [
        ("questions/q1", {}, True),
        ("tasks/t1", {"state": "pr", "awaiting": "approval"}, True),
        ("tasks/t1", {"state": "pr", "awaiting": "checks"}, False),
        ("tasks/t1", {"state": "blocked"}, False),
        ("followups/f1", {"needs_operator": True}, True),
        ("followups/f1", {}, False),
        ("phases/p1", {"review": {"escalated": True}}, True),
        ("phases/p1", {"review": {"escalated": False}}, False),
        ("phases/p1", {"review": None}, False),
        ("phases/p1", {}, False),
    ],
)
def test_only_operator_decisions_refuse_an_agents_write(path, item, decides):
    row = {"item": path}
    doc = {path.split("/")[0]: [{"id": path.split("/")[1], **item}]}
    assert priority_sweep._operator_decides(path, item) is decides
    assert priority_sweep._counts(doc, row, priority_sweep.Write("eng-1@sw", "comment", path, "x")) is not decides
    assert priority_sweep._counts(doc, row, priority_sweep.Write("operator", "comment", path, "x")) is True


@pytest.mark.parametrize(
    "by, handed",
    [
        ("dispatcher@a1b2c3-0001", True),
        ("master@a1b2c3-0001", False),
        ("engineer@a1b2c3-0001", False),
        ("dispatcher", False),
        ("ledger", False),
        (None, False),
    ],
)
def test_a_priority_a_dispatcher_handed_to_the_operator_counts_only_his_writes(by, handed):
    path = "tasks/t1"
    row = {"item": path, "by": by}
    doc = {"tasks": [{"id": "t1", "state": "blocked"}]}
    assert priority_sweep.handed(row) is handed
    for agent in ("dispatcher@a1b2c3-0001", "eng-1@sw"):
        assert priority_sweep._counts(doc, row, priority_sweep.Write(agent, "comment", path, "x")) is not handed
    assert priority_sweep._counts(doc, row, priority_sweep.Write("operator", "comment", path, "x")) is True


def test_operator_only_covers_handed_rows_and_operator_decisions():
    empty = {"questions": [], "followups": [], "tasks": []}
    assert priority_sweep.operator_only(empty, {"item": "questions/q1"}) is True
    assert priority_sweep.operator_only(empty, {"item": "followups/f1"}) is False
    assert priority_sweep.operator_only(empty, {"item": "tasks/t1", "by": "dispatcher@a1b2c3-0001"}) is True
    approval = {"tasks": [{"id": "t1", "awaiting": "approval"}]}
    assert priority_sweep.operator_only(approval, {"item": "tasks/t1", "by": "master@a1b2c3-0001"}) is True


def test_a_master_relay_without_the_operators_words_never_clears_it(env):
    master = "master@sw-0001"
    core.sync(SLUG, ops=[{"op": "join", "id": "j-m", "by": master, "role": "orchestrator"}])
    await_approval(env)
    relay = {"op": "relay", "id": "rl-2", "by": master, "item": "tasks/t1", "text": "Approved, merge it."}
    core.sync(SLUG, ops=[relay | {"quote": "words he never said"}])
    judge = Judge(yes=1.0)
    assert run(env, judge) == []
    assert "tasks/t1" in priorities()


def test_an_amended_agent_comment_is_judged_by_its_new_text(env):
    raise_priority(env, "tasks/t1")
    agent_comment("tasks/t1", "Looking at it.")
    run(env)
    agent_comment("tasks/t1", "The operator approved it in chat.", cid="c-ag2")
    judge = Judge()
    run(env, judge)
    assert judge.asked[0][0]["write"]["text"] == "The operator approved it in chat."
    assert judge.asked[0][0]["write"]["by"] == "an agent"


def test_a_chat_line_counts_only_for_the_item_it_names(env):
    raise_priority(env, "tasks/t1")
    raise_priority(env, item("followups"))
    run(env)
    env[1].say(SLUG, "Go ahead with t1, it is approved.")
    env[1].say(SLUG, "Nice work everyone.")
    judge = Judge()
    run(env, judge)
    assert [s["item"] for s, _, _ in judge.asked] == ["Ship the demo"]
    assert judge.asked[0][0]["write"]["kind"] == "chat line"
    assert "tasks/t1" not in priorities()
    assert item("followups") in priorities()


def test_the_swarm_never_judges_its_own_writes(env):
    raise_priority(env, "tasks/t1")
    run(env)
    env[1].comment(SLUG, "t1", "A note from the swarm.", "swarm")
    judge = Judge()
    run(env, judge)
    assert judge.asked == []


def test_a_write_on_an_item_without_a_priority_is_not_judged(env):
    run(env)
    agent_comment("tasks/t1", "Done with the first half.")
    judge = Judge()
    run(env, judge)
    assert judge.asked == []


def test_run_tick_runs_the_priority_pass(env, monkeypatch):
    from scripts.swarm import cli as swarm_cli
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeRuntime

    store, ledger = env
    monkeypatch.setattr(swarm_cli, "connect", lambda: store)
    monkeypatch.setattr(swarm_cli, "LedgerClient", FileLedger)
    monkeypatch.setattr(swarm_cli, "HerdrRuntime", FakeRuntime)
    monkeypatch.setattr(swarm_cli.timer, "ensure", lambda binary: True)
    assert swarm_cli.main([SLUG, "create", "--repo", "/repo", "--max-eng-agents", "0"]) == 0
    path = item("followups")
    raise_priority(env, path)
    core.sync(SLUG, ops=[{"op": "set", "id": "s", "by": "boss", "path": f"{path}/done", "value": True}])
    actions = swarm_cli.run_tick(store, SLUG, ledger, FakeRuntime(), FakeHerdr({}))
    assert f"cleared the priority on {path}: its item is done" in actions
    assert path not in priorities()


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, *args))


def row(item, at=1, derived=False, rid=None):
    return {"id": rid or f"p-{item}", "item": item, "text": ASK, "by": "swarm", "at": at, "derived": derived}


def write(rev, target, text=None, kind="comment added", by="operator", at=10, wid="c1"):
    event = {"rev": rev, "at": at, "by": by, "kind": kind, "target": target, "id": wid}
    return event if text is None else {**event, "text": text}


def dict_pass(rows, events=(), judge=None, github=no_github, **lists):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.redis.set(store.key("d", priority_sweep.CURSOR), 0)
    doc = {"phases": [], "questions": [], "followups": [], "tasks": [], **lists}
    doc["_meta"] = {"rev": 99, "events": list(events)}
    if rows is not None:
        doc["priorities"] = rows
    ledger = Recorder()
    actions = priority_pass_on(store, doc, ledger, judge or Judge(), github)
    return actions, ledger.calls


def priority_pass_on(store, doc, ledger, judge, github):
    return priority_sweep.priority_pass(store, "d", doc, ledger, judge=judge, github=github)


def asked_texts(judge):
    return [(s["item"], s["write"]["text"]) for s, _, _ in judge.asked]


def test_a_doc_without_priorities_clears_nothing():
    assert dict_pass(None, [write(1, "tasks/t1", "Done.")]) == ([], [])


def test_the_sweep_leaves_a_derived_row_to_the_ledger():
    actions, _ = dict_pass([row("tasks/t1", derived=True)], tasks=[{"id": "t1", "done": True}])
    assert actions == []


def test_a_task_in_state_done_is_done_without_its_flag():
    _, calls = dict_pass([row("tasks/t1")], tasks=[{"id": "t1", "state": "done"}])
    assert calls == [("clear_priority", "d", "p-tasks/t1", "its item is done")]


def test_a_claimed_task_with_a_pull_request_is_not_looked_up():
    actions, _ = dict_pass([row("tasks/t1")], tasks=[{"id": "t1", "state": "claimed", "pr_url": "u"}])
    assert actions == []


def test_a_swept_priority_is_not_judged_in_the_same_pass():
    judge = Judge()
    tasks = [{"id": "t1", "title": "Ship", "done": True, "comments": []}]
    actions, _ = dict_pass([row("tasks/t1")], [write(1, "tasks/t1", "Shipped.")], judge, tasks=tasks)
    assert judge.asked == []
    assert actions == ["cleared the priority on tasks/t1: its item is done"]


def test_one_text_is_judged_once_and_later_items_still_are():
    judge = Judge(yes=0.1)
    followups = [{"id": "f1", "text": "Pick a port", "comments": [{"id": "c1", "text": "Port nine."}]}]
    tasks = [{"id": "t1", "title": "Ship", "comments": []}]
    events = [
        write(1, "followups/f1", "Port nine."),
        write(2, "followups/f1", kind="comment edited"),
        write(3, "tasks/t1", "Ship it."),
    ]
    dict_pass([row("followups/f1"), row("tasks/t1")], events, judge, followups=followups, tasks=tasks)
    assert asked_texts(judge) == [("Pick a port", "Port nine."), ("Ship", "Ship it.")]


def test_a_cleared_priority_is_not_judged_again_in_the_same_pass():
    judge = Judge()
    tasks = [{"id": "t1", "title": "Ship", "comments": []}]
    events = [write(1, "tasks/t1", "Ship it."), write(2, "tasks/t1", "Really, ship it.")]
    _, calls = dict_pass([row("tasks/t1")], events, judge, tasks=tasks)
    assert asked_texts(judge) == [("Ship", "Ship it.")]
    assert [c[0] for c in calls] == ["clear_priority", "comment_item"]


def test_an_even_answer_counts_as_a_yes():
    actions, _ = dict_pass([row("tasks/t1")], [write(1, "tasks/t1", "Go.")], Judge(yes=0.5), tasks=[{"id": "t1"}])
    assert actions == [
        "cleared the priority on tasks/t1: the classifier judged that the comment from the operator resolves it, "
        "at probability 0.50"
    ]


def test_events_that_are_not_writes_or_carry_no_text_are_skipped():
    judge = Judge(yes=0.1)
    tasks = [{"id": "t1", "title": "Ship", "comments": []}]
    events = [
        {"rev": 1, "at": 10, "by": "engineer@sw-0001", "kind": "joined", "target": ""},
        write(2, "tasks/t1", kind="comment edited", wid="missing"),
        write(3, "tasks/t1", "Ship it."),
    ]
    dict_pass([row("tasks/t1")], events, judge, tasks=tasks)
    assert asked_texts(judge) == [("Ship", "Ship it.")]


@pytest.mark.parametrize("raised, judged", [(10, True), (11, False)])
def test_a_write_counts_only_from_the_moment_its_priority_was_raised(raised, judged):
    judge = Judge(yes=0.1)
    dict_pass([row("tasks/t1", at=raised)], [write(1, "tasks/t1", "Go.", at=10)], judge, tasks=[{"id": "t1"}])
    assert bool(judge.asked) is judged


def test_an_edit_on_a_gone_item_is_skipped():
    judge = Judge()
    dict_pass([row("tasks/t9", derived=True)], [write(1, "tasks/t9", kind="comment edited")], judge)
    assert judge.asked == []


def test_an_edited_answer_is_judged_by_its_new_text():
    judge = Judge(yes=0.1)
    questions = [{"id": "q1", "text": "Which?", "comments": [], "answers": [{"id": "a1", "text": "The small one."}]}]
    event = write(1, "questions/q1", kind="answer edited", wid="a1")
    dict_pass([row("questions/q1", derived=True)], [event], judge, questions=questions)
    assert asked_texts(judge) == [("Which?", "The small one.")]
    assert judge.asked[0][0]["write"]["kind"] == "answer"


def test_the_question_names_both_outcomes():
    judge = Judge(yes=0.1)
    dict_pass([row("tasks/t1")], [write(1, "tasks/t1", "Go.")], judge, tasks=[{"id": "t1"}])
    (question,) = judge.asked[0][1].values()
    assert question.criteria() == {
        "true": "the write resolves what the priority asks",
        "false": "the priority still waits",
    }


def test_the_pass_keeps_its_own_cursor(env):
    store, _ = env
    run(env)
    assert store.redis.get(store.key(SLUG, "priority-cursor")) == str(state(SLUG)["_meta"]["rev"])
