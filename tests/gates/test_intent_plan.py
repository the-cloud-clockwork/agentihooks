import json

from hooks.classifier import ClassifierUnavailable, definitions
from hooks.classifier.result import Answer, DecisionResult
from scripts.gates import intent, intent_history
from scripts.gates.verdicts import Verdicts

SLUG, ME, TASK = "demo", "engineer@1-1", "t1"
NOW = 1_000_000_000
URL = "https://github.com/o/r/pull/9"
DOC = {
    "overview": "Swarm tooling.",
    "phases": [{"id": "p1", "title": "Swarm runtime fixes", "description": "Defects the running swarm hits."}],
    "tasks": [
        {
            "id": TASK,
            "phase": "p1",
            "title": "Refuse revisionless events",
            "description": "The ledger refuses an event without a revision and the metrics pass skips one.",
            "state": "claimed",
            "claimed_by": ME,
            "proof": {"command": "pytest"},
        }
    ],
}
TRACED = {
    "plan_hash": "h1",
    "pieces": [
        {
            "what": "Refuse revisionless events",
            "areas": ["events.py", "test_events.py"],
            "why": "keep bad rows out",
            "kept": True,
        },
        {"what": "Skip a stored bad event", "areas": ["metrics.py"], "why": "one row never stops a pass", "kept": True},
        {"what": "Recolour the page", "areas": ["palette.css"], "why": "dropped", "kept": False},
    ],
}


def answered(seen=None, **values):
    def decide(state, questions, purpose):
        if seen is not None:
            seen.append((state, questions, purpose))
        return DecisionResult({name: Answer("noul", noul=values.get(name, 0.9)) for name in questions}, "liquid-d1")

    return decide


def plan_check(tmp_path, monkeypatch, seen=None, mode="observe", **values):
    real = intent.judged
    monkeypatch.setattr(intent, "judged", lambda state: real(state, decide=answered(seen, **values)))
    return intent.plan_check(SLUG, DOC, TASK, TRACED, mode, NOW, home=tmp_path)


def log_rows(tmp_path):
    path = tmp_path / SLUG / "gates" / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_the_plan_check_asks_plan_stage_questions_with_no_as_delivered_demand(tmp_path, monkeypatch):
    seen = []
    plan_check(tmp_path, monkeypatch, seen, weakens=0.1)
    [(state, questions, purpose)] = seen
    assert state == {
        "stage": "plan",
        "overview": "Swarm tooling.",
        "phase": "Swarm runtime fixes",
        "phase_intent": "Defects the running swarm hits.",
        "task": "Refuse revisionless events",
        "task_text": "The ledger refuses an event without a revision and the metrics pass skips one.",
        "planned_pieces": [
            "Refuse revisionless events | events.py, test_events.py | keep bad rows out",
            "Skip a stored bad event | metrics.py | one row never stops a pass",
        ],
        "planned_areas": ["events.py", "metrics.py", "test_events.py"],
    }
    assert (list(questions), purpose) == (["aligned", "covers", "weakens"], "intent-check")
    for question in questions.values():
        assert "not built yet" in question.instructions
        assert "as delivered" not in question.instructions


def test_a_fitting_plan_passes_and_records_its_stage_criterion_and_model(tmp_path, monkeypatch):
    assert plan_check(tmp_path, monkeypatch, weakens=0.1) == {
        "verdict": "pass",
        "reason": "the plan serves the phase at probability 0.90 and covers the task at probability 0.90",
    }
    record = Verdicts(SLUG, "intent", tmp_path).read(TASK)
    digest = definitions.load(intent.PLAN).digest
    assert record == {
        "verdict": "pass",
        "reason": "the plan serves the phase at probability 0.90 and covers the task at probability 0.90",
        "at": NOW,
        "phase": "p1",
        "planned": "h1",
        "stage": "plan",
        "criterion": None,
        "model": "liquid-d1",
        "definition": "intent-check-plan",
        "definition_digest": digest,
    }
    [history] = [json.loads(line) for line in (tmp_path / SLUG / "gates" / "intent" / "history.jsonl").open()]
    assert (history["stage"], history["definition"], history["model"]) == ("plan", "intent-check-plan", "liquid-d1")
    assert list(json.loads(history["classifier_input"])["questions"]) == ["aligned", "covers", "weakens"]
    assert log_rows(tmp_path) == []


def test_a_plan_serving_another_phase_fails_on_phase_fit(tmp_path, monkeypatch):
    checked = plan_check(tmp_path, monkeypatch, aligned=0.05, covers=0.1, weakens=0.1)
    assert checked["verdict"] == "fail"
    assert checked["reason"].startswith(
        "the plan may not serve the phase: it serves it at probability 0.05, under 0.3; the plan may leave out what "
        "the task asks: it covers it at probability 0.10, under 0.3; What would meet intent: Plan work that serves "
        "Swarm runtime fixes: Defects the running swarm hits., and covers Refuse revisionless events:"
    )
    assert Verdicts(SLUG, "intent", tmp_path).read(TASK)["criterion"] == "phase"
    assert [(row["kind"], row["reason"]) for row in log_rows(tmp_path)] == [("observe", checked["reason"])]


def test_a_fitting_plan_that_misses_the_task_fails_on_coverage_not_phase(tmp_path, monkeypatch):
    checked = plan_check(tmp_path, monkeypatch, mode="enforce", covers=0.2, weakens=0.1)
    assert checked["reason"].startswith("the plan may leave out what the task asks: it covers it at probability 0.20")
    assert Verdicts(SLUG, "intent", tmp_path).read(TASK)["criterion"] == "coverage"
    assert [row["kind"] for row in log_rows(tmp_path)] == ["deny"]


def test_a_weakening_plan_fails_on_weakens(tmp_path, monkeypatch):
    checked = plan_check(tmp_path, monkeypatch, weakens=0.8)
    assert checked["reason"].startswith("the planned work may weaken what the phase builds, at probability 0.80")
    assert Verdicts(SLUG, "intent", tmp_path).read(TASK)["criterion"] == "weakens"


def test_the_plan_chunk_is_judged_as_a_plan(tmp_path):
    state = {
        "stage": "plan",
        "task": "T",
        "task_text": "do",
        "phase": "P",
        "phase_intent": "use",
        "plan_lines": "1-1",
        "plan_chunk": "row\n",
    }
    answer = intent.judged(state, decide=answered(weakens=0.1, underdelivers=0.1, overdelivers=0.7))
    assert (answer["verdict"], answer["criterion"]) == ("fail", "chunk")
    assert answer["reason"] == (
        "the plan may add scope plan lines 1-1 do not ask for, at probability 0.70; What would meet intent: Plan work "
        'that serves P: use, and covers T: do. Remove the scope beyond plan lines 1-1, which ask only for line 1 "row".'
    )
    questions = intent.questions_for(state)
    assert list(questions) == ["aligned", "covers", "weakens", "underdelivers", "overdelivers", "misses_line_1"]
    assert questions["misses_line_1"].instructions == 'Does the plan leave out what plan line 1 asks for: "row"?'


def test_the_delivered_judge_asks_phase_fit_apart_from_delivery():
    questions = intent.questions_for({"task": "T"})
    assert list(questions) == ["usable", "aligned", "delivers", "reachable", "weakens"]
    assert questions["aligned"].instructions.startswith("Judge phase fit apart from completeness.")


def test_a_fitting_change_that_is_not_usable_yet_fails_on_delivery_not_phase():
    answer = intent.judged({}, decide=answered(usable=0.2, aligned=0.95, weakens=0.1))
    assert (answer["verdict"], answer["criterion"], answer["stage"]) == ("fail", "delivery", "delivered")
    assert answer["reason"] == (
        "the change fits the phase at probability 0.95, and the phase can use it as delivered at probability 0.20, "
        "under 0.3: a delivery shortfall, not a phase mismatch"
    )


def test_a_change_serving_another_phase_fails_on_phase():
    answer = intent.judged({}, decide=answered(usable=0.1, aligned=0.1, weakens=0.1))
    assert (answer["verdict"], answer["criterion"]) == ("fail", "phase")
    assert answer["reason"] == (
        "the change may not fit the phase: it fits at probability 0.10, under 0.3, and the phase can use it at "
        "probability 0.10"
    )


def test_a_usable_change_keeps_its_pass_whatever_the_phase_fit():
    answer = intent.judged({}, decide=answered(usable=0.9, aligned=0.1, weakens=0.1))
    assert (answer["verdict"], answer["criterion"]) == ("pass", None)


class Ledger:
    def update_task(self, slug, task_id, fields, by="swarm"):
        pass

    def comment(self, slug, task_id, text, by):
        pass


class Mail:
    master = "master"

    def engineer(self, task):
        return "seat"

    def send(self, key, address, text, ref=""):
        return []


def test_the_tick_records_the_failed_criterion_of_a_delivered_change(tmp_path):
    task = {**DOC["tasks"][0], "state": "pr", "pr_url": URL}
    doc = {**DOC, "tasks": [task]}
    view = {"title": "Refuse revisionless events", "body": "Closes 1", "files": ["events.py"]}

    def ask(state):
        return intent.judged(state, decide=answered(usable=0.2, aligned=0.95, weakens=0.1))

    intent.Check(SLUG, "observe", NOW, Ledger(), Mail(), lambda url: view, ask, home=tmp_path).run(doc)
    record = Verdicts(SLUG, "intent", tmp_path).read(TASK)
    assert (record["verdict"], record["criterion"], record["stage"], record["model"]) == (
        "fail",
        "delivery",
        "delivered",
        "liquid-d1",
    )
    [history] = [json.loads(line) for line in (tmp_path / SLUG / "gates" / "intent" / "history.jsonl").open()]
    assert (history["criterion"], history["definition"]) == ("delivery", "intent-check")


def test_the_plan_state_passes_through_the_history_bounds():
    state = intent_history.prepare(
        intent.plan_state(DOC, DOC["tasks"][0], [row for row in TRACED["pieces"] if row["kept"]])
    )
    assert state["stage"] == "plan"
    assert "pull_request_title" not in state


PLAN = {"stage": "plan", "task": "T", "task_text": "do", "phase": "P", "phase_intent": "use"}
CHUNK = {"plan_lines": "1-1", "plan_chunk": "row\n"}


def test_the_plan_fail_line_and_weaken_line_hold_at_their_exact_values():
    passed = intent.judged(PLAN, decide=answered(aligned=0.3, covers=0.3, weakens=0.49))
    assert (passed["verdict"], passed["criterion"]) == ("pass", None)
    weakened = intent.judged(PLAN, decide=answered(weakens=0.5))
    assert (weakened["verdict"], weakened["criterion"]) == ("fail", "weakens")


def test_a_passing_plan_cites_the_plan_lines_it_judged():
    answer = intent.judged({**PLAN, **CHUNK}, decide=answered(weakens=0.1, underdelivers=0.1, overdelivers=0.1))
    assert answer["reason"] == (
        "the plan serves the phase at probability 0.90 and covers the task at probability 0.90, "
        "judged against plan lines 1-1"
    )


def test_a_plan_missing_a_chunk_line_names_the_line_the_plan_leaves_out():
    decide = answered(weakens=0.1, underdelivers=0.7, overdelivers=0.1, misses_line_1=0.7)
    answer = intent.judged({**PLAN, **CHUNK}, decide=decide)
    assert answer["reason"].endswith('Deliver what plan lines 1-1 ask for and the plan leaves out: line 1 "row".')


def test_a_change_fitting_the_phase_exactly_at_the_line_fails_on_delivery():
    answer = intent.judged({}, decide=answered(usable=0.1, aligned=0.3, weakens=0.1))
    assert answer["criterion"] == "delivery"


def test_a_usable_change_that_fails_names_weakens_or_its_plan_chunk():
    assert intent.judged({}, decide=answered(usable=0.9, weakens=0.5))["criterion"] == "weakens"
    state = {"task": "T", "task_text": "do", "phase": "P", "phase_intent": "use", **CHUNK}
    decide = answered(usable=0.9, weakens=0.1, underdelivers=0.1, overdelivers=0.7)
    assert intent.judged(state, decide=decide)["criterion"] == "chunk"


def test_every_early_answer_names_its_stage(monkeypatch):
    assert intent.judged({"task_part": "tests-first"}) == {
        "verdict": "fail",
        "reason": "the tests first part requires a complete pull request diff to judge gate behaviour",
        "stage": "tests-first",
        "criterion": "tests-first",
    }
    assert intent.judged({"plan_lines": "1-2", "plan_chunk": None}) == {
        "verdict": "unchecked",
        "reason": "the plan chunk for lines 1-2 could not be read",
        "stage": "delivered",
    }

    def down(state, questions, purpose):
        raise ClassifierUnavailable("down")

    assert intent.judged({}, decide=down) == {
        "verdict": "unchecked",
        "reason": "the classifier did not answer",
        "stage": "delivered",
    }
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_INTENT_CHECK_FAIL", "x")
    assert intent.judged({}, decide=answered()) == {
        "verdict": "fail",
        "reason": "the intent definition is invalid: threshold fail must be between zero and one",
        "stage": "delivered",
    }


def test_a_tests_first_part_is_judged_whatever_its_plan_chunk():
    seen = []
    state = {"task_part": "tests-first", "pull_request_diff": "", "plan_lines": "1-2", "plan_chunk": None}
    answer = intent.judged(state, decide=answered(seen))
    assert (answer["verdict"], answer["criterion"], answer["stage"], len(seen)) == (
        "fail",
        "tests-first",
        "tests-first",
        1,
    )
