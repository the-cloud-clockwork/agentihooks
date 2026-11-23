import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.handoff import transfers
from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, RedisStore
from tests.handoff.test_expert_contract import VALID as HANDOFF
from tests.helpers.seat import seat
from tests.swarm import expert_fixture
from tests.swarm.expert_fixture import store
from tests.swarm.test_cli import env, started
from tests.swarm.test_expert_seat import MINUTE, NEXT, NOW, handed_off, launched
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.unit
_fixture = env, seat, store
DOC = "# Handoff v2\n## Next\nRead the saved proof.\n## Read first\nNone\n"


@pytest.mark.feature("swarm-handoff-continuity")
def test_confirmation_command_records_continuity_but_never_infers_binding(
    env: tuple[RedisStore, Any, FakeRuntime], seat: Callable[..., AgentRecord], capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger, runtime = env
    cli.main(["sw", "create", "--repo", "/repo"])
    agent = next(a for a in started(env, seat) if a.lane == "eng")
    row = transfers.record(store, "sw", agent, "recycle", DOC, 1)
    transfers.attach(store, "sw", agent)
    assert cli.main(["sw", "--as", agent.name, "confirm-handoff", row["id"], "--next", "Read the saved proof."]) == 0
    capsys.readouterr()
    assert cli.main(["sw", "status", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)["transfers"][0]
    assert result["continuity"]["state"] == "confirmed"
    assert result["binding"]["state"] == "pending"
    runtime.live.discard(agent.name)
    assert cli.main(["sw", "--as", agent.name, "confirm-handoff", row["id"], "--next", "Read the saved proof."]) == 1


@pytest.mark.feature("expert-seat-step")
def test_an_experts_confirmation_prints_the_transfer_and_takes_the_handoff_off_its_lineage_seat(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expert_fixture.started(store)
    runtime = FakeRuntime()
    row = handed_off(store, launched(store, runtime))
    second = launched(store, runtime, NOW + MINUTE)
    expert_fixture.acting(monkeypatch, second.name)
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: SimpleNamespace(live_names=lambda: set()))
    assert cli.main(["sw", "confirm-handoff", row["id"], "--next", NEXT]) == 1
    assert "Only a live successor can confirm its handoff" in capsys.readouterr().err
    assert store.handoff("sw", "expert-1") == HANDOFF
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: SimpleNamespace(live_names=lambda: {second.name}))
    assert cli.main(["sw", "confirm-handoff", row["id"], "--next", NEXT]) == 0
    printed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (printed["id"], printed["successor"]) == (row["id"], second.name)
    confirmed = printed["continuity"]
    assert (confirmed["state"], confirmed["by"], confirmed["next"]) == ("confirmed", second.name, NEXT)
    assert isinstance(confirmed["at"], int)
    assert store.handoff("sw", "expert-1") == ""


@pytest.mark.feature("swarm-restore-decision")
def test_a_worker_cannot_decide_another_seats_restore(
    env: tuple[RedisStore, Any, FakeRuntime], seat: Callable[..., AgentRecord], capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["sw", "create", "--repo", "/repo"])
    agent = next(a for a in started(env, seat) if a.lane == "eng")
    assert cli.main(["sw", "--as", agent.name, "restore-decision", agent.name, "fresh"]) == 1
    assert "Only the master or operator" in capsys.readouterr().err
