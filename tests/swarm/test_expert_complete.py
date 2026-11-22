"""Ending an expert lineage: the expert that works it completes it on a mission proof that passes, and on no other."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import NamedTuple

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import expert_commands, expert_lineage, ledger_events
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import RedisStore, SwarmError
from tests.handoff.test_mission_proof import D1, D2, D3, FIRST, RULED, SECOND, VALID, VALIDATED
from tests.swarm.expert_fixture import (
    CONTROLS,
    ENGINEER,
    EXPERT,
    MASTER,
    SECOND_EXPERT,
    TITLE,
    acting,
    indexed,
    run,
    seated,
    started,
    store,
)

pytestmark = pytest.mark.unit
_fixtures = (store,)

MERGED, OPEN = PullRequest("MERGED", 5, 1, False, True), PullRequest("OPEN", None, 1, False, True)
ONLY = "swarm: only the expert that works a running lineage completes it, from its own session\n"
REFUSED = "swarm: mission proof refused; fix what it names and run the command again:\n"
RECAP = (
    "## Deliverables\n" + (D1 + D2 + D3).strip() + "\n\n## Validation\n" + VALIDATED.strip() + "\n\n## Open items\nNone"
)
OPEN_ITEM = "## Open items must be None; a lineage ends with nothing open"
NO_THIRD = "deliverable D3 is named by no Deliverables bullet; start one with D3"
NO_RULE = "## Semgrep rules holds no bullet; name at least one check with its evidence"
UNMERGED = f"pull request {FIRST} is not merged"


class Fault(NamedTuple):
    """One edit that breaks the valid proof, the pull request left open with it and the faults the command names."""

    old: str
    new: str
    unmerged: str
    named: list[str]


FAULTY = [
    Fault("## Open items\nNone\n", "## Open items\n- D3 needs one more pass\n", "", [OPEN_ITEM]),
    Fault(D3, "", "", [NO_THIRD]),
    Fault(D3, D3, FIRST, [UNMERGED]),
    Fault(RULED, "None\n", "", [NO_RULE]),
    Fault(D3, "", SECOND, [NO_THIRD]),
    Fault(D3 + "## Validation", "## Validation", FIRST, [NO_THIRD]),
    Fault("## Open items\nNone\n", "## Open items\nOne doc line.\n", FIRST, [OPEN_ITEM, UNMERGED]),
]


@pytest.fixture
def working(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    """Swarm sw with lineage expert-1 worked by EXPERT, every pull request merged and the clock at 30000."""
    started(store)
    seated(store, EXPERT, "expert-1")
    acting(monkeypatch, EXPERT)
    monkeypatch.setattr(expert_commands, "now_ms", lambda: 30_000)
    monkeypatch.setattr(ledger_events, "view", lambda url: MERGED)
    return store


def proof(tmp_path: Path, text: str = VALID) -> str:
    path = tmp_path / "proof.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def state(store: RedisStore, name: str) -> list[str]:
    return [agent.state for agent in store.agents("sw") if agent.name == name]


def mail(store: RedisStore) -> list[tuple[str, str]]:
    inbox = InboxStore(store.redis)
    found = [*inbox.pending_items("master@sw"), *inbox.pending_items(MASTER)]
    return [(item.sender, item.text) for item in found]


@pytest.mark.feature("expert-lineage-ends-on-proof")
def test_the_expert_ends_its_lineage_on_a_proof_that_passes(
    working: RedisStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lineage = expert_lineage.get(working, "sw", "expert-1")
    assert run("sw", "expert", "complete", proof(tmp_path)) == 0
    ended = replace(lineage, state="complete", ended_by=EXPERT, ended_at=30_000, note="mission proof accepted")
    assert json.loads(capsys.readouterr().out) == asdict(ended)
    assert expert_lineage.get(working, "sw", "expert-1") == ended
    assert expert_lineage.proof(working, "sw", "expert-1") == VALID
    assert expert_lineage.running(working, "sw") == [] and indexed(working) == set()
    assert state(working, EXPERT) == ["finished"]
    latest = working.memory.recaps("expert-1@sw")[0]
    assert (latest["occupant"], latest["task"], latest["text"]) == (EXPERT, "expert-1", RECAP)
    assert mail(working) == [
        (
            EXPERT,
            f"Expert lineage expert-1 completed its mission: {TITLE}. Its mission proof was accepted; read it with "
            "`agentihooks swarm sw expert proof expert-1`.",
        )
    ]


@pytest.mark.feature("expert-lineage-ends-on-proof")
@pytest.mark.parametrize("fault", FAULTY)
def test_a_proof_with_a_fault_is_refused_with_each_fault_and_the_lineage_still_runs(
    working: RedisStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: Fault,
) -> None:
    lineage = expert_lineage.get(working, "sw", "expert-1")
    monkeypatch.setattr(ledger_events, "view", lambda url: OPEN if url == fault.unmerged else MERGED)
    assert fault.old in VALID
    assert run("sw", "expert", "complete", proof(tmp_path, VALID.replace(fault.old, fault.new, 1))) == 1
    assert capsys.readouterr().err == REFUSED + "".join(f"- {named}\n" for named in fault.named)
    assert expert_lineage.get(working, "sw", "expert-1") == lineage
    assert expert_lineage.proof(working, "sw", "expert-1") == ""
    assert expert_lineage.running(working, "sw") == [lineage]
    assert state(working, EXPERT) == ["working"] and mail(working) == []
    assert len(working.memory.recaps("expert-1@sw")) == 0


def test_a_pull_request_that_cannot_be_read_counts_as_not_merged(
    working: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ledger_events, "view", lambda url: None if url == FIRST else MERGED)
    assert run("sw", "expert", "complete", proof(tmp_path)) == 1
    assert capsys.readouterr().err == f"{REFUSED}- pull request {FIRST} is not merged\n"


@pytest.mark.feature("expert-lineage-ends-on-proof")
@pytest.mark.parametrize("actor", ["", MASTER, ENGINEER, SECOND_EXPERT])
def test_only_the_expert_that_works_the_lineage_completes_it(
    working: RedisStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    actor: str,
) -> None:
    if actor:
        acting(monkeypatch, actor)
    else:
        monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    assert run("sw", "expert", "complete", proof(tmp_path)) == 1
    assert capsys.readouterr().err == ONLY
    assert expert_lineage.get(working, "sw", "expert-1").state == "running"


def test_an_expert_that_already_left_its_seat_completes_nothing(
    working: RedisStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seated(working, EXPERT, "expert-1", state="finished")
    assert run("sw", "expert", "complete", proof(tmp_path)) == 1
    assert capsys.readouterr().err == ONLY
    seated(working, EXPERT, "expert-1")
    assert run("sw", "expert", "complete", proof(tmp_path)) == 0
    capsys.readouterr()
    assert run("sw", "expert", "complete", proof(tmp_path)) == 1
    assert capsys.readouterr().err == ONLY


def test_each_expert_completes_its_own_lineage_and_no_other(
    working: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    second = started(working, at=2_000)
    seated(working, SECOND_EXPERT, second.id)
    acting(monkeypatch, SECOND_EXPERT)
    assert run("sw", "expert", "complete", proof(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "expert-2"
    assert [row.id for row in expert_lineage.running(working, "sw")] == ["expert-1"]
    assert (state(working, EXPERT), state(working, SECOND_EXPERT)) == (["working"], ["finished"])


def test_a_lineage_ends_once(working: RedisStore) -> None:
    expert_lineage.complete(working, "sw", "expert-1", EXPERT, VALID, 5)
    for end in (
        lambda: expert_lineage.complete(working, "sw", "expert-1", EXPERT, "another proof", 6),
        lambda: expert_lineage.stop(working, "sw", "expert-1", MASTER, "too late", 7),
    ):
        with pytest.raises(SwarmError) as refused:
            end()
        assert str(refused.value) == "expert lineage expert-1 is complete, not running"
    assert expert_lineage.proof(working, "sw", "expert-1") == VALID
    assert expert_lineage.proof(working, "sw", "expert-9") == ""


def test_a_lineage_stopped_by_order_holds_no_proof(working: RedisStore) -> None:
    expert_lineage.stop(working, "sw", "expert-1", MASTER, "the order changed", 5)
    assert expert_lineage.proof(working, "sw", "expert-1") == ""
    assert working.redis.hlen(working.key("sw", "expert-proofs")) == 0


def test_a_proof_file_that_cannot_be_read_is_refused_by_name(
    working: RedisStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nowhere.md"
    assert run("sw", "expert", "complete", str(missing)) == 1
    assert capsys.readouterr().err == f"swarm: cannot read the mission proof {missing}: No such file or directory\n"
    binary = tmp_path / "binary.md"
    binary.write_bytes(b"\xff\xfe")
    assert run("sw", "expert", "complete", str(binary)) == 1
    assert capsys.readouterr().err == f"swarm: cannot read the mission proof {binary}: it is not UTF-8 text\n"
    assert expert_lineage.get(working, "sw", "expert-1").state == "running"


@pytest.mark.feature("expert-lineage-ends-on-proof")
def test_the_master_and_the_operator_read_the_proof_a_lineage_ended_on(
    working: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    acting(monkeypatch, MASTER)
    assert run("sw", "expert", "proof", "expert-1") == 1
    assert capsys.readouterr().err == "swarm: expert lineage expert-1 holds no mission proof\n"
    acting(monkeypatch, EXPERT)
    assert run("sw", "expert", "complete", proof(tmp_path)) == 0
    capsys.readouterr()
    for actor in (MASTER, ""):
        if actor:
            acting(monkeypatch, actor)
        else:
            monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
        assert run("sw", "expert", "proof", "expert-1") == 0
        assert capsys.readouterr().out == VALID
    assert run("sw", "expert", "status", "expert-1") == 0
    assert capsys.readouterr().out == (
        f"expert-1 complete at xhigh: {TITLE} (started by {MASTER}, ended by {EXPERT}: mission proof accepted)\n"
    )
    acting(monkeypatch, ENGINEER)
    assert run("sw", "expert", "proof", "expert-1") == 1
    assert capsys.readouterr().err == f"swarm: {CONTROLS}, and {ENGINEER} is neither\n"
    assert run("sw", "expert", "proof", "expert-7") == 1
