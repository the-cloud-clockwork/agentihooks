import pytest

from hooks.classifier import Answer, ClassifierRequestError, ClassifierUnavailable, DecisionResult
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
DEFAULT = {"difficulty": "M", "difficulty_source": "default", "difficulty_confidence": 0.0}


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


def failing(monkeypatch, error):
    def decide(*args, **kwargs):
        raise error

    monkeypatch.setattr(difficulty, "decide", decide)


def test_limits():
    assert (difficulty.PER_TICK, difficulty.MIN_CONFIDENCE, difficulty.FALLBACK) == (5, 0.6, "M")


@pytest.mark.parametrize(
    "area",
    [
        "k8s/charts/ledger",
        "antoncore/Kubernetes/apps",
        "charts/helm/values.yaml",
        "k8s/argocd/apps/ledger.yaml",
        "argo/apps",
        "scripts/deploy.sh",
        "docs/deploys.md",
        "deployment/ledger",
        "infra/terraform",
        "infrastructure",
        "charts/helm_values.yaml",
        "argocd_app.yaml",
        "k8s_manifests/x",
        "charts/templates/deployments.yaml",
        "scripts/deploying.sh",
    ],
)
def test_infrastructure_territory_is_large(area):
    assert difficulty.rule({**TASK, "territory": ["scripts/swarm/tick.py", area]}) == {
        "difficulty": "L",
        "difficulty_source": "rule",
        "difficulty_confidence": 1.0,
    }


@pytest.mark.parametrize("area", ["scripts/infr.py", "helmet/x", "argon.py", "redeploy.py", "k8.py", "deplo"])
def test_words_that_only_contain_an_infrastructure_name_have_no_rule(area):
    assert difficulty.rule({**TASK, "territory": [area]}) is None


def test_ops_kind_is_large_without_infrastructure_territory():
    assert difficulty.rule({**TASK, "kind": "ops"})["difficulty"] == "L"


@pytest.mark.parametrize(
    "territory",
    [
        ["scripts/swarm_ledger/static/js", "scripts/swarm_ledger/static"],
        ["scripts/swarm_ledger/static/"],
        [
            "scripts/swarm_ledger/home.html",
            "scripts/swarm_ledger/shell.html",
            "scripts/swarm_ledger/template.html",
            "scripts/swarm_ledger/palette.css",
            "scripts/swarm_ledger/tooltips.js",
        ],
    ],
)
def test_frontend_task_only_on_the_page_is_small(territory):
    task = {**TASK, "profile": "frontend", "territory": territory}
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
    assert difficulty.classify(TASK, DOC) == {
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
    assert questions["difficulty"].options == {
        "S": "A mundane frontend change: copy, layout, style or a small control on a page, even when grouped with "
        "others.",
        "M": "Normal agentihooks code or CI work: hooks, scripts, commands, the ledger server, tests or workflows.",
        "L": "Touches infrastructure, collects information, sets configuration or deploys to Kubernetes, possibly "
        "together with code.",
    }
    assert questions["difficulty"].instructions == (
        'Task t1 "Order claims": how big is this task by the operator rubric? Judge the work it asks for from the '
        "description, kind, profile, territory and phase intent."
    )
    assert kw == {"purpose": "task-difficulty"}


def test_classifier_state_without_a_phase_has_no_intent(asked):
    calls = asked("S", 0.9)
    difficulty.classify({"id": "t2"}, DOC)
    state, questions, _ = calls[0]
    assert state == {
        "task": "t2",
        "title": "",
        "description": "",
        "kind": "code",
        "profile": "",
        "territory": [],
        "phase_intent": "",
    }
    assert questions["difficulty"].instructions.startswith('Task t2 "": how big')


def test_classifier_state_of_a_bare_phase_and_doc(asked):
    calls = asked("S", 0.9)
    difficulty.classify({"id": "t3", "phase": "p2"}, {"phases": [{"id": "p2"}]})
    difficulty.classify({"id": "t4", "phase": "p2"}, {})
    assert [call[0]["phase_intent"] for call in calls] == [": ", ""]


def test_state_of_a_bare_task_matches_no_phase_without_an_id():
    assert difficulty.state({}, {"phases": [{"title": "Loose"}]}) == {
        "task": "",
        "title": "",
        "description": "",
        "kind": "code",
        "profile": "",
        "territory": [],
        "phase_intent": "",
    }


def test_a_task_without_territory_has_no_rule():
    task = {key: value for key, value in TASK.items() if key != "territory"}
    assert difficulty.rule(task) is None
    assert difficulty.rule({**task, "kind": "ops"})["difficulty"] == "L"


def test_pass_over_a_doc_without_tasks_sizes_nothing():
    assert difficulty.size_pass("sw", FakeLedger([]), {}) == []


def test_confidence_at_the_floor_is_kept(asked):
    asked("S", 0.6)
    assert difficulty.classify(TASK, DOC)["difficulty_source"] == "classifier"


def test_confidence_above_one_is_capped(asked):
    asked("S", 1.4)
    assert difficulty.classify(TASK, DOC)["difficulty_confidence"] == 1.0


@pytest.mark.parametrize(
    ("choice", "confidence", "kept"),
    [
        ("S", 0.59, 0.59),
        ("L", None, 0.0),
        ("XL", 0.9, 0.9),
        ("S", -0.2, 0.0),
        ("S", "high", 0.0),
        ("S", float("nan"), 0.0),
        ("S", True, 0.0),
    ],
)
def test_low_confidence_or_unknown_answer_falls_back_to_medium(asked, choice, confidence, kept):
    asked(choice, confidence)
    assert difficulty.classify(TASK, DOC) == {
        "difficulty": "M",
        "difficulty_source": "default",
        "difficulty_confidence": kept,
    }


@pytest.mark.parametrize("error", [ClassifierUnavailable("down"), ClassifierRequestError("bad request")])
def test_no_classifier_answer_falls_back_to_medium(monkeypatch, error):
    failing(monkeypatch, error)
    assert difficulty.classify(TASK, DOC) == DEFAULT


def test_pass_sizes_only_open_unsized_tasks_up_to_the_bound(asked):
    asked("S", 0.9)
    tasks = [
        {**TASK, "id": "a", "kind": "ops"},
        {**TASK, "id": "b", "difficulty": "L", "difficulty_source": "operator"},
        {**TASK, "id": "c", "state": "done"},
    ]
    ledger = FakeLedger(tasks + [{**TASK, "id": f"d{i}"} for i in range(5)])
    actions = difficulty.size_pass("sw", ledger, {**DOC, "tasks": ledger.tasks("sw")})
    assert actions == ["sized task a L by rule"] + [f"sized task d{i} S by classifier" for i in range(4)]
    assert ledger.rows["b"]["difficulty_source"] == "operator"
    assert ledger.rows["c"].get("difficulty") is None
    assert ledger.rows["d4"].get("difficulty") is None
    assert difficulty.size_pass("sw", ledger, {**DOC, "tasks": ledger.tasks("sw")}) == ["sized task d4 S by classifier"]


def test_pass_goes_on_past_a_refused_task(asked):
    asked("S", 0.9)

    class Refusing(FakeLedger):
        def update_task(self, slug, task_id, fields, by="swarm", if_state=()):
            if task_id == "a":
                raise LedgerRefused("refused")
            return super().update_task(slug, task_id, fields, by, if_state)

    ledger = Refusing([{**TASK, "id": "a"}, {**TASK, "id": "b"}])
    assert difficulty.size_pass("sw", ledger, {**DOC, "tasks": ledger.tasks("sw")}) == [
        "skipped sizing task a: the ledger refused its write",
        "sized task b S by classifier",
    ]
    assert ledger.rows["b"]["difficulty"] == "S"


def test_pass_asks_the_classifier_for_every_task_of_its_bound_at_once(monkeypatch):
    import threading

    monkeypatch.setattr(difficulty, "PER_TICK", 40)
    together = threading.Barrier(40, timeout=10)

    def decide(state, questions, **kw):
        together.wait()
        return answered("S", 0.9)

    monkeypatch.setattr(difficulty, "decide", decide)
    ledger = FakeLedger([{**TASK, "id": f"t{i}"} for i in range(41)])
    actions = difficulty.size_pass("sw", ledger, {**DOC, "tasks": ledger.tasks("sw")})
    assert actions == [f"sized task t{i} S by classifier" for i in range(40)]
    assert ledger.rows["t40"].get("difficulty") is None


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
