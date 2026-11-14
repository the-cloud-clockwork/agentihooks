from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit

MINUTE = 60_000


@pytest.fixture(autouse=True)
def outage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import outage

    monkeypatch.setattr(outage, "HOME", tmp_path)


def _authority(agent: Any) -> dict[str, Any]:
    return {
        "state": "active",
        "holder": agent.name,
        "execution_id": agent.execution_id,
        "execution_generation": agent.generation,
        "generation": 1,
        "lease_deadline_ms": 20 * MINUTE,
    }


def _owner(store: Any) -> tuple[Any, Any, Any]:
    from scripts.swarm.tick import tick
    from tests.helpers.seat import seat_owner
    from tests.swarm.test_outage_resume import RecoveringRuntime
    from tests.swarm.test_tick import tasks

    ledger = tasks(("t1", "eng"))
    runtime = RecoveringRuntime()
    seat_owner(store, ledger, runtime, "t1", 1_000)
    tick("sw", store, ledger, runtime, 1_000)
    store.update("sw", state="paused")
    agent = next(agent for agent in store.agents("sw") if agent.lane == "eng")
    store.put_agent("sw", replace(agent, conversation_id="conversation-0", profile="engineer"))
    runtime.statuses[agent.name] = "idle"
    return ledger, runtime, next(found for found in store.agents("sw") if found.name == agent.name)


def _sections_read(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    from scripts.tick.adapters.records_redis import RedisRecords

    real = RedisRecords.sections
    asked: list[tuple[str, ...]] = []

    def counted(self: RedisRecords, slug: str, names: Any) -> Any:
        asked.append(tuple(names))
        return real(self, slug, names)

    monkeypatch.setattr(RedisRecords, "sections", counted)
    return asked


@pytest.mark.feature("track-outages-claims-only")
def test_read_claims_answers_the_claims_section_without_the_archive(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import fencing

    claim = {"state": "active", "holder": "eng", "generation": 3}
    fencing.write(store, "sw", lambda doc: doc["claims"].update({"t1": claim}))
    fencing.migrate_records(store, "sw", writers_upgraded=True)
    asked = _sections_read(monkeypatch)

    assert fencing.read_claims(store, "sw") == {"t1": claim}
    assert asked == [("claims",)]
    assert fencing.read_claims(store, "sw") == fencing.read(store, "sw")["claims"]


@pytest.mark.feature("track-outages-claims-only")
def test_read_claims_on_a_document_swarm_answers_its_claims(store: Any) -> None:
    from scripts.swarm import fencing

    claim = {"state": "active", "holder": "eng", "generation": 2}
    fencing.write(store, "sw", lambda doc: doc["claims"].update({"t9": claim}))

    assert fencing.read_claims(store, "sw") == {"t9": claim}
    assert fencing.read_claims(store, "sw") == fencing.read(store, "sw")["claims"]


@pytest.mark.feature("track-outages-claims-only")
def test_track_outages_judges_owners_without_reading_the_whole_fencing_document(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import fencing, outage, resume

    ledger, runtime, agent = _owner(store)
    fencing.write(store, "sw", lambda doc: doc["claims"].update({agent.task: _authority(agent)}))
    fencing.migrate_records(store, "sw", writers_upgraded=True)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    rows = {task["id"]: task for task in ledger.state("sw")["tasks"]}
    asked = _sections_read(monkeypatch)

    def whole(found_store: Any, slug: str) -> dict[str, Any]:
        raise AssertionError(f"track outages read the whole fencing document of {slug}")

    monkeypatch.setattr(fencing, "read", whole)
    resume.track_outages("sw", store, runtime, rows, 8 * MINUTE)

    assert resume.recovery_pending(store, "sw", agent)
    assert ("claims",) in asked
    assert all("archive" not in names for names in asked)
    assert resume._recovery_identity(store, "sw", agent) == [
        agent.task,
        agent.seat,
        agent.execution_id,
        agent.generation,
        1,
    ]
