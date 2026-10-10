from types import SimpleNamespace

import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.swarm import profile_choice, runtime, store, tick
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm_ledger import legacy_page

pytestmark = pytest.mark.xdist_group("fakeredis")

REAL_INSTALLED = profile_choice.installed
REMEDY = (
    "set it with agentihooks ledger --slug sw task set t9 profile=<frontend|engineer|qa>, "
    "or split the task into one public responsibility each, then reopen it"
)
INSTRUCTIONS = (
    'Task t9 "Rank tasks before claim": which responsibility owns the public behavior this task changes? Judge what '
    "a user or caller observes changing, from the description, parent intent and territory, never from keywords or "
    "file names. Mixed interface and backend work follows the public behavior changed."
)
TASK = {
    "id": "t9",
    "title": "Rank tasks before claim",
    "description": "Claim tasks in the order the operator ranks them on the page",
    "kind": "ops",
    "phase": "p1",
    "territory": ["scripts/swarm/tick.py", "scripts/swarm_ledger/static"],
    "culture": "not public",
}


from tests.swarm.profile_fixture import validated


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
    phases = [{"id": "p0"}, {"id": "p1", "title": "Ordering", "description": "Operator ranks the queue"}, {"id": "p2"}]
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    legacy_page.store(tmp_path, "sw", {"overview": "Swarm Design System", "phases": phases})
    return tmp_path


def test_explicit_task_profile_wins_without_classifier(monkeypatch):
    monkeypatch.setattr(profile_choice, "decide", lambda *a, **k: pytest.fail("explicit profile asked classifier"))
    decision = profile_choice.choose("sw", "eng", {"profile": "engineer"}, {**TASK, "profile": "qa"}, {})
    assert decision == profile_choice.ProfileDecision("qa", "task", "explicit task profile")


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
    assert decision == profile_choice.ProfileDecision(expected, "lane", f"{lane} lane")


def test_unpinned_engineering_task_asks_one_typed_choice_with_public_behaviour_and_intent(asked, ledger_file):
    calls = asked("frontend", confidence=0.83)
    decision = profile_choice.choose("sw", "eng", {"profile": "engineer"}, TASK, {})
    state, questions, kwargs = calls[0]
    assert kwargs == {"purpose": "profile-pick"}
    assert set(questions) == {"responsibility"}
    question = questions["responsibility"]
    assert question.type == "choice"
    assert question.instructions == INSTRUCTIONS
    assert set(question.options) == {"frontend", "engineer", "qa", "split", "unresolved"}
    assert "claim ordering" in question.options["frontend"]
    assert state == {
        "task": "t9",
        "title": TASK["title"],
        "description": TASK["description"],
        "kind": "ops",
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


def test_bare_task_state_and_question_name_empty_fields(asked, ledger_file):
    calls = asked("engineer")
    profile_choice.choose("sw", "eng", {}, {"id": "t9"}, {})
    state, questions, _ = calls[0]
    assert questions["responsibility"].instructions.startswith('Task t9 "": which responsibility')
    assert state == {
        "task": "t9",
        "title": "",
        "description": "",
        "kind": "code",
        "territory": [],
        "project_intent": "Swarm Design System",
        "phase": "",
        "phase_intent": "",
    }
    assert profile_choice.state("sw", {})["task"] == ""
    assert profile_choice.state("sw", {"phase": "p2"})["phase_intent"] == ": "
    assert profile_choice.anchors({"id": "t9"}) == ("task:t9",)


def test_close_summary_is_not_parent_intent_and_missing_ledger_is_empty(monkeypatch, tmp_path):
    from scripts.swarm_ledger import ledger_close

    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    legacy_page.store(tmp_path, "sw", {"overview": f"Intent {ledger_close.MARK} summary"})
    assert profile_choice.state("sw", {})["project_intent"] == "Intent"
    assert profile_choice.state("gone", {})["project_intent"] == ""


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
    asked(choice, confidence=0.97)
    with pytest.raises(profile_choice.ProfileUnresolved) as raised:
        profile_choice.choose("sw", "eng", {}, TASK, {})
    assert str(raised.value) == (
        f"task t9 profile is unresolved: pplx-decider-v1-27b answered {choice} with confidence 0.97, "
        f"the floor is 0.60: {REMEDY}"
    )


@pytest.mark.parametrize(
    "choice,confidence,floor",
    [
        ("frontend", 0.59, {}),
        ("qa", 0.7, {"AGENTIHOOKS_PROFILE_PICK_MIN_CONFIDENCE": "0.75"}),
        ("split", 0.4, {}),
        ("unresolved", 0.59, {}),
    ],
)
def test_low_confidence_takes_the_lane_default_profile(asked, choice, confidence, floor):
    asked(choice, confidence=confidence)
    decision = profile_choice.choose("sw", "eng", {}, TASK, floor)
    shown = float(floor.get("AGENTIHOOKS_PROFILE_PICK_MIN_CONFIDENCE", 0.6))
    assert decision == profile_choice.ProfileDecision(
        "engineer",
        "lane default",
        f"eng lane default: pplx-decider-v1-27b answered {choice} with confidence {confidence:.2f}, "
        f"below the floor {shown:.2f}",
        "pplx-decider-v1-27b",
        confidence,
        True,
        ("task:t9", "phase:p1", "territory:scripts/swarm/tick.py", "territory:scripts/swarm_ledger/static"),
    )


def test_an_answer_without_confidence_takes_the_lane_default(asked):
    asked("frontend", confidence=None)
    decision = profile_choice.choose("sw", "eng", {}, TASK, {"AGENTIHOOKS_PROFILE_PICK_MIN_CONFIDENCE": "0.001"})
    assert (decision.profile, decision.source, decision.confidence) == ("engineer", "lane default", None)
    assert decision.responsibility.endswith("answered frontend with confidence 0.00, below the floor 0.00")


def test_a_legacy_floor_above_one_is_refused_with_its_remedy(monkeypatch, tmp_path, ledger_file):
    from hooks import config

    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(profile_choice, "decide", lambda *a, **k: pytest.fail("picked with a malformed floor"))
    with pytest.raises(profile_choice.ProfileUnresolved) as refused:
        profile_choice.choose("sw", "eng", {}, TASK, {"AGENTIHOOKS_PROFILE_PICK_MIN_CONFIDENCE": "1.5"})
    assert str(refused.value) == (
        "task t9 profile classification is unavailable (threshold confidence must be between zero and one): "
        "set it with agentihooks ledger --slug sw task set t9 profile=<frontend|engineer|qa>, or split the task "
        "into one public responsibility each, then reopen it"
    )


def test_a_refused_definition_is_unresolved_with_its_remedy(monkeypatch, tmp_path, ledger_file):
    from hooks import config

    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(profile_choice, "decide", lambda *a, **k: pytest.fail("picked without a definition"))
    with pytest.raises(profile_choice.ProfileUnresolved) as refused:
        profile_choice.classify("sw", TASK, {"AGENTIHOOKS_CLASSIFIER_PROFILE_PICK_CONFIDENCE": "1.5"})
    assert str(refused.value) == (
        "task t9 profile classification is unavailable (threshold confidence must be between zero and one): "
        "set it with agentihooks ledger --slug sw task set t9 profile=<frontend|engineer|qa>, or split the task "
        "into one public responsibility each, then reopen it"
    )


def test_pinned_task_profile_wins_over_a_low_confidence_answer(asked):
    calls = asked("frontend", confidence=0.1)
    decision = profile_choice.choose("sw", "eng", {}, {**TASK, "profile": "qa"}, {})
    assert (decision.profile, decision.source) == ("qa", "task") and calls == []


def test_confidence_at_floor_is_accepted(asked):
    asked("engineer", confidence=0.6)
    decision = profile_choice.choose("sw", "eng", {}, TASK, {})
    assert (decision.profile, decision.source) == ("engineer", "classifier")


def test_unavailable_classifier_never_falls_back_to_engineer(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("no decision backend answered")

    monkeypatch.setattr(profile_choice, "decide", unavailable)
    with pytest.raises(profile_choice.ProfileUnresolved) as raised:
        profile_choice.choose("sw", "eng", {}, TASK, {})
    assert (
        str(raised.value) == f"task t9 profile classification is unavailable (no decision backend answered): {REMEDY}"
    )


@pytest.mark.parametrize(
    "lane,task,profile",
    [("eng", {**TASK, "profile": "ghost"}, "ghost"), ("master", {"id": "t9"}, "master"), ("eng", TASK, "frontend")],
)
def test_missing_profile_refuses_with_reason(monkeypatch, asked, lane, task, profile):
    asked("frontend")
    monkeypatch.setattr(profile_choice, "installed", lambda name: name != profile)
    with pytest.raises(profile_choice.ProfileUnresolved) as raised:
        profile_choice.choose("sw", lane, {}, task, {})
    assert str(raised.value) == (
        f"task t9 needs profile {profile}, which is not installed: install it with agentihooks init or {REMEDY}"
    )


def test_installed_asks_the_profile_resolver(monkeypatch):
    from scripts.targets import _common

    found = {"engineer": "/profiles/engineer"}
    monkeypatch.setattr(_common, "_install_module", lambda: SimpleNamespace(_resolve_profile_dir=found.get))
    assert REAL_INSTALLED("engineer") is True
    assert REAL_INSTALLED("ghost") is False


def test_runtime_records_the_decision_and_launches_its_profile(tmp_path, asked, ledger_file):
    asked("frontend")
    calls = []

    def launch(argv, **kwargs):
        calls.append(argv)
        out = "status=started\nroute_status=direct\npane_id=w:p1\nagent=claude\nprofile=frontend\nmodel=m\neffort=low\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    config = store.SwarmConfig("sw", str(tmp_path), 1, 0, code="a1b2c3")
    rt = runtime.HerdrRuntime(home=tmp_path, run=launch, choose=lambda *_: ("claude", "open"))
    placed = rt.spawn(config, "eng", "a", TASK)
    assert calls[0][calls[0].index("--profile") + 1] == "frontend"
    assert placed.profile_decision["profile"] == "frontend"
    assert placed.profile_decision["source"] == "classifier"
    assert placed.profile_decision["confidence"] == 0.9
    assert tick.placed_record(store.AgentRecord("a", "eng", "t9"), placed).profile_decision == placed.profile_decision


def test_runtime_launches_the_lane_default_below_the_floor(tmp_path, asked, ledger_file):
    asked("frontend", confidence=0.3)
    calls = []

    def launch(argv, **kwargs):
        calls.append(argv)
        out = "status=started\nroute_status=direct\npane_id=w:p1\nagent=claude\nprofile=engineer\nmodel=m\neffort=low\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    config = store.SwarmConfig("sw", str(tmp_path), 1, 0, code="a1b2c3")
    rt = runtime.HerdrRuntime(home=tmp_path, run=launch, choose=lambda *_: ("claude", "open"))
    placed = rt.spawn(config, "eng", "a", TASK)
    assert calls[0][calls[0].index("--profile") + 1] == "engineer"
    assert placed.profile_decision["source"] == "lane default"
    assert placed.profile_decision["confidence"] == 0.3
    assert tick.placed_record(store.AgentRecord("a", "eng", "t9"), placed).profile_decision["source"] == "lane default"


def test_runtime_refuses_launch_on_unresolved_profile(tmp_path, asked):
    asked("split")
    rt = runtime.HerdrRuntime(home=tmp_path, run=lambda *a, **k: pytest.fail("launched"), choose=lambda *_: ("x", "y"))
    with pytest.raises(profile_choice.ProfileUnresolved):
        rt.spawn(store.SwarmConfig("sw", str(tmp_path), 1, 0, code="a1b2c3"), "eng", "a", TASK)


def _commenting(ledger):
    ledger.comments = []
    ledger.comment = lambda slug, task_id, text, by: ledger.comments.append((slug, task_id, text, by))
    return ledger


def test_tick_blocks_task_with_the_unresolved_reason(swarm):
    ledger = _commenting(tasks(("t1", "eng")))
    rt = FakeRuntime(crash=profile_choice.ProfileUnresolved("profile unresolved: set it"))
    actions = tick.tick("sw", swarm, ledger, rt, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "blocked" and swarm.claimant("sw", "t1") is None
    assert ledger.comments == [("sw", "t1", "profile unresolved: set it", "swarm")]
    assert "blocked t1: profile unresolved: set it" in actions


def test_unresolved_leaves_a_task_that_moved_on(swarm):
    ledger = _commenting(tasks(("t1", "eng")))
    ledger.rows["t1"]["state"] = "done"
    rows = {"t1": dict(ledger.rows["t1"])}
    said = tick._unresolved("sw", ledger, rows, "t1", "why")
    assert said == "task t1 is done on the ledger, its profile stays unresolved"
    assert ledger.rows["t1"]["state"] == "done" and ledger.comments == []


def test_tick_still_reopens_on_an_ordinary_spawn_failure(swarm):
    ledger = tasks(("t1", "eng"))
    tick.tick("sw", swarm, ledger, FakeRuntime(fail=True), now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open"
