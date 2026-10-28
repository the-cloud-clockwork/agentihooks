from dataclasses import asdict

import pytest

from hooks import hook_manager
from hooks.context import master_question, operator_mode
from scripts.swarm import input_wait, master_questions
from scripts.swarm.health.findings import master_blocked_on_prompt
from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import _watch_idle
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.unit

SID = "master-session"
NAME = "master@d44a5a-0002"
MINUTE = 60_000
ENTERED = 1_792_000_000_000
PROMPT = "Which phase should the swarm focus on next?"
QUESTIONS = [f"{PROMPT} Options: Proof and cleanup; Hive placement.", "Freeze the hive phase? Options: Yes; No."]


class Ledger(FakeLedger):
    def __init__(self):
        super().__init__([])
        self.questions = []

    def question(self, slug, text, by, op_id):
        self.questions.append((slug, text, by, op_id))


class Runtime(FakeRuntime):
    def __init__(self, dismisses=True):
        super().__init__()
        self.dismisses, self.dismissed = dismisses, []

    def dismiss(self, agent):
        self.dismissed.append(agent.name)
        return self.dismisses


@pytest.fixture
def master(tmp_path, monkeypatch):
    for key, value in {
        "AGENTIHOOKS_SWARM": "sw",
        "AGENTIHOOKS_SWARM_TASK": "master",
        "AGENTIHOOKS_AGENT_NAME": NAME,
        "AGENTIHOOKS_HOME": str(tmp_path / "ah"),
        "LEDGER_DIR": str(tmp_path / "ledgers"),
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0))
    found.put_agent("sw", AgentRecord(NAME, "master", "", seat="master@sw"))
    return found


def unattributed():
    return {"session_id": SID, "tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": PROMPT}]}}


def test_the_gate_refuses_a_switched_on_master_whose_operator_has_been_quiet_past_the_window(master):
    operator_mode._save(SID, {"on": True, "turn_at": 1_000.0})
    assert master_question.refusal(unattributed(), now=1_000.0 + master_question.WINDOW_SEC) == (
        operator_mode.MASTER_ASK_REFUSAL
    )
    assert hook_manager._operator_question(unattributed()) == operator_mode.MASTER_ASK_REFUSAL


def test_the_gate_opens_for_a_master_whose_operator_typed_inside_the_window(master):
    operator_mode._save(SID, {"on": True, "turn_at": 1_000.0})
    assert master_question.refusal(unattributed(), now=1_000.0 + master_question.WINDOW_SEC - 1) == ""


def test_the_gate_leaves_engineers_and_other_tools_to_the_presence_check(master, monkeypatch):
    operator_mode._save(SID, {"on": True, "turn_at": None})
    assert master_question.refusal({**unattributed(), "tool_name": "Bash"}, now=1_000.0) == ""
    monkeypatch.setattr(master_question.master_coding, "is_master", lambda session: False)
    assert master_question.refusal(unattributed(), now=1_000.0) == ""


def watch(store, runtime, ledger, observation, now_ms):
    runtime.observe = lambda agent: observation
    return _watch_idle("sw", store, ledger, runtime, {}, store.agents("sw")[0], now_ms)


def waited(store, runtime, ledger, minutes):
    actions = []
    for minute in range(minutes + 1):
        actions = watch(store, runtime, ledger, PaneObservation("waiting", PROMPT), ENTERED + minute * MINUTE)
    return actions


def test_a_recorded_master_prompt_open_past_the_window_is_dismissed_and_its_questions_go_to_priorities(store):
    runtime, ledger = Runtime(), Ledger()
    master_questions.hold(store, "sw", NAME, QUESTIONS)
    assert waited(store, runtime, ledger, 9) == []
    assert runtime.dismissed == [] and ledger.questions == []
    actions = watch(store, runtime, ledger, PaneObservation("waiting", PROMPT), ENTERED + 10 * MINUTE)
    assert actions == [f"dismissed the question prompt of {NAME} after ten minutes and moved 2 questions to Priorities"]
    assert runtime.dismissed == [NAME]
    assert [(slug, text, by) for slug, text, by, _ in ledger.questions] == [("sw", q, NAME) for q in QUESTIONS]
    assert len({op for *_, op in ledger.questions}) == 2
    row = store.agents("sw")[0]
    assert (row.input_prompt, row.input_ticks) == ("", 0)
    assert master_questions.held(store, "sw", NAME) == []
    assert input_wait.since(store, "sw", NAME) is None
    assert master_blocked_on_prompt([asdict(row)]) == []


def test_a_failed_dismissal_keeps_the_prompt_and_retries_with_the_same_ledger_ids(store):
    runtime, ledger = Runtime(dismisses=False), Ledger()
    master_questions.hold(store, "sw", NAME, QUESTIONS)
    actions = waited(store, runtime, ledger, 10)
    assert actions == [f"could not dismiss the question prompt of {NAME}, retrying next tick"]
    again = watch(store, runtime, ledger, PaneObservation("waiting", PROMPT), ENTERED + 11 * MINUTE)
    assert again == actions
    first, second = ledger.questions[:2], ledger.questions[2:]
    assert first == second
    assert store.agents("sw")[0].input_prompt == PROMPT
    assert master_questions.held(store, "sw", NAME) == QUESTIONS


@pytest.mark.parametrize("held", [[], ["Restart the hive?"]], ids=["nothing recorded", "another question"])
def test_a_master_prompt_the_gate_did_not_record_is_left_alone(store, held):
    runtime, ledger = Runtime(), Ledger()
    if held:
        master_questions.hold(store, "sw", NAME, held)
    assert waited(store, runtime, ledger, 20) == []
    assert runtime.dismissed == [] and ledger.questions == []
    assert store.agents("sw")[0].input_prompt == PROMPT


def test_a_prompt_that_ends_drops_its_recorded_questions(store):
    runtime, ledger = Runtime(), Ledger()
    master_questions.hold(store, "sw", NAME, QUESTIONS)
    waited(store, runtime, ledger, 2)
    watch(store, runtime, ledger, PaneObservation("idle"), ENTERED + 3 * MINUTE)
    assert master_questions.held(store, "sw", NAME) == []


def test_an_opened_question_is_recorded_for_the_tick(master, store, tmp_path):
    from tests.test_master_question import FOCUS, ask, typed_at

    typed_at(1_000.0)
    assert master_question.move(ask(FOCUS), Ledger(), now=1_000.0 + 1, store=store) == ""
    assert master_questions.held(store, "sw", NAME) == [f"{FOCUS['question']} Options: Proof and cleanup; Hive placement."]
