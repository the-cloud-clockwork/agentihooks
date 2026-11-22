"""The handoff command holds an expert to the expert handoff of its lineage and every other agent to Handoff v2."""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.handoff import check, contracts, expert
from scripts.swarm import cli, expert_lineage
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.handoff.test_expert_contract import ADDRESS, D1, LINE, OPENS, VALID
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit

FIRST, EXPERT, ENGINEER = "expert@a1b2c3-0001", "expert@a1b2c3-0002", "engineer@a1b2c3-0001"
DIGEST = ADDRESS.rpartition("/")[2]
V2 = """# Handoff v2
## Intent
Finish the parser task for the swarm.
## Done
- Seam one green, `pytest -k seam_one` 3 passed
## Stopped at
Seam two test red for the expected reason.
## Decisions and promises
None
## Next
1. Make seam two green; done when its test passes.
## Read first
None
<!-- handoff complete -->
"""
RECAP = (
    f"## Done\n{D1.strip()}\n\n"
    "## Stopped at\nSlice two of the plan, its test red for the expected reason.\n\n"
    "## Next\n1. Finish slice two; done when its test passes."
)


class Ledger:
    """A ledger that holds no task and no item, as for a seat that claims none."""

    def item(self, slug: str, path: str) -> dict[str, Any] | None:
        return None

    def task(self, slug: str, task_id: str) -> dict[str, Any] | None:
        return None


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RedisStore:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("hooks.context.broadcast.session_name", lambda pid: "")
    for name in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM"):
        monkeypatch.delenv(name, raising=False)
    found = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", state="running", max_eng=1, max_ci=0))
    lineage = expert_lineage.start(found, "sw", GOOD, "/missions/batch.md", "xhigh", "operator", 1_000)
    found.put_agent("sw", AgentRecord(EXPERT, "expert", lineage.id, seat=lineage.seat, state="working"))
    found.put_agent("sw", AgentRecord(ENGINEER, "eng", "t1", seat="eng-1@sw", state="working"))
    found.memory.add_recap(lineage.seat, FIRST, lineage.id, "## Done\nNone", 2_000)
    monkeypatch.setattr(cli, "connect", lambda: found)
    monkeypatch.setattr(cli, "LedgerClient", Ledger)
    return found


def handed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, text: str) -> int:
    """The exit code of the handoff command run by the named agent of swarm sw on a document holding the text."""
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    doc = tmp_path / "handoff.md"
    doc.write_text(text, encoding="utf-8")
    return cli.main(["sw", "handoff", str(doc)])


def state(store: RedisStore, name: str) -> list[str]:
    return [agent.state for agent in store.agents("sw") if agent.name == name]


@pytest.mark.feature("expert-handoff-contract")
def test_an_experts_conforming_handoff_is_stored_under_its_lineage_and_finishes_the_expert(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert handed(monkeypatch, tmp_path, EXPERT, VALID) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["task"], out["state"]) == ("expert-1", "handoff")
    assert store.handoff("sw", "expert-1") == VALID
    assert store.handoff_seat("sw", "expert-1") == "expert-1@sw"
    assert out["envelope"] == store.handoff_envelope("sw", "expert-1")
    assert [out["envelope"][key] for key in ("seat", "agent", "task", "reason")] == [
        "expert-1@sw",
        EXPERT,
        "expert-1",
        "recycle",
    ]
    assert [out["transfer"][key] for key in ("seat", "task", "predecessor", "handoff", "next")] == [
        "expert-1@sw",
        "expert-1",
        EXPERT,
        VALID,
        "1. Finish slice two; done when its test passes.",
    ]
    assert state(store, EXPERT) == ["finished"]
    latest = store.memory.recaps("expert-1@sw")[0]
    assert (latest["occupant"], latest["task"], latest["text"]) == (EXPERT, "expert-1", RECAP)


@pytest.mark.feature("expert-handoff-contract")
def test_an_experts_handoff_v2_document_is_refused_with_each_fault_and_stores_nothing(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert handed(monkeypatch, tmp_path, EXPERT, V2) == 1
    err = capsys.readouterr().err
    for fault in (
        "handoff refused; fix the document and run the command again:",
        "- the first line must be # Expert handoff",
        "- ## Intent is not one of the ten headings: Mission, Done, Pending,",
        "- ## Mission is missing",
        f"- {OPENS}",
        "- deliverable D1 is named by no Done or Pending bullet; start one with D1",
    ):
        assert fault in err, fault
    assert store.handoff("sw", "expert-1") == "" and store.handoff_envelope("sw", "expert-1") is None
    assert state(store, EXPERT) == ["working"]
    assert len(store.memory.recaps("expert-1@sw")) == 1


@pytest.mark.feature("expert-handoff-contract")
def test_a_worker_is_held_to_handoff_v2_and_its_expert_document_is_refused(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert handed(monkeypatch, tmp_path, ENGINEER, VALID) == 1
    err = capsys.readouterr().err
    for fault in (
        "- the first line must be # Handoff v2",
        "- ## Mission is not one of the six headings: Intent, Done, Stopped at, Decisions and promises, Next, Read",
        "- ## Intent is missing",
    ):
        assert fault in err, fault
    assert "Expert handoff" not in err and "deliverable" not in err
    assert store.handoff("sw", "t1") == "" and state(store, ENGINEER) == ["working"]
    assert handed(monkeypatch, tmp_path, ENGINEER, V2) == 0
    assert store.handoff("sw", "t1") == V2 and state(store, ENGINEER) == ["finished"]


@pytest.mark.feature("expert-handoff-contract")
@pytest.mark.parametrize("gone", ["mission", "lineage"])
def test_an_expert_whose_lineage_or_saved_mission_is_gone_is_refused_by_name(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], gone: str
) -> None:
    if gone == "mission":
        store.redis.hdel(store.key("sw", "expert-missions"), DIGEST)
        told = f"swarm: the saved mission of lineage expert-1 is missing or does not match {ADDRESS}\n"
    else:
        store.redis.hdel(store.key("sw", "expert-lineages"), "expert-1")
        told = "swarm: swarm sw has no expert lineage expert-1\n"
    assert handed(monkeypatch, tmp_path, EXPERT, VALID) == 1
    assert capsys.readouterr().err == told
    assert store.handoff("sw", "expert-1") == "" and state(store, EXPERT) == ["working"]


@pytest.mark.feature("expert-handoff-contract")
def test_the_contract_follows_the_lane_of_the_author(store: RedisStore) -> None:
    def resolves(address: str) -> bool:
        return True

    agents = {agent.name: agent for agent in store.agents("sw")}
    for text in (VALID, V2):
        assert contracts.problems(text, resolves, store, "sw", agents[EXPERT]) == expert.problems(
            text, resolves, "sw", GOOD
        )
        assert contracts.problems(text, resolves, store, "sw", agents[ENGINEER]) == check.problems(text, resolves)
    assert contracts.problems(VALID, resolves, store, "sw", agents[EXPERT]) == []
    assert contracts.problems(V2, resolves, store, "sw", agents[ENGINEER]) == []
    assert contracts.problems(V2, resolves, store, "sw", agents[EXPERT]) != []
    assert contracts.problems(VALID, resolves, store, "sw", agents[ENGINEER]) != []


def test_the_contract_of_an_expert_reads_the_mission_of_its_own_lineage(store: RedisStore) -> None:
    later = GOOD.replace("Keep an ordered batch built", "Keep a second batch built")
    second = expert_lineage.start(store, "sw", later, "/missions/next.md", "max", "operator", 3_000)
    agent = AgentRecord("expert@a1b2c3-0003", "expert", second.id, seat=second.seat, state="working")
    found = contracts.problems(VALID, lambda address: True, store, "sw", agent)
    assert found == [OPENS.replace(LINE, f"{second.mission} Keep a second batch built")]
    with pytest.raises(SwarmError, match="swarm other has no expert lineage expert-2"):
        contracts.problems(VALID, lambda address: True, store, "other", agent)
