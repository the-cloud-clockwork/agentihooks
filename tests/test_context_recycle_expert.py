"""The context limit speaks the expert handoff to an expert: the preparation directive, the hard gate and the check
the gate runs on a handoff document before the command."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import hooks.context.context_recycle as recycle
from scripts.swarm import expert_lineage
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.handoff.test_expert_contract import OPENS, VALID
from tests.swarm.test_expert_handoff_command import V2
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit

NAME = "expert@a1b2c3-0002"
EXPERT = {"AGENTIHOOKS_AGENT_NAME": NAME, "AGENTIHOOKS_SWARM": "sw"}
ENGINEER = {"AGENTIHOOKS_AGENT_NAME": "engineer@a1b2c3-0001", "AGENTIHOOKS_SWARM": "sw"}
MASTER = {"AGENTIHOOKS_AGENT_NAME": "master@a1b2c3-0001", "AGENTIHOOKS_SWARM": "sw"}
FORM = (
    "in expert handoff form, as the expert-handoff skill lays it out: the line # Expert handoff, then ## Mission, "
    "## Done, ## Pending, ## Assumptions, ## Tests, ## Validation, ## Stopped at, ## Decisions and promises, ## Next "
    "and ## Read first, each once and in that order, None under a heading with nothing to say, and "
    "<!-- handoff complete --> as the last line. Mission opens with the mission line that "
    "`agentihooks swarm sw expert mission` prints. Every deliverable of the mission is named once, by a Done or "
    "a Pending bullet that starts D and its number. Every Done, Tests and Validation bullet carries its evidence or "
    "the word hypothesis; Read first lists addresses (an issue or pull request link, mission:<slug>/<sha256>, "
    "recap:<seat>, inbox:<id>), each with the question it answers; no file paths, line numbers or credential "
    "values. Write it to a file under ~/scratchpad, record any lesson for your seat with "
    '`agentihooks swarm sw learned "<lesson>"`, then run `agentihooks swarm sw handoff <doc>` and stop. '
    "The next expert of your lineage continues the mission from it."
)
HEAD = "CONTEXT RECYCLE — this session holds 700k tokens, at or over the 650k limit. Write a handoff document "
ALLOWED = (
    " Until the handoff every tool call is denied except reading files (cat, head, tail, ls, wc, grep and git status, "
    "log, diff, show in the shell), writing the handoff document under ~/scratchpad, "
    "`agentihooks swarm sw expert mission`, `agentihooks swarm sw handoff <doc>`, "
    "`agentihooks swarm sw learned` and `agentihooks api` comment, say, leave and ack, each as one command."
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(recycle, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(recycle, "HANDOFF_MARGIN", 50)
    monkeypatch.setattr(recycle, "codex_context", lambda session_id: None)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 700_000)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", state="running", max_eng=1, max_ci=0))
    lineage = expert_lineage.start(found, "sw", GOOD, "/missions/batch.md", "xhigh", "operator", 1_000)
    found.put_agent("sw", AgentRecord(NAME, "expert", lineage.id, seat=lineage.seat, state="working"))
    found.memory.add_recap(lineage.seat, "expert@a1b2c3-0001", lineage.id, "## Done\nNone", 2_000)
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: found)
    return found


def _handoff(tmp_path: Path, text: str) -> dict[str, Any]:
    doc = tmp_path / "handoff.md"
    doc.write_text(text, encoding="utf-8")
    return {"command": f"agentihooks swarm sw handoff {doc}"}


@pytest.mark.feature("expert-handoff-contract")
def test_at_the_hard_gate_an_expert_is_told_the_expert_handoff_form() -> None:
    assert recycle.directive("s1", EXPERT) == HEAD + FORM


@pytest.mark.feature("expert-handoff-contract")
def test_in_the_preparation_window_an_expert_is_told_the_expert_handoff_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 600_000)
    told = recycle.directive("s1", EXPERT, now=datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc))
    assert told == (
        "HANDOFF PREPARATION — this session holds 600k tokens. Write your handoff now. "
        "Deadline: 2026-11-22 10:25 UTC, or before the 650k hard gate, whichever comes first. " + FORM
    )


@pytest.mark.parametrize("env", [ENGINEER, MASTER])
def test_a_worker_and_a_master_keep_the_handoff_v2_form(env: dict[str, str]) -> None:
    told = recycle.directive("s1", env)
    denied = recycle.gate("Bash", {"command": "git commit -m more"}, "s1", env)
    assert told is not None and denied is not None
    for text in (told, denied):
        assert "the line # Handoff v2, then ## Intent," in text
        assert "Expert handoff" not in text and "expert mission" not in text and "## Pending" not in text
    assert "`agentihooks swarm sw handoff <doc> [--recap <recap>]`" in denied


@pytest.mark.feature("expert-handoff-contract")
@pytest.mark.parametrize(
    "tool_name,tool_input",
    [
        ("Bash", {"command": "git commit -m more"}),
        ("Edit", {"file_path": "/home/u/dev/worktrees/repo/x/hooks/a.py", "old_string": "a", "new_string": "b"}),
        ("Agent", {"prompt": "keep going"}),
        ("Bash", {"command": "agentihooks swarm sw expert status"}),
        ("Bash", {"command": "agentihooks swarm sw expert mission expert-2"}),
        ("Bash", {"command": "agentihooks swarm sw expert stop expert-1 --note done"}),
        ("Bash", {"command": "agentihooks swarm other expert mission"}),
        ("Bash", {"command": "agentihooks swarm sw expert mission && git push"}),
        ("Bash", {"command": "agentihooks swarm sw expert"}),
    ],
)
def test_over_the_limit_an_experts_work_is_denied_with_the_expert_form_and_its_allowed_steps(
    tool_name: str, tool_input: dict[str, Any]
) -> None:
    assert recycle.gate(tool_name, tool_input, "s1", EXPERT) == "BLOCKED: " + HEAD + FORM + ALLOWED


@pytest.mark.feature("expert-handoff-contract")
def test_over_the_limit_an_expert_still_reads_its_mission() -> None:
    assert recycle.gate("Bash", {"command": "agentihooks swarm sw expert mission"}, "s1", EXPERT) is None
    assert recycle.gate("Read", {"file_path": "/home/u/dev/repo/hooks/a.py"}, "s1", EXPERT) is None
    assert (
        recycle.gate(
            "Bash", {"command": "agentihooks swarm sw learned 'ruff twice because CI runs both'"}, "s1", EXPERT
        )
        is None
    )


@pytest.mark.feature("expert-handoff-contract")
def test_over_the_limit_an_experts_handoff_is_checked_against_the_expert_contract_before_the_command(
    store: RedisStore, tmp_path: Path
) -> None:
    refused = recycle.gate("Bash", _handoff(tmp_path, V2), "s1", EXPERT)
    assert refused is not None
    assert refused.startswith(
        "BLOCKED: handoff refused; fix the document and run the command again:\n"
        "- the first line must be # Expert handoff\n"
    )
    assert f"\n- {OPENS}\n" in refused
    assert recycle.gate("Bash", _handoff(tmp_path, VALID), "s1", EXPERT) is None


def test_over_the_limit_a_workers_handoff_is_still_checked_against_handoff_v2(
    store: RedisStore, tmp_path: Path
) -> None:
    assert recycle.gate("Bash", _handoff(tmp_path, V2), "s1", ENGINEER) is None
    refused = recycle.gate("Bash", _handoff(tmp_path, VALID), "s1", ENGINEER)
    assert refused is not None
    assert refused.startswith(
        "BLOCKED: handoff refused; fix the document and run the command again:\n- the first line must be # Handoff v2\n"
    )
    assert "deliverable" not in refused and "Expert handoff" not in refused


def test_an_expert_the_swarm_store_cannot_place_is_left_to_the_handoff_command(
    store: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import ConnectionError as RedisDown

    stranger = {**EXPERT, "AGENTIHOOKS_AGENT_NAME": "expert@a1b2c3-0009"}
    assert recycle.gate("Bash", _handoff(tmp_path, V2), "s1", stranger) is None

    def refuse() -> RedisStore:
        raise SwarmError("the swarm store is unreachable")

    monkeypatch.setattr(recycle, "_resolver", lambda slug: lambda address: True)
    monkeypatch.setattr("scripts.swarm.store.connect", refuse)
    assert recycle.gate("Bash", _handoff(tmp_path, V2), "s1", EXPERT) is None

    def down(slug: str) -> list[AgentRecord]:
        raise RedisDown("connection lost")

    monkeypatch.setattr(store, "agents", down)
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: store)
    assert recycle.gate("Bash", _handoff(tmp_path, V2), "s1", EXPERT) is None


def test_the_gate_reads_an_expert_by_the_name_the_registry_resolves(
    store: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = {**EXPERT, "AGENTIHOOKS_AGENT_NAME": "expert@a1b2c3-0007"}
    monkeypatch.setattr(store.names, "resolve", lambda name, reader=None: NAME if name.endswith("-0007") else name)
    refused = recycle.gate("Bash", _handoff(tmp_path, V2), "s1", alias)
    assert refused is not None and "- the first line must be # Expert handoff\n" in refused
