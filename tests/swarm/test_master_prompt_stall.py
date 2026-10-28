import json

import pytest

from scripts.gates import catalog
from scripts.gates import log as gate_log
from scripts.handoff import transfers
from scripts.inbox.store import InboxStore
from scripts.swarm import master_prompt_stall, master_retire, master_stall
from scripts.swarm.pane import PaneObservation, interrupted_prompt
from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit
MINUTE = 60_000
START = 1_800_000_000_000


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path / "swarm")
    monkeypatch.setenv(master_retire.MINUTES, "5")


class PromptRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.panes, self.tails = {}, {}

    def observe(self, agent):
        return self.panes.get(agent.name) or super().observe(agent)

    def pane_tail(self, pane_id, lines):
        assert lines == master_prompt_stall.TAIL_LINES
        tail = self.tails.get(pane_id, "")
        if isinstance(tail, Exception):
            raise tail
        return tail


QUESTION = PaneObservation("waiting", "Which task first?")
INTERRUPTED = PaneObservation("idle")
INTERRUPT = "● Asking the operator\n  ⎿  Interrupted · What should Claude do instead?\n\n─────\n❯ \n─────\n"


def master(store, pane=QUESTION, slug="sw", enforce=True):
    name = store.next_name(slug, MASTER)
    agent = AgentRecord(name, MASTER, MASTER, pane_id="w1:p1", started_at=START, seat=f"master@{slug}")
    store.put_agent(slug, agent)
    store.seats.occupy(agent.seat, agent.name, START)
    if enforce:
        store.update(slug, gates={master_prompt_stall.GATE.name: "enforce"})
    runtime = PromptRuntime()
    runtime.live.add(name)
    runtime.panes[name] = pane
    runtime.tails[agent.pane_id] = INTERRUPT
    return agent, runtime


def operator_says(store, at, slug="sw", text="are you there"):
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", f"master@{slug}", text)
    store.redis.hset(inbox.key("item", item.id), "created_at", at)
    return inbox.get(item.id)


def run(store, runtime, at, slug="sw"):
    return master_prompt_stall.run(slug, store.config(slug), store, FakeLedger([]), runtime, at)


def rows(slug="sw"):
    path = gate_log.gate_log_path(slug)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("pane", [QUESTION, INTERRUPTED], ids=["question", "interrupted"])
def test_master_held_on_a_prompt_with_an_operator_item_is_woken_then_recycled(store, pane):
    agent, runtime = master(store, pane)
    operator_says(store, START)
    assert run(store, runtime, START) == []
    assert run(store, runtime, START + 9 * MINUTE) == []
    held = master_prompt_stall.held_on(pane, INTERRUPT)
    reason = master_prompt_stall.REASON.format(prompt=held, minutes=10)
    assert run(store, runtime, START + 10 * MINUTE) == [
        f"asked {agent.name} for a handoff before retiring it: {reason}"
    ]
    assert [i.ref for i in InboxStore(store.redis).pending_items(agent.name)] == [master_retire.REF]
    assert run(store, runtime, START + 14 * MINUTE) == [
        f"waiting on {agent.name}'s handoff before retiring it: {reason}"
    ]
    assert run(store, runtime, START + 15 * MINUTE) == [
        f"recycled master {agent.name} in its seat after 15 minutes held on {held} with operator items waiting"
    ]
    assert [a.state for a in store.agents("sw")] == ["finished"]
    handoff = store.handoff("sw", MASTER)
    assert f"{agent.name} sat 15 minutes on {held} while operator items waited" in handoff
    assert handoff.endswith("<!-- handoff complete -->\n")
    assert store.handoff_envelope("sw", MASTER)["reason"] == "recycle"
    [row] = transfers.list_transfers(store, "sw")
    assert (row["reason"], row["predecessor"]) == (master_stall.STALLED, agent.name)
    assert [(r["gate"], r["kind"], r["tool"]) for r in rows()] == [
        ("reset", "deny", "wake"),
        ("reset", "deny", "recycle"),
    ]


def test_held_on_names_the_prompt():
    assert master_prompt_stall.held_on(QUESTION) == "the prompt Which task first?"
    assert master_prompt_stall.held_on(PaneObservation("waiting")) == "the prompt waiting on input"
    assert master_prompt_stall.held_on(INTERRUPTED, INTERRUPT) == "an interrupted prompt"
    assert master_prompt_stall.held_on(INTERRUPTED, INTERRUPT, "codex") == ""
    assert master_prompt_stall.held_on(INTERRUPTED) == ""
    assert master_prompt_stall.held_on(PaneObservation("working"), INTERRUPT) == ""


@pytest.mark.parametrize("tail", ["● Back to work\n❯ \n", OSError("pane gone")], ids=["answered", "unreadable"])
def test_idle_master_off_an_interrupt_stays(store, tail):
    agent, runtime = master(store, INTERRUPTED)
    runtime.tails[agent.pane_id] = tail
    operator_says(store, START)
    run(store, runtime, START)
    assert run(store, runtime, START + 60 * MINUTE) == []


def test_the_count_starts_when_an_operator_item_arrives(store):
    agent, runtime = master(store)
    assert run(store, runtime, START) == []
    operator_says(store, START + 20 * MINUTE)
    assert run(store, runtime, START + 20 * MINUTE) == []
    assert run(store, runtime, START + 29 * MINUTE) == []
    assert run(store, runtime, START + 30 * MINUTE)[0].startswith(f"asked {agent.name}")


def test_a_prompt_without_operator_items_stays(store):
    agent, runtime = master(store)
    InboxStore(store.redis).send("engineer@sw-3", "master@sw", "a question")
    for minutes in (0, 15, 60):
        assert run(store, runtime, START + minutes * MINUTE) == []
    assert InboxStore(store.redis).pending_items(agent.name) == []


@pytest.mark.parametrize("pane", [PaneObservation("working"), PaneObservation("idle")])
def test_master_off_the_prompt_stays_and_restarts_the_count(store, pane):
    agent, runtime = master(store)
    operator_says(store, START)
    run(store, runtime, START)
    runtime.panes[agent.name] = pane
    assert run(store, runtime, START + 30 * MINUTE) == []
    runtime.panes[agent.name] = QUESTION
    assert run(store, runtime, START + 31 * MINUTE) == []
    assert run(store, runtime, START + 40 * MINUTE) == []
    assert run(store, runtime, START + 41 * MINUTE)[0].startswith(f"asked {agent.name}")


def test_leaving_the_prompt_after_the_ask_withdraws_it(store):
    agent, runtime = master(store)
    operator_says(store, START)
    run(store, runtime, START)
    run(store, runtime, START + 10 * MINUTE)
    [ask] = InboxStore(store.redis).pending_items(agent.name)
    runtime.panes[agent.name] = PaneObservation("working")
    assert run(store, runtime, START + 16 * MINUTE) == []
    assert master_retire.reason(store, "sw", agent.name) == ""
    assert InboxStore(store.redis).get(ask.id).reason == f"done: {master_prompt_stall.RESUMED}"
    assert store.agents("sw")[0].state != "finished"


def test_other_retire_reasons_are_left_alone(store):
    agent, runtime = master(store, PaneObservation("working"))
    master_retire.hold(store, "sw", agent, "the swarm is stopping", True, START)
    assert run(store, runtime, START + MINUTE) == []
    assert master_retire.reason(store, "sw", agent.name) == "the swarm is stopping"


def test_log_only_by_default_writes_would_be_rows_once_and_acts_on_nothing(store):
    agent, runtime = master(store, enforce=False)
    assert catalog.defaults()["reset"] == "observe"
    operator_says(store, START)
    run(store, runtime, START)
    held = master_prompt_stall.held_on(QUESTION)
    assert run(store, runtime, START + 10 * MINUTE) == [
        f"log only: would ask {agent.name} for a handoff, held on {held} with operator items waiting for 10 minutes"
    ]
    assert run(store, runtime, START + 11 * MINUTE) == []
    assert run(store, runtime, START + 15 * MINUTE) == [
        f"log only: would recycle {agent.name}, held on {held} with operator items waiting for 15 minutes"
    ]
    assert run(store, runtime, START + 60 * MINUTE) == []
    assert InboxStore(store.redis).pending_items(agent.name) == []
    assert store.agents("sw")[0].state != "finished"
    assert [(r["kind"], r["tool"], r["agent"]) for r in rows()] == [
        ("observe", "wake", agent.name),
        ("observe", "recycle", agent.name),
    ]


def test_off_mode_skips(store):
    agent, runtime = master(store, enforce=False)
    store.update("sw", gates={"reset": "off"})
    operator_says(store, START)
    run(store, runtime, START)
    assert run(store, runtime, START + 60 * MINUTE) == []
    assert rows() == []


@pytest.mark.parametrize("state", ["stopping", "stopped"])
def test_stopping_swarm_is_left_to_its_stop(store, state):
    _, runtime = master(store)
    operator_says(store, START)
    store.update("sw", state=state)
    assert run(store, runtime, START + 60 * MINUTE) == []


def test_master_not_live_stays(store):
    agent, runtime = master(store)
    operator_says(store, START)
    runtime.live.discard(agent.name)
    assert run(store, runtime, START + 60 * MINUTE) == []


def test_tick_recycles_a_master_on_a_question_and_a_fresh_master_answers_the_operator(store):
    agent, runtime = master(store)
    item = operator_says(store, START)
    ledger = FakeLedger([])
    tick("sw", store, ledger, runtime, START)
    first = tick("sw", store, ledger, runtime, START + 10 * MINUTE)
    assert any(a.startswith(f"asked {agent.name} for a handoff") for a in first)
    later = tick("sw", store, ledger, runtime, START + 15 * MINUTE)
    assert any(a.startswith(f"recycled master {agent.name} in its seat after 15 minutes held on") for a in later)
    tick("sw", store, ledger, runtime, START + 16 * MINUTE)
    assert agent.name in runtime.killed
    tick("sw", store, ledger, runtime, START + 17 * MINUTE)
    fresh = store.seats.occupant(agent.seat).occupant
    assert fresh and fresh != agent.name and fresh in runtime.live
    inbox = InboxStore(store.redis)
    assert item.id in [i.id for i in inbox.open_items(agent.seat)]
    inbox.reply(item.id, fresh, "here, working your list")
    assert inbox.get(item.id).state not in ("pending", "delivered", "confirmed")


@pytest.mark.parametrize(
    "harness, text, expected",
    [
        ("claude", INTERRUPT, True),
        ("claude", "  ⎿  User declined to answer questions\n❯ \n", True),
        ("claude", "  ⎿  User rejected tool use\n❯ \n", True),
        ("claude", "  ⎿  \x1b[31mInterrupted by user\x1b[0m\n❯ \n", True),
        ("claude", INTERRUPT + "● Back to work\n", False),
        ("claude", "● All done\n❯ \n", False),
        ("claude", "Interrupted · somewhere in prose\n", False),
        ("codex", INTERRUPT, False),
    ],
)
def test_interrupted_prompt_reads_the_last_turn(harness, text, expected):
    assert interrupted_prompt(harness, text) is expected
