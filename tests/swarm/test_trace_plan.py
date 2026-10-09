import hashlib
import json
import time
from pathlib import Path

import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.gates import Who
from scripts.gates import log as gate_log
from scripts.swarm import trace_plan

pytestmark = pytest.mark.xdist_group("fakeredis")

DOGHOUSE = (
    "# Plan\n\n"
    "Pieces of the doghouse task.\n"
    "- walls and roof | doghouse/frame, doghouse/roof | the house needs a shell\n"
    "- a light over the door | doghouse/light | the dog sleeps there at night\n"
    "- a diesel generator | power/generator | it powers the light\n"
)
INTENT = {"project intent": "Keep the dog warm.", "phase": "Build", "phase intent": "Build the doghouse."}
WHO = Who(name="engineer@1-1", swarm="sw", task="t1")


def piece(what, *areas, why="the task needs it"):
    return trace_plan.Piece(what, areas or ("src",), why)


def needed(p_yes):
    return Answer(type="noul", noul=p_yes)


def size(score, confidence):
    return Answer(type="score", score=score, confidence=confidence)


class Recorder:
    def __init__(self, answers=None, error=None, source="pplx-decider-v1-27b", calibrated=True):
        self.answers, self.error, self.source, self.calibrated = answers, error, source, calibrated
        self.calls = []

    def __call__(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        if self.error:
            raise self.error
        return DecisionResult(self.answers, self.source, calibrated=self.calibrated)


def answers(*p_yes, score=1.0, confidence=0.9, start=0):
    found = {f"piece_{start + i}": needed(p) for i, p in enumerate(p_yes)}
    if score is not None:
        found["size"] = size(score, confidence)
    return found


@pytest.fixture
def ask(monkeypatch):
    def install(*p_yes, **kwargs):
        error = kwargs.pop("error", None)
        fake = Recorder(None if error else answers(*p_yes, **kwargs), error=error)
        monkeypatch.setattr(trace_plan, "decide", fake)
        return fake

    return install


# parse


def test_parse_reads_one_piece_per_dash_line_and_skips_prose():
    pieces = trace_plan.parse(DOGHOUSE, "t1")
    assert pieces == [
        piece("walls and roof", "doghouse/frame", "doghouse/roof", why="the house needs a shell"),
        piece("a light over the door", "doghouse/light", why="the dog sleeps there at night"),
        piece("a diesel generator", "power/generator", why="it powers the light"),
    ]
    assert pieces[0].key == "walls and roof | doghouse/frame, doghouse/roof | the house needs a shell"


def test_parse_drops_empty_areas_and_trims_spaces():
    [found] = trace_plan.parse("-   a light   |  a , ,b  |  why not  ", "t1")
    assert found == piece("a light", "a", "b", why="why not")


@pytest.mark.parametrize(
    ("line", "number"),
    [
        ("- only what", 1),
        ("- what | areas", 1),
        ("- what | a | why | extra", 1),
        ("- | a | why", 1),
        ("- what | , | why", 1),
        ("- what | a |  ", 1),
        ("- fine | a | ok\n- what | a", 2),
    ],
)
def test_parse_refuses_a_malformed_piece_line_by_number(line, number):
    with pytest.raises(ValueError) as refused:
        trace_plan.parse(line, "t1")
    assert str(refused.value) == f"plan line {number} is not a piece; write {trace_plan.FORMAT}"


def test_parse_refuses_a_plan_without_pieces():
    with pytest.raises(ValueError, match=r"^the plan holds no pieces; write one piece per line"):
        trace_plan.parse("# Plan\n\nnothing here\n", "t1")


def test_parse_refuses_a_piece_the_ledger_would_refuse_as_a_follow_up():
    with pytest.raises(ValueError) as refused:
        trace_plan.parse("- edit scripts/swarm/cli.py | scripts | it wires the command", "t1")
    assert str(refused.value).startswith(
        "plan line 1: the ledger would refuse its follow up, write what in plain words: "
    )
    assert "file name or path" in str(refused.value)


def test_parse_refuses_a_task_id_the_ledger_would_refuse_in_a_follow_up():
    with pytest.raises(ValueError, match="^plan line 1: the ledger would refuse its follow up"):
        trace_plan.parse("- a light | a | b", "my_task")


def test_parse_names_every_problem_of_a_piece():
    with pytest.raises(ValueError) as refused:
        trace_plan.parse("- fix plan_hash in cli.py | a | b", "t1")
    assert str(refused.value).endswith(": file name or path 'cli.py'; code identifier 'plan_hash'")


def test_parse_caps_the_number_of_pieces():
    allowed = "".join(f"- piece {i} | a | b\n" for i in range(trace_plan.MAX_PIECES))
    assert len(trace_plan.parse(allowed, "t1")) == trace_plan.MAX_PIECES
    with pytest.raises(ValueError, match=f"^the plan holds {trace_plan.MAX_PIECES + 1} pieces, at most 100$"):
        trace_plan.parse(allowed + "- one more | a | b\n", "t1")


def test_followup_text_names_the_task_and_the_piece():
    assert trace_plan.followup_text("t1", "a generator") == "Cut from the plan of task t1: a generator"


def test_plan_hash_changes_with_any_field_and_ignores_nothing():
    base = [piece("a", "x", why="w")]
    digest = trace_plan.plan_hash(base)
    assert digest == hashlib.sha256(b"a | x | w").hexdigest()
    pair = trace_plan.plan_hash([piece("a", "x", why="w"), piece("b", "y", "z", why="v")])
    assert pair == hashlib.sha256(b"a | x | w\nb | y, z | v").hexdigest()
    for other in ([piece("b", "x", why="w")], [piece("a", "y", why="w")], [piece("a", "x", why="v")], base * 2):
        assert trace_plan.plan_hash(other) != digest


def test_intent_reads_project_phase_and_task():
    doc = {
        "overview": "Keep the dog warm.",
        "phases": [{"id": "p0", "title": "Other"}, {"id": "p1", "title": "Build", "description": "Build it."}],
    }
    doc["tasks"] = [
        {"id": "t0", "phase": "p0", "title": "Other"},
        {"id": "t1", "phase": "p1", "title": "Doghouse", "description": "A house for the dog."},
    ]
    assert trace_plan.intent(doc, "t1") == {
        "project intent": "Keep the dog warm.",
        "phase": "Build",
        "phase intent": "Build it.",
        "task": "Doghouse",
        "task intent": "A house for the dog.",
        "task ids": ["t0", "t1"],
    }
    blank = dict.fromkeys(("project intent", "phase", "phase intent", "task", "task intent"), "")
    assert trace_plan.intent({}, "t1") == {**blank, "task ids": []}
    assert trace_plan.intent(doc, "t9") == {**blank, "project intent": "Keep the dog warm.", "task ids": ["t0", "t1"]}


# trace


def test_trace_asks_one_needed_question_per_piece_and_one_size_question(ask):
    fake = ask(0.9, 0.8, 0.1)
    pieces = trace_plan.parse(DOGHOUSE, "t1")
    record = trace_plan.trace(pieces, INTENT, None, now_ms=5)
    [(state, questions, kwargs)] = fake.calls
    assert kwargs == {"purpose": "trace-plan"}
    assert state == {
        **INTENT,
        "pieces": [
            {
                "piece": 1,
                "what": "walls and roof",
                "areas": ["doghouse/frame", "doghouse/roof"],
                "why": "the house needs a shell",
            },
            {
                "piece": 2,
                "what": "a light over the door",
                "areas": ["doghouse/light"],
                "why": "the dog sleeps there at night",
            },
            {"piece": 3, "what": "a diesel generator", "areas": ["power/generator"], "why": "it powers the light"},
        ],
    }
    assert list(questions) == ["piece_0", "piece_1", "piece_2", "size"]
    assert questions["piece_2"].instructions == "Is piece 3, a diesel generator, needed to deliver the task?"
    assert (questions["piece_2"].true, questions["piece_2"].false) == ("the task needs it", "it serves something else")
    assert questions["size"].instructions == (
        "How much source work is the plan? Judge only pieces with nonempty areas; "
        "test only pieces and the shared mutation clearance file are supporting proof, not additional scope."
    )
    assert questions["size"].levels == ["trivial", "one pull request", "several pull requests", "a whole phase"]
    assert record == {
        "verdict": "pass",
        "plan_hash": trace_plan.plan_hash(pieces),
        "pieces": [
            {
                "what": "walls and roof",
                "areas": ["doghouse/frame", "doghouse/roof"],
                "why": "the house needs a shell",
                "probability": 0.9,
                "kept": True,
            },
            {
                "what": "a light over the door",
                "areas": ["doghouse/light"],
                "why": "the dog sleeps there at night",
                "probability": 0.8,
                "kept": True,
            },
            {
                "what": "a diesel generator",
                "areas": ["power/generator"],
                "why": "it powers the light",
                "probability": 0.1,
                "kept": False,
            },
        ],
        "size": {"level": 1, "name": "one pull request", "confidence": 0.9},
        "reasons": [],
        "source": "pplx-decider-v1-27b",
        "calibrated": True,
        "failures": 0,
        "filed": [],
        "at": 5,
    }


@pytest.mark.parametrize(
    "areas, expected",
    [
        (("tests", "tests/fixtures/profile", "./tests/swarm", "mutation-cleared.txt"), []),
        (
            ("./scripts/init_agent.py", "tests/test_profile_binding.py", "./mutation-cleared.txt"),
            ["./scripts/init_agent.py"],
        ),
        (
            ("tests_extra/module.py", "scripts/tests.py", "scripts/mutation-cleared.txt"),
            ["tests_extra/module.py", "scripts/tests.py", "scripts/mutation-cleared.txt"],
        ),
    ],
)
def test_scope_input_excludes_only_shared_proof_areas(ask, areas, expected):
    fake = ask(1.0)
    pieces = [trace_plan.Piece("proof backed source change", areas, "deliver the task")]
    record = trace_plan.trace(pieces, INTENT, None, now_ms=5)
    [(state, questions, _)] = fake.calls
    assert state["pieces"][0]["areas"] == expected
    assert questions["size"].instructions == (
        "How much source work is the plan? Judge only pieces with nonempty areas; "
        "test only pieces and the shared mutation clearance file are supporting proof, not additional scope."
    )
    assert record["pieces"][0]["areas"] == list(areas)
    assert record["pieces"][0]["kept"] is True


@pytest.mark.parametrize("name", ["versioned_resources", "incremental_observations"])
def test_observed_plans_keep_source_scope_and_original_proof_areas(monkeypatch, name):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" / "plan_scope" / f"{name}.json").read_text())
    pieces = trace_plan.parse(fixture["plan"], fixture["observed_task"])
    calls = []

    def classify(state, questions, **kwargs):
        calls.append(state)
        source_areas = [area for p in state["pieces"] for area in p["areas"]]
        assert not any(area.startswith("tests/") or area == "mutation-cleared.txt" for area in source_areas)
        return DecisionResult(answers(*([1.0] * len(pieces))), "scope-control", calibrated=True)

    monkeypatch.setattr(trace_plan, "decide", classify)
    record = trace_plan.trace(pieces, fixture["intent"], None, now_ms=5)
    assert record["verdict"] == "pass"
    assert record["size"]["name"] == "one pull request"
    assert [row["areas"] for row in record["pieces"]] == [list(p.areas) for p in pieces]
    assert any("mutation-cleared.txt" in row["areas"] for row in record["pieces"])
    assert [[p["what"], p["why"]] for p in calls[0]["pieces"]] == [[p.what, p.why] for p in pieces]


def test_oversized_source_plan_still_fails_with_all_source_areas_visible(monkeypatch):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" / "plan_scope" / "oversized.json").read_text())
    pieces = trace_plan.parse(fixture["plan"], fixture["observed_task"])

    def classify(state, questions, **kwargs):
        assert [area for p in state["pieces"] for area in p["areas"]] == [
            "services/billing",
            "services/identity",
            "apps/dashboard",
            "infrastructure/database",
        ]
        return DecisionResult(answers(1.0, 1.0, 1.0, 1.0, score=3.0), "scope-control", calibrated=True)

    monkeypatch.setattr(trace_plan, "decide", classify)
    record = trace_plan.trace(pieces, fixture["intent"], None, now_ms=5)
    assert record["verdict"] == "fail"
    assert record["reasons"] == ["the plan is sized a whole phase at confidence 0.90, above one pull request"]


@pytest.mark.parametrize(
    ("areas", "kept"),
    [
        (("mutation-cleared.txt",), True),
        (("./mutation-cleared.txt",), True),
        (("mutation-clearances",), True),
        (("./mutation-clearances/ruling.json",), True),
        (("mutation-clearances.bak/ruling.json",), False),
        (("scripts/mutation-clearances/ruling.json",), False),
        (("mutation-cleared.txt", "power/generator"), False),
        (("scripts/mutation-cleared.txt",), False),
        (("power/generator",), False),
    ],
)
def test_a_clearance_ruling_piece_is_kept_whatever_the_classifier_says(ask, areas, kept):
    ask(0.0, 0.9, 0.9)
    pieces = [piece("append the rulings", *areas), piece("b"), piece("c")]
    record = trace_plan.trace(pieces, INTENT, None)
    assert record["pieces"][0]["kept"] is kept
    assert record["verdict"] == "pass"


def test_clearance_pieces_never_count_as_cut_toward_a_failed_plan(ask):
    ask(0.0, 0.0, 0.9)
    pieces = [piece("append the rulings", "mutation-cleared.txt"), piece("b"), piece("c")]
    record = trace_plan.trace(pieces, INTENT, None)
    assert record["verdict"] == "pass"
    assert [row["kept"] for row in record["pieces"]] == [True, False, True]


@pytest.mark.parametrize(("p_yes", "kept"), [(0.3, True), (0.29, False), (0.0, False), (1.0, True)])
def test_a_piece_under_three_tenths_is_cut(ask, p_yes, kept):
    ask(p_yes, 0.9, 0.9)
    record = trace_plan.trace([piece("a"), piece("b"), piece("c")], INTENT, None)
    assert record["pieces"][0]["kept"] is kept
    assert record["verdict"] == "pass"


@pytest.mark.parametrize(
    ("p_yes", "verdict"),
    [((0.1, 0.9), "pass"), ((0.1, 0.1, 0.9), "fail"), ((0.1, 0.1, 0.9, 0.9), "pass"), ((0.1, 0.1, 0.1, 0.9), "fail")],
)
def test_a_plan_with_more_than_half_its_pieces_cut_fails_whole(ask, p_yes, verdict):
    ask(*p_yes)
    pieces = [piece(f"piece {i}") for i in range(len(p_yes))]
    record = trace_plan.trace(pieces, INTENT, None)
    assert record["verdict"] == verdict
    cut = sum(1 for p in p_yes if p < 0.3)
    expected = [f"{cut} of {len(p_yes)} pieces are off the task intent, more than half"] if verdict == "fail" else []
    assert record["reasons"] == expected
    assert record["failures"] == (verdict == "fail")


@pytest.mark.parametrize(
    ("score", "confidence", "fails"),
    [(1.4, 0.99, False), (1.6, 0.7, True), (1.6, 0.69, False), (3.0, 0.95, True), (2.0, 1.0, True)],
)
def test_a_plan_sized_above_one_pull_request_at_confidence_fails_whole(ask, score, confidence, fails):
    ask(0.9, score=score, confidence=confidence)
    record = trace_plan.trace([piece("a")], INTENT, None)
    level = round(score)
    assert record["size"] == {"level": level, "name": trace_plan.SIZES[level], "confidence": confidence}
    assert record["verdict"] == ("fail" if fails else "pass")
    if fails:
        name = trace_plan.SIZES[level]
        assert record["reasons"] == [f"the plan is sized {name} at confidence {confidence:.2f}, above one pull request"]


def test_both_failure_reasons_are_given_together(ask):
    ask(0.1, score=3.0, confidence=0.9)
    record = trace_plan.trace([piece("a")], INTENT, None)
    assert record["reasons"] == [
        "1 of 1 pieces are off the task intent, more than half",
        "the plan is sized a whole phase at confidence 0.90, above one pull request",
    ]


def test_failures_add_up_across_plans_and_filed_pieces_carry(ask):
    ask(0.1)
    previous = {"verdict": "fail", "plan_hash": "old", "failures": 1, "filed": ["x"], "pieces": []}
    record = trace_plan.trace([piece("a")], INTENT, previous)
    assert (record["verdict"], record["failures"], record["filed"]) == ("fail", 2, ["x"])
    ask(0.9)
    record = trace_plan.trace([piece("a")], INTENT, previous)
    assert (record["verdict"], record["failures"]) == ("pass", 1)


def test_no_classifier_answer_is_unchecked_with_every_piece_kept(ask):
    ask(error=ClassifierUnavailable("down"))
    previous = {"verdict": "fail", "plan_hash": "old", "failures": 1, "filed": ["x"], "pieces": []}
    record = trace_plan.trace([piece("a"), piece("b")], INTENT, previous, now_ms=7)
    assert record == {
        "verdict": "unchecked",
        "plan_hash": trace_plan.plan_hash([piece("a"), piece("b")]),
        "pieces": [
            {"what": "a", "areas": ["src"], "why": "the task needs it", "probability": None, "kept": True},
            {"what": "b", "areas": ["src"], "why": "the task needs it", "probability": None, "kept": True},
        ],
        "size": None,
        "reasons": ["the classifier did not answer"],
        "source": "",
        "calibrated": False,
        "failures": 1,
        "filed": ["x"],
        "at": 7,
    }


def test_an_uncalibrated_answer_records_its_source(monkeypatch):
    fake = Recorder(answers(0.9), source="claude-haiku", calibrated=False)
    monkeypatch.setattr(trace_plan, "decide", fake)
    record = trace_plan.trace([piece("a")], INTENT, None)
    assert (record["source"], record["calibrated"]) == ("claude-haiku", False)


def test_a_piece_appended_to_a_passing_plan_is_traced_alone(ask):
    first = [piece("walls"), piece("a light")]
    ask(0.9, 0.9)
    passed = trace_plan.trace(first, INTENT, None)
    passed["filed"] = ["k"]
    fake = ask(0.2, score=None, start=2)
    fake.source, fake.calibrated = "claude-haiku", False
    later = trace_plan.trace([*first, piece("a generator")], INTENT, passed, now_ms=9)
    assert (later["source"], later["calibrated"], later["at"]) == ("claude-haiku", False, 9)
    [(state, questions, _)] = fake.calls
    assert state["pieces"] == [{"piece": 3, "what": "a generator", "areas": ["src"], "why": "the task needs it"}]
    assert list(questions) == ["piece_2"]
    assert later["pieces"][:2] == passed["pieces"]
    assert later["pieces"][2] == {
        "what": "a generator",
        "areas": ["src"],
        "why": "the task needs it",
        "probability": 0.2,
        "kept": False,
    }
    assert (later["verdict"], later["size"], later["reasons"], later["filed"]) == ("pass", passed["size"], [], ["k"])
    assert later["plan_hash"] == trace_plan.plan_hash([*first, piece("a generator")])


def test_an_appended_piece_with_no_answer_leaves_the_plan_unchecked(ask):
    ask(0.9)
    passed = trace_plan.trace([piece("walls")], INTENT, None)
    ask(error=ClassifierUnavailable("down"))
    later = trace_plan.trace([piece("walls"), piece("light")], INTENT, passed)
    assert (later["verdict"], later["size"]) == ("unchecked", passed["size"])
    assert later["pieces"] == [passed["pieces"][0], {**passed["pieces"][0], "what": "light", "probability": None}]


def test_a_trace_without_a_clock_stamps_the_time_in_milliseconds(ask):
    ask(0.9)
    before = int(time.time() * 1000)
    record = trace_plan.trace([piece("a")], INTENT, None)
    assert before <= record["at"] <= int(time.time() * 1000)


@pytest.mark.parametrize(
    "previous",
    [
        {"verdict": "fail", "pieces": [{"what": "walls", "areas": ["src"], "why": "the task needs it"}]},
        {"verdict": "unchecked", "pieces": [{"what": "walls", "areas": ["src"], "why": "the task needs it"}]},
        {"verdict": "pass", "pieces": [{"what": "roof", "areas": ["src"], "why": "the task needs it"}]},
        {"verdict": "pass", "pieces": [{"what": "walls", "areas": ["src"], "why": "the task needs it"}] * 2},
        {"verdict": "pass", "pieces": [{"what": "walls", "areas": ["src"], "why": "the task needs it"}] * 3},
        {
            "verdict": "pass",
            "pieces": [
                {"what": "walls", "areas": ["src"], "why": "the task needs it"},
                {"what": "light", "areas": ["src"], "why": "the task needs it"},
            ],
        },
    ],
)
def test_a_changed_failed_or_unchecked_plan_is_traced_whole(ask, previous):
    fake = ask(0.9, 0.9)
    trace_plan.trace([piece("walls"), piece("light")], INTENT, {**previous, "plan_hash": "old"})
    [(state, questions, _)] = fake.calls
    assert len(state["pieces"]) == 2 and "size" in questions


# run


class Ledger:
    def __init__(self, refuse=False):
        self.followups, self.refuse = [], refuse

    def followup(self, slug, text):
        assert slug == "sw"
        self.followups.append(text)


def write_plan(folder, text=DOGHOUSE):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plan.md").write_text(text)


def rows(home):
    return gate_log.recent("sw", 100, home)


def run(folder, ledger, home, mode="observe", now_ms=11):
    return trace_plan.run(folder, INTENT, ledger, WHO, mode, home=home, now_ms=now_ms)


def test_run_files_each_cut_piece_once_and_writes_the_verdict(ask, tmp_path):
    folder, home, ledger = tmp_path / "t1", tmp_path / "home", Ledger()
    write_plan(folder)
    ask(0.9, 0.8, 0.1)
    record, block = run(folder, ledger, home)
    assert (block, record["at"]) == (False, 11)
    assert ledger.followups == ["Cut from the plan of task t1: a diesel generator"]
    assert record["filed"] == ["a diesel generator | power/generator | it powers the light"]
    assert json.loads((folder / "plan-verdict.json").read_text()) == record
    [row] = rows(home)
    assert {k: row[k] for k in ("gate", "kind", "agent", "task", "reason")} == {
        "gate": "trace-plan",
        "kind": "count",
        "agent": "engineer@1-1",
        "task": "t1",
        "reason": "cut: a diesel generator",
    }
    write_plan(folder, DOGHOUSE + "- a dog bed | doghouse/bed | the dog sleeps on it\n")
    fake = ask(0.9, score=None, start=3)
    record, _ = run(folder, ledger, home)
    assert len(fake.calls) == 1
    assert ledger.followups == ["Cut from the plan of task t1: a diesel generator"]
    assert len(record["pieces"]) == 4 and len(rows(home)) == 1


def test_run_saves_the_filed_cut_before_its_follow_up(ask, tmp_path):
    from scripts.swarm.store import SwarmError

    class Failing(Ledger):
        def followup(self, slug, text):
            raise SwarmError("ledger sw: connection reset")

    folder, home = tmp_path / "t1", tmp_path / "home"
    write_plan(folder)
    ask(0.9, 0.8, 0.1)
    with pytest.raises(SwarmError):
        run(folder, Failing(), home)
    saved = json.loads((folder / "plan-verdict.json").read_text())
    assert saved["filed"] == ["a diesel generator | power/generator | it powers the light"]


def test_a_later_cut_is_filed_beside_the_earlier_one(ask, tmp_path):
    folder, home, ledger = tmp_path / "t1", tmp_path / "home", Ledger()
    write_plan(folder)
    ask(0.9, 0.8, 0.1)
    run(folder, ledger, home)
    write_plan(folder, DOGHOUSE + "- a dog bed | doghouse/bed | the dog sleeps on it\n")
    ask(0.1, score=None, start=3)
    record, _ = run(folder, ledger, home)
    assert record["filed"] == [
        "a diesel generator | power/generator | it powers the light",
        "a dog bed | doghouse/bed | the dog sleeps on it",
    ]


def test_run_on_an_unchanged_plan_asks_nothing_and_writes_nothing(ask, tmp_path):
    folder, home, ledger = tmp_path / "t1", tmp_path / "home", Ledger()
    write_plan(folder)
    ask(0.1, 0.1, 0.9)
    first, _ = run(folder, ledger, home)
    fake = ask(0.9, 0.9, 0.9)
    again, block = run(folder, ledger, home, now_ms=99)
    assert (again, block, fake.calls) == (first, False, [])
    assert again["failures"] == 1 and len(rows(home)) == 1


def test_run_files_nothing_for_a_failed_plan_and_logs_the_would_be_refusal(ask, tmp_path):
    folder, home, ledger = tmp_path / "t1", tmp_path / "home", Ledger()
    write_plan(folder)
    ask(0.1, 0.1, 0.9, score=3.0, confidence=0.8)
    record, block = run(folder, ledger, home)
    assert (record["verdict"], block, ledger.followups) == ("fail", False, [])
    [row] = rows(home)
    assert (row["gate"], row["kind"], row["reason"]) == (
        "trace-plan",
        "observe",
        "2 of 3 pieces are off the task intent, more than half and "
        "the plan is sized a whole phase at confidence 0.80, above one pull request",
    )


@pytest.mark.parametrize(
    ("mode", "kind", "blocks"), [("enforce", "deny", True), ("observe", "observe", False), ("off", None, False)]
)
def test_the_second_failed_plan_blocks_only_when_enforced(ask, tmp_path, mode, kind, blocks):
    folder, home, ledger = tmp_path / "t1", tmp_path / "home", Ledger()
    write_plan(folder)
    ask(0.1, 0.1, 0.9)
    _, first = run(folder, ledger, home, mode)
    assert first is False
    write_plan(folder, DOGHOUSE.replace("diesel", "petrol"))
    record, second = run(folder, ledger, home, mode)
    assert (record["failures"], second) == (2, blocks)
    assert [row["kind"] for row in rows(home)] == ([kind, kind] if kind else [])


def test_run_logs_an_unchecked_plan_as_fail_open(ask, tmp_path):
    folder, home, ledger = tmp_path / "t1", tmp_path / "home", Ledger()
    write_plan(folder)
    ask(error=ClassifierUnavailable("down"))
    record, block = run(folder, ledger, home, "enforce")
    assert (record["verdict"], block, ledger.followups) == ("unchecked", False, [])
    [row] = rows(home)
    assert (row["gate"], row["kind"], row["reason"]) == ("trace-plan", "fail-open", "the classifier did not answer")


def test_run_needs_a_plan_file(tmp_path):
    with pytest.raises(ValueError) as refused:
        run(tmp_path / "t1", Ledger(), tmp_path / "home")
    assert str(refused.value) == f"write the plan first: {tmp_path / 't1' / 'plan.md'}, {trace_plan.FORMAT}"


def test_run_checks_follow_ups_against_its_own_task(ask, tmp_path):
    folder = tmp_path / "t1"
    write_plan(folder, "- a light | a | b\n")
    fake = ask(0.9)
    who = Who(name="engineer@1-1", swarm="sw", task="my_task")
    with pytest.raises(ValueError, match="^plan line 1: the ledger would refuse its follow up"):
        trace_plan.run(folder, INTENT, Ledger(), who, "observe", home=tmp_path / "home")
    assert fake.calls == []


@pytest.mark.parametrize("task_id", ["fx-8be892c4", "fx-8be892c4-code", "fx-8be892c4-cause"])
def test_run_accepts_registered_doctor_task_and_files_cut_piece(ask, tmp_path, task_id):
    folder = tmp_path / "task"
    write_plan(folder)
    ask(0.9, 0.8, 0.1)
    doc = {
        "overview": "Keep the dog warm.",
        "tasks": [{"id": task_id, "title": "Doghouse", "description": "Build the doghouse."}],
    }
    state = trace_plan.intent(doc, task_id)
    who = Who(name="engineer@1-1", swarm="sw", task=task_id)

    class CheckedLedger(Ledger):
        def followup(self, slug, text):
            trace_plan.ledger_comments.check(text, "item", task_ids=[task["id"] for task in doc["tasks"]])
            super().followup(slug, text)

    ledger = CheckedLedger()
    record, block = trace_plan.run(folder, state, ledger, who, "observe", home=tmp_path / "home")
    assert record["verdict"] == "pass" and not block
    assert ledger.followups == [f"Cut from the plan of task {task_id}: a diesel generator"]


def test_run_reads_a_broken_verdict_file_as_no_verdict(ask, tmp_path):
    folder = tmp_path / "t1"
    write_plan(folder)
    (folder / "plan-verdict.json").write_text("[1")
    fake = ask(0.9, 0.9, 0.9)
    record, _ = run(folder, Ledger(), tmp_path / "home")
    assert record["verdict"] == "pass" and len(fake.calls) == 1
    (folder / "plan-verdict.json").write_text("[1]")
    assert trace_plan.load(folder) == {}


# report and block note


def test_report_tells_the_agent_what_comes_next():
    passed = {"verdict": "pass", "pieces": [{"what": "a", "kept": True}, {"what": "b", "kept": False}], "reasons": []}
    assert trace_plan.report("t1", passed, False) == {
        "task": "t1",
        "verdict": "pass",
        "kept": ["a"],
        "cut": ["b"],
        "reasons": [],
        "next": "edit only inside the kept pieces' areas; each cut piece is a ledger follow up",
    }
    failed = {**passed, "verdict": "fail", "reasons": ["r1", "r2"]}
    assert trace_plan.report("t1", failed, False)["next"] == "revise plan.md and run trace-plan again: r1 and r2"
    assert trace_plan.report("t1", failed, True)["next"] == "stop now; the plan failed twice and the task is blocked"
    unchecked = {**passed, "verdict": "unchecked"}
    assert trace_plan.report("t1", unchecked, False)["next"] == (
        "the classifier did not answer; edits are allowed and counted, run trace-plan again later"
    )


def test_the_block_note_passes_the_ledger_comment_filter():
    from scripts.swarm_ledger import ledger_comments

    record = {
        "failures": 2,
        "reasons": [
            "3 of 4 pieces are off the task intent, more than half",
            "the plan is sized several pull requests at confidence 0.82, above one pull request",
        ],
    }
    note = trace_plan.block_note(record)
    assert note == (
        "Blocked by the plan trace after 2 failed plans: 3 of 4 pieces are off the task intent, more than half "
        "and the plan is sized several pull requests at confidence 0.82, above one pull request"
    )
    assert ledger_comments.problems(note, "comment") == []
