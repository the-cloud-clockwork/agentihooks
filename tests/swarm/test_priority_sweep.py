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


def test_agent_comment_that_resolves_a_question_is_not_recorded_as_an_answer(env):
    path = item("questions")
    run(env)
    agent_comment(path, "The operator chose the small database in chat.")
    run(env)
    assert state(SLUG)["questions"][0]["answers"] == []
    assert path not in priorities()


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
