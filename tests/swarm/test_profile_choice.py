import json
from types import SimpleNamespace

import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.swarm import profile_choice, runtime, store, tick
from tests.swarm.test_tick import FakeRuntime, tasks

pytestmark = pytest.mark.xdist_group("fakeredis")

TASK = {
    "id": "t9",
    "title": "Rank tasks before claim",
    "description": "Claim tasks in the order the operator ranks them on the page",
    "kind": "code",
    "phase": "p1",
    "territory": ["scripts/swarm/tick.py", "scripts/swarm_ledger/static"],
    "culture": "not public",
}


def answered(choice, confidence=0.9, source="pplx-decider-v1-27b", calibrated=True):
    return DecisionResult(
        {"responsibility": Answer("choice", choice=choice, confidence=confidence)}, source, calibrated=calibrated
    )


@pytest.fixture
def swarm():
    import fakeredis

    saved = store.RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(store.SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    return saved


@pytest.fixture
def asked(monkeypatch):
    calls = []

    def answer(choice, **kwargs):
        def decide(state, questions, **kw):
            calls.append((state, questions, kw))
            return answered(choice, **kwargs)

        monkeypatch.setattr(profile_choice, "decide", decide)
        return calls

    return answer


@pytest.fixture
def ledger_file(monkeypatch, tmp_path):
    path = tmp_path / "sw.json"
    path.write_text(
        json.dumps(
            {
                "overview": "Swarm Design System",
                "phases": [{"id": "p1", "title": "Ordering", "description": "Operator ranks the queue"}],
            }
        )
    )
    monkeypatch.setattr(profile_choice, "ledger_path", lambda slug: path)
    return path


def test_explicit_task_profile_wins_without_classifier(monkeypatch):
    monkeypatch.setattr(profile_choice, "decide", lambda *a, **k: pytest.fail("explicit profile asked classifier"))
    decision = profile_choice.choose("sw", "eng", {"profile": "engineer"}, {**TASK, "profile": "qa"}, {})
    assert (decision.profile, decision.source) == ("qa", "task")


@pytest.mark.parametrize(
    "lane,lanes,expected",
    [
        ("master", {}, "master"),
        ("plan", {}, "planner"),
        ("ci", {}, "cicd"),
        ("ci", {"profile": "cicd"}, "cicd"),
        ("eng", {"profile": "qa"}, "qa"),
    ],
)
def test_fixed_lane_responsibility_survives_without_classifier(monkeypatch, lane, lanes, expected):
    monkeypatch.setattr(profile_choice, "decide", lambda *a, **k: pytest.fail("fixed lane asked classifier"))
    decision = profile_choice.choose("sw", lane, lanes, {"id": "t9", "title": "Fix the page layout"}, {})
    assert (decision.profile, decision.source) == (expected, "lane")


def test_unpinned_engineering_task_asks_one_typed_choice_with_public_behaviour_and_intent(asked, ledger_file):
    calls = asked("frontend", confidence=0.83)
    decision = profile_choice.choose("sw", "eng", {"profile": "engineer"}, TASK, {})
    state, questions, kwargs = calls[0]
    assert kwargs == {"purpose": "profile-pick"}
    assert set(questions) == {"responsibility"}
    question = questions["responsibility"]
    assert question.type == "choice"
    assert set(question.options) == {"frontend", "engineer", "qa", "split", "unresolved"}
    assert "t9" in question.instructions and "Rank tasks before claim" in question.instructions
    assert "claim ordering" in question.options["frontend"]
    assert state == {
        "task": "t9",
        "title": TASK["title"],
        "description": TASK["description"],
        "kind": "code",
        "territory": TASK["territory"],
        "project_intent": "Swarm Design System",
        "phase": "p1",
        "phase_intent": "Ordering: Operator ranks the queue",
    }
    assert decision == profile_choice.ProfileDecision(
        profile="frontend",
        source="classifier",
        responsibility="frontend",
        model="pplx-decider-v1-27b",
        confidence=0.83,
        calibrated=True,
        anchors=("task:t9", "phase:p1", "territory:scripts/swarm/tick.py", "territory:scripts/swarm_ledger/static"),
    )


@pytest.mark.parametrize(
    "title,answer",
    [
        ("Restyle the frontend page buttons and css", "engineer"),
        ("Fix the backend python api server", "frontend"),
    ],
)
def test_contradictory_keywords_never_override_the_classifier(asked, title, answer):
    asked(answer)
    decision = profile_choice.choose("sw", "eng", {}, {**TASK, "title": title, "description": title}, {})
    assert decision.profile == answer


@pytest.mark.parametrize("choice", ["split", "unresolved"])
def test_mixed_or_unclear_responsibility_refuses_with_actionable_reason(asked, choice):
    asked(choice)
    with pytest.raises(profile_choice.ProfileUnresolved) as raised:
        profile_choice.choose("sw", "eng", {}, TASK, {})
    reason = str(raised.value)
    assert choice in reason and "pplx-decider-v1-27b" in reason
    assert "agentihooks ledger --slug sw task set t9 profile=" in reason


@pytest.mark.parametrize("confidence,floor", [(0.59, {}), (0.7, {"AGENTIHOOKS_PROFILE_PICK_MIN_CONFIDENCE": "0.75"})])
def test_low_confidence_refuses_instead_of_guessing(asked, confidence, floor):
    asked("frontend", confidence=confidence)
    with pytest.raises(profile_choice.ProfileUnresolved, match=f"frontend with confidence {confidence:.2f}"):
        profile_choice.choose("sw", "eng", {}, TASK, floor)


def test_confidence_at_floor_is_accepted(asked):
    asked("engineer", confidence=0.6)
    assert profile_choice.choose("sw", "eng", {}, TASK, {}).profile == "engineer"


def test_unavailable_classifier_never_falls_back_to_engineer(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("no decision backend answered")

    monkeypatch.setattr(profile_choice, "decide", unavailable)
    with pytest.raises(profile_choice.ProfileUnresolved, match="no decision backend answered.*task set t9 profile="):
        profile_choice.choose("sw", "eng", {}, TASK, {})


@pytest.mark.parametrize(
    "lane,task",
    [("eng", {**TASK, "profile": "ghost"}), ("master", {"id": "master"}), ("eng", TASK)],
)
def test_missing_profile_refuses_with_reason(monkeypatch, asked, lane, task):
    asked("frontend")
    monkeypatch.setattr(profile_choice, "installed", lambda name: False)
    with pytest.raises(profile_choice.ProfileUnresolved, match="is not installed"):
        profile_choice.choose("sw", lane, {}, task, {})


def test_runtime_records_the_decision_and_launches_its_profile(tmp_path, asked):
    asked("frontend")
    calls = []

    def launch(argv, **kwargs):
        calls.append(argv)
        out = "status=started\nroute_status=direct\npane_id=w:p1\nagent=claude\nprofile=frontend\nmodel=m\neffort=low\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    config = store.SwarmConfig("sw", str(tmp_path), 1, 0, code="a1b2c3")
    rt = runtime.HerdrRuntime(home=tmp_path, run=launch, choose=lambda *_: ("claude", "open"))
    placed = rt.spawn(config, "eng", "a", TASK)
    assert calls[0][calls[0].index("--profile") + 1] == "frontend"
    assert placed.profile_decision["profile"] == "frontend"
    assert placed.profile_decision["source"] == "classifier"
    assert placed.profile_decision["confidence"] == 0.9
    assert tick._placed(store.AgentRecord("a", "eng", "t9"), placed).profile_decision == placed.profile_decision


def test_runtime_refuses_launch_on_unresolved_profile(tmp_path, asked):
    asked("split")
    rt = runtime.HerdrRuntime(home=tmp_path, run=lambda *a, **k: pytest.fail("launched"), choose=lambda *_: ("x", "y"))
    with pytest.raises(profile_choice.ProfileUnresolved):
        rt.spawn(store.SwarmConfig("sw", str(tmp_path), 1, 0, code="a1b2c3"), "eng", "a", TASK)


def test_tick_blocks_task_with_the_unresolved_reason(swarm):
    ledger, rt = tasks(("t1", "eng")), FakeRuntime(crash=profile_choice.ProfileUnresolved("profile unresolved: set it"))
    ledger.comments = []
    ledger.comment = lambda slug, task_id, text, by: ledger.comments.append((task_id, text, by))
    actions = tick.tick("sw", swarm, ledger, rt, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "blocked" and swarm.claimant("sw", "t1") is None
    assert ledger.comments == [("t1", "profile unresolved: set it", "swarm")]
    assert "blocked t1: profile unresolved: set it" in actions


def test_tick_still_reopens_on_an_ordinary_spawn_failure(swarm):
    ledger = tasks(("t1", "eng"))
    tick.tick("sw", swarm, ledger, FakeRuntime(fail=True), now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open"
