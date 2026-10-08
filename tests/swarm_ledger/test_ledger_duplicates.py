from hooks import classifier
from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.swarm_ledger import ledger_duplicates
from scripts.swarm_ledger.ledger_duplicates import PURPOSE, Match, find


def ledger():
    return {
        "phases": [
            {"id": "p1", "title": "Release automation", "description": "Ship releases from CI", "done": False},
            {"id": "p2", "title": "Ledger page", "description": "Page layout", "done": True},
        ],
        "tasks": [
            {
                "id": "t1",
                "title": "Publish the wheel to PyPI from main",
                "description": "Run the publish workflow on main after the release tag",
                "state": "open",
                "rank": "low",
                "phase": "p1",
            },
            {
                "id": "t2",
                "title": "Fold the chat panel on header click",
                "description": "The chat panel folds like every other section",
                "state": "done",
                "phase": "p2",
            },
            {
                "id": "t3",
                "title": "Dropped publish idea",
                "description": "publish wheel pypi main",
                "state": "open",
                "out_of_scope": True,
            },
        ],
        "followups": [
            {"id": "f1", "text": "Seed the Sonar caches from main so queued scans reuse plugins", "done": False},
            {"id": "f2", "text": "Retire the hour long leak sweep trigger", "done": True},
        ],
    }


class Judge:
    def __init__(self, yes=(), error=None, probability=0.9):
        self.yes, self.error, self.probability, self.asked = set(yes), error, probability, []

    def __call__(self, state, questions, *, purpose):
        self.asked.append((state, questions, purpose))
        if self.error:
            raise self.error
        answers = {}
        for name, question in questions.items():
            hit = any(f" {item} " in question.instructions for item in self.yes)
            answers[name] = Answer("noul", noul=self.probability if hit else 0.05)
        return DecisionResult(answers, "unit-test")


def test_a_reworded_repeat_is_found_with_its_rank_and_phase():
    judge = Judge(yes={"t1"})
    new = {"title": "Upload the package wheel to PyPI on main", "description": "after the release tag is cut"}

    assert find(ledger(), "task", [new], judge=judge) == [
        Match("t1", "task", "Publish the wheel to PyPI from main", "open", "low", "p1", "Release automation", 0.9)
    ]
    state, questions, purpose = judge.asked[0]
    assert purpose == PURPOSE == "ledger-duplicate"
    assert [e["id"] for e in state["existing"]] == ["t1"]
    assert state["new"] == [{"ref": 0, "title": new["title"], "description": new["description"]}]
    assert list(questions) == ["new_0_existing_0"]
    assert questions["new_0_existing_0"].instructions == (
        "Does new item 0 ask for the same change as task t1 titled Publish the wheel to PyPI from main?"
    )
    assert questions["new_0_existing_0"].true == "the new item asks for the same change as the existing item"
    assert questions["new_0_existing_0"].false == "the new item asks for a different change"


def test_a_distinct_item_passes():
    judge = Judge()
    new = {"title": "Publish nightly docs", "description": "Build the docs site every night"}

    assert find(ledger(), "task", [new], judge=judge) == [None]
    assert len(judge.asked) == 1


def test_an_item_sharing_no_words_never_reaches_the_classifier():
    judge = Judge(yes={"t1"})

    assert find(ledger(), "task", [{"title": "Zebra xylophone", "description": ""}], judge=judge) == [None]
    assert judge.asked == []


def test_a_done_task_is_reported_as_built():
    match = find(
        ledger(), "task", [{"title": "Fold the chat panel when its header is clicked"}], judge=Judge(yes={"t2"})
    )[0]

    assert match.id == "t2" and match.state == "done" and match.built
    assert match.rank == "normal" and match.phase_title == "Ledger page"


def test_an_open_task_is_not_built():
    match = find(ledger(), "task", [{"title": "Publish the wheel to PyPI"}], judge=Judge(yes={"t1"}))[0]

    assert not match.built


def test_a_classifier_error_returns_unchecked():
    judge = Judge(error=ClassifierUnavailable("down"))
    items = [{"title": "Publish the wheel to PyPI"}, {"title": "Zebra xylophone"}]

    assert find(ledger(), "task", items, judge=judge) == ["unchecked", None]


def test_the_shortlist_caps_what_is_sent():
    doc = {
        "phases": [],
        "tasks": [
            {"id": f"t{n}", "title": f"Publish the wheel to PyPI variant{n}", "state": "open"} for n in range(10)
        ],
        "followups": [],
    }
    judge = Judge()

    find(doc, "task", [{"title": "Publish the wheel to PyPI"}, {"title": "Publish wheel"}], judge=judge)

    state, questions, _ = judge.asked[0]
    assert len(judge.asked) == 1
    assert len(questions) == 8
    assert [e["id"] for e in state["existing"][:4]] == ["t0", "t1", "t2", "t3"]


def test_the_shortlist_ranks_by_shared_words_over_the_size_of_both_sets():
    tasks = [
        ("wide", "Publish wheel alpha bravo charlie delta echo foxtrot"),
        ("pair", "Publish wheel"),
        ("near", "Publish wheel pypi golf"),
        ("none", "Unrelated hotel"),
    ]
    doc = {"tasks": [{"id": i, "title": t, "state": "open"} for i, t in tasks]}
    judge = Judge()

    find(doc, "task", [{"title": "Publish wheel pypi"}], judge=judge)

    assert [e["id"] for e in judge.asked[0][0]["existing"]] == ["near", "pair", "wide"]


def test_an_item_of_only_short_or_common_words_never_reaches_the_classifier():
    judge = Judge(yes={"t1"})

    assert find(ledger(), "task", [{"title": "Add the new task to it", "description": "for all"}], judge=judge) == [
        None
    ]
    assert judge.asked == []


def test_a_batch_past_the_classifier_question_limit_is_unchecked_not_an_error():
    doc = {"phases": [], "tasks": [{"id": f"t{n}", "title": f"Publish wheel {n}", "state": "open"} for n in range(4)]}
    items = [{"title": "Publish wheel"}] * 33

    assert find(doc, "task", items, judge=classifier.decide) == ["unchecked"] * 33


def test_the_best_confirmed_candidate_wins():
    doc = ledger()
    doc["tasks"].append({"id": "t4", "title": "Publish the wheel to PyPI", "state": "pr", "rank": "high"})

    class Split(Judge):
        def __call__(self, state, questions, *, purpose):
            answers = {n: Answer("noul", noul=0.7 if "t1" in q.instructions else 0.95) for n, q in questions.items()}
            return DecisionResult(answers, "unit-test")

    match = find(doc, "task", [{"title": "Publish the wheel to PyPI from main"}], judge=Split())[0]

    assert (match.id, match.probability, match.phase, match.phase_title) == ("t4", 0.95, None, None)


def test_a_probability_at_the_threshold_or_missing_is_no_match():
    assert find(
        ledger(), "task", [{"title": "Publish the wheel to PyPI"}], judge=Judge(yes={"t1"}, probability=0.6)
    ) == [None]
    assert find(
        ledger(), "task", [{"title": "Publish the wheel to PyPI"}], judge=Judge(yes={"t1"}, probability=0.61)
    ) == [Match("t1", "task", "Publish the wheel to PyPI from main", "open", "low", "p1", "Release automation", 0.61)]
    assert find(
        ledger(), "task", [{"title": "Publish the wheel to PyPI"}], judge=Judge(yes={"t1"}, probability=True)
    ) == [None]
    assert find(
        ledger(), "task", [{"title": "Publish the wheel to PyPI"}], judge=Judge(yes={"t1"}, probability=None)
    ) == [None]


def test_a_follow_up_is_compared_with_follow_ups_and_open_tasks():
    judge = Judge(yes={"f1"})
    new = {"text": "Seed Sonar caches from main for the queued scans, the wheel publish on PyPI and the chat panel"}

    match = find(ledger(), "followup", [new], judge=judge)[0]

    assert match == Match(
        "f1", "followup", "Seed the Sonar caches from main so queued scans reuse plugins", "open", None, None, None, 0.9
    )
    kinds = {(e["kind"], e["id"]) for e in judge.asked[0][0]["existing"]}
    assert ("task", "t1") in kinds and ("task", "t2") not in kinds and ("task", "t3") not in kinds


def test_a_done_follow_up_reports_done():
    match = find(ledger(), "followup", [{"text": "Retire the leak sweep trigger"}], judge=Judge(yes={"f2"}))[0]

    assert (match.state, match.built) == ("done", True)


def test_a_phase_is_compared_with_phases():
    judge = Judge(yes={"p1"})

    match = find(ledger(), "phase", [{"title": "Release automation for CI"}], judge=judge)[0]

    assert (match.id, match.kind, match.state, match.title) == ("p1", "phase", "open", "Release automation")
    assert {e["kind"] for e in judge.asked[0][0]["existing"]} == {"phase"}


def test_the_default_judge_is_the_classifier(monkeypatch):
    judge = Judge(yes={"t1"})
    monkeypatch.setattr(ledger_duplicates, "decide", judge)

    assert find(ledger(), "task", [{"title": "Publish the wheel to PyPI"}])[0].id == "t1"


def test_the_unit_suite_keeps_the_classifier_offline():
    assert find(ledger(), "task", [{"title": "Publish the wheel to PyPI"}]) == ["unchecked"]


def test_words_drop_short_and_common_words():
    item = {"title": "Add the new CI api task", "description": "for PyPI", "text": "x"}

    assert ledger_duplicates.words(item) == {"api", "pypi"}
