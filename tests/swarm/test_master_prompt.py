from pathlib import Path

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import master_prompt
from scripts.swarm.health.findings import Finding, master_blocked_on_prompt
from scripts.swarm.pane import PaneObservation, selection_options
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.unit

MINUTE = 60_000
START = 1_000_000
SEAT = "master@sw"
NAME = "master@a1b2c3-0001"
TITLE = "Which phase should the swarm focus on next?"
OPTIONS = ("Proof and cleanup", "Hive placement")
PANE = (
    "Some earlier output\n"
    "1. Bring the master back\n"
    "\n"
    " ☐ Focus\n"
    "\n"
    f"{TITLE}\n"
    "\n"
    "❯ 1. Proof and cleanup\n"
    "     Finish the chaos test first\n"
    "  2. Hive placement\n"
    "     Spread engineers across hives\n"
    "\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)
ROLE_RULE = Path(__file__).resolve().parents[2] / "profiles/package/roles/master/.claude/rules/master-role.md"


class PromptRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.pane = PaneObservation("waiting", TITLE, prompt_options=OPTIONS)
        self.dismissed = []

    def observe(self, agent):
        return self.pane

    def dismiss_prompt(self, agent):
        self.dismissed.append(agent.name)


class QuestionLedger:
    def __init__(self):
        self.questions = {}

    def question(self, slug, text, by, op_id):
        assert slug == "sw"
        self.questions[op_id] = (text, by)


@pytest.fixture
def swarm():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0, state="paused"))
    store.put_agent("sw", AgentRecord(NAME, MASTER, "", pane_id="w1:m1", seat=SEAT, started_at=START))
    runtime = PromptRuntime()
    runtime.live.add(NAME)
    return store, runtime, QuestionLedger()


def run(swarm, at):
    store, runtime, ledger = swarm
    return master_prompt.run("sw", store, ledger, runtime, at)


def mail(store):
    return [item.text for item in InboxStore(store.redis).inbox(SEAT)]


def test_selection_options_reads_the_choices_under_the_question():
    assert selection_options(PANE) == OPTIONS


def test_selection_options_is_empty_without_a_prompt():
    assert selection_options("1. Bring the master back\n2. Fix the outage\n") == ()


def test_a_prompt_younger_than_ten_minutes_stays(swarm):
    run(swarm, START)
    assert run(swarm, START + 10 * MINUTE - 1) == []
    store, runtime, ledger = swarm
    assert ledger.questions == {} and runtime.dismissed == [] and mail(store) == []


def test_a_ten_minute_prompt_moves_to_priorities_dismisses_and_wakes_the_master(swarm):
    run(swarm, START)
    actions = run(swarm, START + 10 * MINUTE)
    store, runtime, ledger = swarm
    assert actions == [f"moved the question prompt open ten minutes on {NAME} to Priorities and freed its pane"]
    assert list(ledger.questions.values()) == [
        (f"{TITLE} Options: Proof and cleanup; Hive placement. Asked by the master {NAME}.", "swarm")
    ]
    assert runtime.dismissed == [NAME]
    assert mail(store) == [master_prompt.NOTICE.format(question=TITLE)]


def test_a_moved_prompt_is_never_copied_or_announced_twice(swarm):
    run(swarm, START)
    run(swarm, START + 10 * MINUTE)
    assert run(swarm, START + 11 * MINUTE) == []
    store, runtime, ledger = swarm
    assert len(ledger.questions) == 1 and len(mail(store)) == 1
    assert runtime.dismissed == [NAME, NAME]


def test_a_cleared_prompt_restarts_the_clock(swarm):
    store, runtime, ledger = swarm
    run(swarm, START)
    runtime.pane = PaneObservation("working")
    run(swarm, START + 5 * MINUTE)
    runtime.pane = PaneObservation("waiting", TITLE, prompt_options=OPTIONS)
    run(swarm, START + 6 * MINUTE)
    assert run(swarm, START + 15 * MINUTE) == []
    assert len(run(swarm, START + 16 * MINUTE)) == 1


def test_a_new_question_restarts_the_clock(swarm):
    store, runtime, ledger = swarm
    run(swarm, START)
    runtime.pane = PaneObservation("waiting", "Freeze the hive phase?", prompt_options=("Yes", "No"))
    run(swarm, START + 5 * MINUTE)
    assert run(swarm, START + 10 * MINUTE) == []
    assert len(run(swarm, START + 15 * MINUTE)) == 1
    assert [text for text, _ in ledger.questions.values()][0].startswith("Freeze the hive phase? Options: Yes; No.")


def test_engineers_on_a_prompt_are_left_to_the_waiting_on_input_finding(swarm):
    store, runtime, ledger = swarm
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0002", "eng", "t1", pane_id="w1:p1"))
    runtime.live.add("engineer@a1b2c3-0002")
    run(swarm, START)
    run(swarm, START + 10 * MINUTE)
    assert runtime.dismissed == [NAME]


def test_a_failed_dismissal_copies_nothing_and_retries_next_tick(swarm):
    store, runtime, ledger = swarm
    runtime.dismiss_prompt = lambda agent: "pane gone"
    run(swarm, START)
    assert run(swarm, START + 10 * MINUTE) == [
        f"could not dismiss the question prompt on {NAME}: pane gone, retrying next tick"
    ]
    assert ledger.questions == {} and mail(store) == []
    runtime.dismiss_prompt = lambda agent: None
    assert len(run(swarm, START + 11 * MINUTE)) == 1
    assert len(ledger.questions) == 1 and len(mail(store)) == 1


def test_a_permission_dialog_is_left_alone(swarm):
    store, runtime, ledger = swarm
    runtime.pane = PaneObservation("waiting", "Do you want to proceed?", prompt_options=("Yes", "No"))
    run(swarm, START)
    assert run(swarm, START + 30 * MINUTE) == []
    assert ledger.questions == {} and runtime.dismissed == []


def test_the_moved_question_shows_in_the_operator_priorities(swarm):
    from scripts.swarm_ledger.ledger_priorities import wanted

    store, runtime, ledger = swarm
    run(swarm, START)
    run(swarm, START + 10 * MINUTE)
    [(text, _)] = ledger.questions.values()
    rows = wanted({"phases": [], "questions": [{"id": "q1", "text": text}]})
    assert list(rows) == ["questions/q1"]
    assert rows["questions/q1"].startswith(f"Answer: {TITLE} Options: Proof and cleanup; Hive placement.")


def test_a_master_blocked_on_a_prompt_raises_a_finding():
    agents = [
        {"name": NAME, "lane": "master", "seat": SEAT, "input_prompt": TITLE, "input_ticks": 4},
        {"name": "master@a1b2c3-0002", "lane": "master", "input_prompt": TITLE, "input_ticks": 3},
        {"name": "engineer@a1b2c3-0003", "lane": "eng", "input_prompt": TITLE, "input_ticks": 9},
    ]
    assert master_blocked_on_prompt(agents) == [
        Finding(
            "master blocked on prompt",
            SEAT,
            "the master pane has waited on a question prompt for 4 ticks",
            (f"prompt: {TITLE}", f"agent {NAME}"),
            "more than 3 consecutive ticks on a question prompt",
            4,
        )
    ]


def test_the_master_role_rule_keeps_approved_decisions_off_the_question_tool():
    rule = " ".join(ROLE_RULE.read_text().split())
    assert (
        "The question tool is only for a decision you cannot make yourself, never for focus, freeze, rank, scope "
        "or caps the operator already approved." in rule
    )


def test_the_runtime_reads_the_prompt_options_from_the_master_pane(tmp_path):
    from scripts.swarm.runtime import HerdrRuntime

    def herdr(args):
        if args[:2] == ["pane", "read"]:
            return {"text": PANE}
        return {"agent": {"name": NAME, "pane_id": "w1:m1", "agent_status": "blocked"}}

    observed = HerdrRuntime(home=tmp_path, herdr=herdr).observe(AgentRecord(NAME, MASTER, "", pane_id="w1:m1"))
    assert (observed.state, observed.prompt_title, observed.prompt_options) == ("waiting", TITLE, OPTIONS)


def test_the_runtime_dismisses_a_prompt_with_escape_on_the_local_pane(tmp_path):
    from scripts.swarm.runtime import HerdrRuntime

    calls = []
    runtime = HerdrRuntime(home=tmp_path, herdr=lambda args: calls.append(args) or {})
    assert runtime.dismiss_prompt(AgentRecord(NAME, MASTER, "", pane_id="w1:m1")) is None
    assert calls == [["pane", "send-keys", "w1:m1", "esc"]]


def test_the_runtime_refuses_to_dismiss_a_pane_it_cannot_reach(tmp_path):
    from scripts.swarm.runtime import HerdrRuntime

    calls = []
    runtime = HerdrRuntime(home=tmp_path, herdr=lambda args: calls.append(args) or {})
    assert runtime.dismiss_prompt(AgentRecord(NAME, MASTER, "")) == "its pane is not a local herdr pane"
    assert calls == []


def test_the_runtime_reports_a_failed_dismissal(tmp_path):
    from scripts.swarm.runtime import HerdrRuntime

    def herdr(args):
        raise RuntimeError("pane gone")

    runtime = HerdrRuntime(home=tmp_path, herdr=herdr)
    assert runtime.dismiss_prompt(AgentRecord(NAME, MASTER, "", pane_id="w1:m1")) == "pane gone"


def test_the_ledger_client_adds_a_question_under_a_stable_operation_id(monkeypatch):
    from scripts.swarm import ledger_client

    sent = []
    monkeypatch.setattr(ledger_client.LedgerClient, "_call", lambda self, slug, ops: sent.append((slug, ops)))
    ledger_client.LedgerClient().question("sw", f"{TITLE} Options: Yes; No.", "swarm", "prompt-1")
    [(slug, [op])] = sent
    assert (slug, op["op"], op["id"], op["by"], op["list"], op["text"]) == (
        "sw",
        "add_item",
        "prompt-1",
        "swarm",
        "questions",
        f"{TITLE} Options: Yes; No.",
    )
