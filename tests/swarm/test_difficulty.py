import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.swarm import difficulty
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

DOC = {"phases": [{"id": "p1", "title": "Sizing", "description": "Every task carries a size"}]}
TASK = {
    "id": "t1",
    "title": "Order claims",
    "description": "Claim by rank",
    "kind": "code",
    "profile": "engineer",
    "phase": "p1",
    "territory": ["scripts/swarm/tick.py"],
}


def answered(choice, confidence):
    return DecisionResult({"difficulty": Answer("choice", choice=choice, confidence=confidence)}, "unit-test")


@pytest.fixture
def asked(monkeypatch):
    calls = []

    def answer(choice, confidence=0.9):
        def decide(state, questions, **kw):
            calls.append((state, questions, kw))
            return answered(choice, confidence)

        monkeypatch.setattr(difficulty, "decide", decide)
        return calls

    return answer


@pytest.mark.parametrize(
    "area",
    [
        "k8s/charts/ledger",
        "antoncore/Kubernetes/apps",
        "charts/helm/values.yaml",
        "k8s/argocd/apps/ledger.yaml",
        "scripts/deploy.sh",
        "infra/terraform",
    ],
)
def test_infrastructure_territory_is_large(area):
    assert difficulty.rule({**TASK, "territory": ["scripts/swarm/tick.py", area]}) == {
        "difficulty": "L",
        "difficulty_source": "rule",
        "difficulty_confidence": 1.0,
    }


def test_ops_kind_is_large_without_infrastructure_territory():
    assert difficulty.rule({**TASK, "kind": "ops"})["difficulty"] == "L"


def test_frontend_task_only_on_the_page_is_small():
    task = {
        **TASK,
        "profile": "frontend",
        "territory": ["scripts/swarm_ledger/static/js", "scripts/swarm_ledger/static"],
    }
    assert difficulty.rule(task) == {"difficulty": "S", "difficulty_source": "rule", "difficulty_confidence": 1.0}


@pytest.mark.parametrize(
    "task",
    [
        {**TASK, "profile": "frontend", "territory": ["scripts/swarm_ledger/static", "scripts/swarm_ledger/server.py"]},
        {**TASK, "profile": "frontend", "territory": ["scripts/swarm_ledger/staticky"]},
        {**TASK, "profile": "frontend", "territory": []},
        {**TASK, "profile": "engineer", "territory": ["scripts/swarm_ledger/static/js"]},
        TASK,
    ],
)
def test_other_tasks_have_no_rule(task):
    assert difficulty.rule(task) is None


def test_classifier_sizes_with_the_operator_rubric(asked):
    calls = asked("L", 0.8)
    assert difficulty.classify(TASK, DOC, {}) == {
        "difficulty": "L",
        "difficulty_source": "classifier",
        "difficulty_confidence": 0.8,
    }
    state, questions, kw = calls[0]
    assert state == {
        "task": "t1",
        "title": "Order claims",
        "description": "Claim by rank",
        "kind": "code",
        "profile": "engineer",
        "territory": ["scripts/swarm/tick.py"],
        "phase_intent": "Sizing: Every task carries a size",
    }
    assert questions["difficulty"].options == difficulty.RUBRIC
    assert set(difficulty.RUBRIC) == {"S", "M", "L"}
    assert questions["difficulty"].instructions.startswith('Task t1 "Order claims": how big')
    assert kw == {"purpose": "task-difficulty"}


def test_classifier_state_without_a_phase_has_no_intent(asked):
    calls = asked("S", 0.9)
    difficulty.classify({"id": "t2"}, DOC, {})
    assert calls[0][0] == {
        "task": "t2",
        "title": "",
        "description": "",
        "kind": "code",
        "profile": "",
        "territory": [],
        "phase_intent": "",
    }


def test_confidence_at_the_floor_is_kept(asked):
    asked("S", 0.6)
    assert difficulty.classify(TASK, DOC, {})["difficulty_source"] == "classifier"


@pytest.mark.parametrize(("choice", "confidence"), [("S", 0.59), ("L", None), ("XL", 0.9)])
def test_low_confidence_or_unknown_answer_falls_back_to_medium(asked, choice, confidence):
    asked(choice, confidence)
    assert difficulty.classify(TASK, DOC, {}) == {
        "difficulty": "M",
        "difficulty_source": "default",
        "difficulty_confidence": confidence or 0.0,
    }


def test_confidence_floor_follows_the_environment(asked):
    asked("S", 0.7)
    assert difficulty.classify(TASK, DOC, {"AGENTIHOOKS_DIFFICULTY_MIN_CONFIDENCE": "0.8"})["difficulty"] == "M"


def test_no_classifier_falls_back_to_medium(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("down")

    monkeypatch.setattr(difficulty, "decide", unavailable)
    assert difficulty.classify(TASK, DOC, {}) == {
        "difficulty": "M",
        "difficulty_source": "default",
        "difficulty_confidence": 0.0,
    }


def test_pass_sizes_only_open_unsized_tasks_up_to_the_bound(asked):
    asked("S", 0.9)
    ledger = FakeLedger(
        [
            {**TASK, "id": "a", "kind": "ops"},
            {**TASK, "id": "b", "difficulty": "L", "difficulty_source": "operator"},
            {**TASK, "id": "c", "state": "done"},
            {**TASK, "id": "d"},
            {**TASK, "id": "e", "state": "claimed"},
        ]
    )
    doc = {**DOC, "tasks": ledger.tasks("sw")}
    assert difficulty.size_pass("sw", ledger, doc, {"AGENTIHOOKS_DIFFICULTY_PER_TICK": "2"}) == [
        "sized task a L by rule",
        "sized task d S by classifier",
    ]
    assert [ledger.rows[t].get("difficulty") for t in "abcde"] == ["L", "L", None, "S", None]
    assert difficulty.size_pass("sw", ledger, {**DOC, "tasks": ledger.tasks("sw")}, {}) == [
        "sized task e S by classifier"
    ]


def test_pass_bound_defaults_to_per_tick(asked):
    asked("M", 0.9)
    ledger = FakeLedger([{**TASK, "id": f"t{i}"} for i in range(difficulty.PER_TICK + 2)])
    assert len(difficulty.size_pass("sw", ledger, {**DOC, "tasks": ledger.tasks("sw")}, {})) == difficulty.PER_TICK


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0))
    return saved


def test_tick_sizes_unsized_tasks(store):
    ledger = FakeLedger([{**TASK, "id": "t1", "kind": "ops"}, {**TASK, "id": "t2"}])
    actions = tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    assert "sized task t1 L by rule" in actions
    assert "sized task t2 M by default" in actions
    assert ledger.rows["t2"]["difficulty_source"] == "default"


def test_tick_goes_on_past_a_refused_size(store):
    class Refusing(FakeLedger):
        def update_task(self, slug, task_id, fields, by="swarm", if_state=()):
            if "difficulty" in fields:
                raise LedgerRefused("refused")
            return super().update_task(slug, task_id, fields, by, if_state)

    actions = tick("sw", store, Refusing([TASK]), FakeRuntime(), now_ms=1_000)
    assert "skipped scripts.swarm.difficulty.size_pass: the ledger refused its write" in actions


@pytest.mark.parametrize(
    "fields",
    [
        difficulty.rule({**TASK, "kind": "ops"}),
        difficulty.sized("M", "default", 0.0),
        difficulty.sized("S", "classifier", 0.75),
    ],
)
def test_written_sizes_pass_the_ledger_task_check(fields):
    from scripts.swarm_ledger import ledger

    ledger.ledger_tasks.check({"op": "task_update", "by": "swarm", "item": "tasks/t1", "fields": dict(fields)})
