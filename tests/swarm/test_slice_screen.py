import pytest

from hooks.classifier import Answer, ClassifierInputError, ClassifierUnavailable, DecisionResult, core, decision_log
from scripts.swarm import slice_screen

PHASE = {"id": "p1", "title": "Build", "description": "Add the phase field parser."}


def task(tid, phase="p1", **fields):
    return {"id": tid, "phase": phase, "title": f"Task {tid}", "description": f"Work {tid}.", **fields}


def ledger(*tasks, ids=None):
    plan = {"id": "plan-p1", "phase": "p1", "kind": "plan", "lane": "plan"}
    plan["proof"] = {"slice": ",".join(ids if ids is not None else [t["id"] for t in tasks])}
    return {"overview": "Ship phase planning.", "phases": [PHASE], "tasks": [plan, *tasks]}


def size(score, confidence):
    return Answer(type="score", score=score, confidence=confidence)


def serves(p_yes):
    return Answer(type="noul", noul=p_yes)


def answered(*pairs):
    answers = {}
    for i, (s, v) in enumerate(pairs):
        answers[f"size_{i}"], answers[f"serves_{i}"] = s, v
    return answers


class Recorder:
    def __init__(self, answers=None, calibrated=True, source="pplx-decider-v1-27b", error=None):
        self.answers, self.calibrated, self.source, self.error = answers, calibrated, source, error
        self.calls = []

    def __call__(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        if self.error:
            raise self.error
        return DecisionResult(self.answers, self.source, calibrated=self.calibrated)


@pytest.mark.parametrize(
    ("score", "confidence", "flagged"),
    [(1.4, 0.95, False), (1.6, 0.7, True), (1.6, 0.69, False), (3.0, 0.9, True), (0.2, 0.99, False)],
)
def test_a_task_is_flagged_too_big_above_one_pull_request_at_the_confidence(score, confidence, flagged):
    found = slice_screen.flags(answered((size(score, confidence), serves(0.9))), [task("t1")], 0.7)
    assert bool(found) is flagged
    if flagged:
        level = slice_screen.SIZES[round(score)]
        assert found == [f"Classifier: task t1 may be too big, {level} at confidence {confidence:.2f}."]


@pytest.mark.parametrize(("p_yes", "flagged"), [(0.3, False), (0.29, True), (0.05, True), (0.9, False)])
def test_a_task_is_flagged_off_intent_below_three_tenths(p_yes, flagged):
    found = slice_screen.flags(answered((size(1.0, 0.9), serves(p_yes))), [task("t1")], 0.7)
    assert found == (
        [f"Classifier: task t1 may be off intent, serves the phase at probability {p_yes:.2f}."] if flagged else []
    )


def test_flags_name_each_task_by_its_own_answers():
    answers = answered((size(1.0, 0.9), serves(0.9)), (size(2.0, 0.8), serves(0.1)))
    found = slice_screen.flags(answers, [task("t1"), task("t2")], 0.7)
    assert [line.split(" may ")[0] for line in found] == ["Classifier: task t2"] * 2


def test_the_screen_asks_two_questions_per_slice_task_in_one_phase_slice_call(monkeypatch):
    fake = Recorder(answered((size(1.0, 0.9), serves(0.9)), (size(1.0, 0.9), serves(0.9))))
    monkeypatch.setattr(slice_screen, "decide", fake)
    doc = ledger(
        task("t1", kind="ci", territory=["scripts"]), task("t2"), task("t9", phase="p2"), ids=["t1", "t2", "t9", "t0"]
    )
    result = slice_screen.screen(PHASE, doc, 0.7)
    assert result == slice_screen.Screen()
    [(state, questions, kwargs)] = fake.calls
    assert kwargs == {"purpose": "phase-slice"}
    assert list(questions) == ["size_0", "serves_0", "size_1", "serves_1"]
    assert questions["size_1"].levels == ["trivial", "one pull request", "several pull requests", "a whole phase"]
    assert questions["size_1"].instructions == "How much work is task t2, titled Task t2?"
    assert questions["serves_0"].instructions == "Does task t1, titled Task t1, serve the phase intent?"
    assert (questions["serves_0"].true, questions["serves_0"].false) == (
        "it advances the phase",
        "it serves something else",
    )
    assert state == {
        "phase": "Build",
        "intent": "Add the phase field parser.",
        "overview": "Ship phase planning.",
        "tasks": [
            {"id": "t1", "title": "Task t1", "description": "Work t1.", "kind": "ci", "territory": ["scripts"]},
            {"id": "t2", "title": "Task t2", "description": "Work t2.", "kind": "code", "territory": []},
        ],
    }


def test_flags_from_a_calibrated_answer_carry_no_reason(monkeypatch):
    monkeypatch.setattr(slice_screen, "decide", Recorder(answered((size(2.0, 0.9), serves(0.9)))))
    result = slice_screen.screen(PHASE, ledger(task("t1")), 0.7)
    assert result.reason == "" and len(result.flags) == 1


def test_a_fallback_answer_is_never_clean(monkeypatch):
    fake = Recorder(answered((size(1.0, 0.9), serves(0.9))), calibrated=False, source="haiku")
    monkeypatch.setattr(slice_screen, "decide", fake)
    result = slice_screen.screen(PHASE, ledger(task("t1")), 0.7)
    assert result == slice_screen.Screen(reason="the answer came from the fallback haiku")
    assert slice_screen.hold([], result) == "the answer came from the fallback haiku"


@pytest.mark.parametrize("error", [ClassifierUnavailable("no decision backend answered"), ClassifierInputError("bad")])
def test_a_classifier_error_leaves_the_slice_to_the_master_with_the_reason(monkeypatch, error):
    monkeypatch.setattr(slice_screen, "decide", Recorder(error=error))
    result = slice_screen.screen(PHASE, ledger(task("t1")), 0.7)
    assert result == slice_screen.Screen(reason="the classifier did not answer")


def test_hold_names_the_first_thing_that_stops_auto_approval():
    flagged = slice_screen.Screen(flags=("x",), reason="the answer came from the fallback haiku")
    assert slice_screen.hold(["problem"], flagged) == "the slice check found problems"
    assert slice_screen.hold([], flagged) == "the classifier flagged a task"
    assert slice_screen.hold([], slice_screen.Screen()) == ""


def test_the_decision_log_line_carries_phase_slice(monkeypatch, tmp_path):
    class Fake:
        name = "fake"

        def decide(self, request):
            return DecisionResult(answered((size(1.0, 0.9), serves(0.9))), "fake", calibrated=False)

    monkeypatch.setattr(decision_log, "log_path", lambda: tmp_path / "decisions.jsonl")
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL", raising=False)
    monkeypatch.setattr(core, "cli_backends", lambda harness=None: [Fake()])
    monkeypatch.setattr(slice_screen, "decide", core.decide)
    slice_screen.screen(PHASE, ledger(task("t1")), 0.7)
    [entry] = decision_log.read("phase-slice")
    assert (entry["source"], entry["calibrated"]) == ("fake", False)
