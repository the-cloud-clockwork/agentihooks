from dataclasses import replace

import pytest

from scripts.swarm import prompt
from scripts.swarm.store import MASTER
from scripts.swarm.tick import tick
from tests.swarm.test_cli import _handoff_doc, env, run  # noqa: F401
from tests.swarm.test_take_master import taker  # noqa: F401
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks, workers  # noqa: F401

pytestmark = pytest.mark.unit

RECAP = """## Done
- Seam one green, `pytest -k seam_one` 3 passed

## Stopped at
Seam two test red for the expected reason.

## Next
1. Make seam two green; done when its test passes."""


@pytest.mark.parametrize("caller,seat", [("engineer@a1b2c3-0001", "eng-1@sw"), ("master@a1b2c3-0001", "master@sw")])
def test_handoff_derives_the_seat_recap_without_another_file(env, tmp_path, caller, seat):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc = _handoff_doc(tmp_path)
    assert run("sw", "--as", caller, "handoff", str(doc)) == 0
    (entry,) = swarm.memory.recaps(seat)
    assert (entry["occupant"], entry["task"]) == (caller, "master" if seat == "master@sw" else "t1")
    assert entry["text"] == RECAP


def test_a_legacy_recap_argument_cannot_replace_the_document_recap(env, tmp_path):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc = _handoff_doc(tmp_path)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "handoff", str(doc), "--recap", "unused") == 0
    assert swarm.memory.recaps("eng-1@sw")[0]["text"] == RECAP


@pytest.mark.parametrize("lane", ["eng", "ci", MASTER])
def test_priming_reads_the_envelope_and_ranked_addresses_before_older_recaps(lane):  # noqa: F811
    handoff = """# Handoff v2
## Intent
Finish the task.
## Done
None
## Stopped at
None
## Decisions and promises
None
## Next
Finish the task.
## Read first
1. workspace:t1/proof What is verified?
2. ledger:sw/tasks/t1 What remains?
<!-- handoff complete -->"""
    task = {
        "id": "t1",
        "title": "task",
        "description": "task",
        "seat": f"{lane}@sw",
        "handoff": handoff,
        "handoff_envelope": {"reason": "quota", "conversation_id": "previous conversation"},
        "recaps": [
            {"occupant": "old", "task": "t1", "text": text}
            for text in ("latest recap contents", "older recap contents")
        ],
    }
    rendered = prompt.build("sw", "/repo", lane, "successor", task)
    envelope = rendered.index('"reason": "quota"')
    document = rendered.index("# Handoff v2")
    reading = rendered.index("Open each address in rank order")
    first = rendered.index("workspace:t1/proof", reading)
    second = rendered.index("ledger:sw/tasks/t1", reading)
    assert envelope < document < reading < first < second < rendered.index("older recap contents")
    assert "previous conversation" in rendered


@pytest.mark.parametrize("lane", ["eng", MASTER])
def test_a_successor_spawn_receives_the_stored_envelope(store, lane):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, 1)
    first = masters(store)[0] if lane == MASTER else workers(store)[0]
    envelope = {"reason": "recycle", "agent": first.name, "task": first.task}
    store.put_handoff("sw", first.task, "document", seat=first.seat, envelope=envelope)
    store.put_agent("sw", replace(first, state="finished"))
    tick("sw", store, ledger, runtime, 2)
    primed = runtime.masters[-1][1] if lane == MASTER else runtime.tasks[-1]
    assert primed["handoff_envelope"] == envelope
    assert store.handoff_envelope("sw", first.task) is None


def test_take_master_renders_the_stored_envelope(taker, capsys):  # noqa: F811
    swarm, _, _, _ = taker
    envelope = {"reason": "operator", "agent": "old master"}
    swarm.put_handoff("sw", MASTER, "master handoff", seat="master@sw", envelope=envelope)
    capsys.readouterr()
    assert run("sw", "take-master") == 0
    rendered = capsys.readouterr().out
    assert rendered.index('"agent": "old master"') < rendered.index("master handoff")
    assert swarm.handoff_envelope("sw", MASTER) is None


@pytest.mark.parametrize(
    "text",
    [
        "run all tests",
        "run all tests because",
        "run all tests because...",
        "because failures matter",
        "run all tests becauseof failures",
    ],
)
def test_learned_refuses_a_missing_reason_and_writes_nothing(env, capsys, text):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as", "engineer@a1b2c3-0001", "learned", text) == 1
    assert "because" in capsys.readouterr().err
    assert swarm.memory.learned("eng-1@sw") == []


@pytest.mark.parametrize("maturity", ["data", "note", "insight", "canon"])
def test_learned_keeps_the_reason_and_maturity(env, maturity):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    text = "Run all tests BECAUSE narrow runs miss integration failures"
    assert run("sw", "--as", "master@a1b2c3-0001", "learned", text, "--maturity", maturity) == 0
    (note,) = swarm.memory.learned("master@sw")
    assert (note["text"], note["maturity"]) == (text, maturity)


@pytest.mark.parametrize("lane", ["eng", "ci", MASTER])
def test_the_prompt_requests_one_handoff_document_and_a_learned_reason(lane):
    rendered = prompt.build("sw", "/repo", lane, "successor", {"id": "t1", "title": "task", "description": "task"})
    assert "--recap" not in rendered
    assert "Handoff v2" in rendered
    assert "handoff skill" in rendered
    assert "recap is derived" in rendered
    instruction = next(line for line in rendered.splitlines() if line.startswith("If your context nears its limit"))
    assert instruction.startswith(
        "If your context nears its limit a hook tells you to write a handoff document: use the handoff skill "
    )
    if lane == MASTER:
        assert "Handoff v2 headings, run agentihooks swarm sw --as successor handoff <doc> and stop." in instruction
    else:
        assert "then run agentihooks swarm sw handoff <doc> and stop;" in instruction
    assert "<lesson because reason>" in rendered


def test_codex_master_handoff_next_uses_the_inbox_wait():
    from scripts.swarm import prompt

    action = "Rearm a Monitor on the ledger and continue."
    task = {
        "id": "master",
        "harness": "codex",
        "seat": "master@sw",
        "handoff": "# Handoff v2\n## Next\n" + action + "\n## Read first\nNone\n",
        "transfer": {"id": "transfer", "next": action, "handoff": "saved handoff"},
    }
    text = prompt.build_master("sw", "/repo", "master", task)
    assert "Monitor" not in text
    assert "agentihooks msg inbox" in text
    assert "agentihooks swarm sw wait --inbox" in text
    assert "Monitor" in task["handoff"]
    assert task["transfer"]["next"] == action


def test_claude_master_preserves_its_handoff_next():
    from scripts.swarm import prompt

    action = "Rearm a Monitor on the ledger and continue."
    task = {"id": "master", "harness": "claude", "handoff": "# Handoff v2\n## Next\n" + action}
    assert action in prompt.build_master("sw", "/repo", "master", task)
