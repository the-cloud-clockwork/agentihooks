"""Proof for task t2667: whether a swarm tick run with a joined hive member's bus settings sends the hive stop."""

import json
import warnings
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hive import execution_events, execution_stop, nats_grant, stop_names
from scripts.swarm import tick as tick_module
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.runtime.base import Outcome, Status
from tests.hive.test_nats_grant import _signed
from tests.hive.test_nats_grant import signer as signer

pytestmark = pytest.mark.unit

SWARM = "rig"
TICK_HOST = "m1"
OTHER = "h2"


def _member(signer: nats_grant.Signer, member: str) -> str:
    return nats_grant.mint(replace(signer, member_swarms={member: (SWARM,)}), member)


def _hub(signer: nats_grant.Signer) -> str:
    return _signed(signer, "hub", nats_grant.controller_claims())


def _bus(signer: nats_grant.Signer, credentials: str, provision: bool) -> Any:
    from scripts.events import nats

    return nats.open_bus(
        BusConfig(backend="nats", url=signer.url, credentials=credentials, provision=provision, scoped=True)
    )


def _agent(hive: str) -> AgentRecord:
    return AgentRecord(
        name="engineer",
        lane="eng",
        task="task",
        execution_id="execution",
        generation=3,
        seat="eng-1",
        account="a",
        hive=hive,
    )


def _start(signer: nats_grant.Signer, agent: AgentRecord, consumer_first: bool) -> None:
    started = Envelope(
        id="started:claim",
        type="execution.started",
        swarm=SWARM,
        entity="execution",
        key=agent.execution_id,
        version=agent.generation,
        time=100,
        author=agent.hive,
        data={
            "task": agent.task,
            "task_generation": 7,
            "claim_key": "claim",
            "claim_revision": 1,
            "generation": agent.generation,
            "lane": agent.lane,
            "hive": agent.hive,
            "name": agent.name,
            "pane_id": "p1",
        },
    )
    hub = _bus(signer, _hub(signer), provision=True)
    try:
        scoped = hub.for_swarm(SWARM)
        scoped.state("spoke-work").create(
            "claim", json.dumps({"state": "started", "claim_revision": 1, "event": started.to_dict()}).encode()
        )
        execution_events.started(hub, started)
        if consumer_first:
            scoped.stops.consumer(
                stop_names.durable(SWARM, agent.hive),
                execution_stop.stop_subject(SWARM, agent.hive),
                execution_stop.POLICY,
            )
    finally:
        hub.close()


def _tick_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, signer: nats_grant.Signer, credentials: str, bind: bool
) -> None:
    creds = tmp_path / "tick-bus.creds"
    creds.write_text(credentials)
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv("AGENTIHOOKS_BUS_URL", signer.url)
    monkeypatch.setenv("AGENTIHOOKS_BUS_CREDENTIALS", str(creds))
    monkeypatch.setenv("AGENTIHOOKS_BUS_BIND", "1" if bind else "0")
    monkeypatch.setenv("AGENTIHOOKS_BUS_SCOPED", "1")
    monkeypatch.setenv("AGENTIHOOKS_BUS_SWARMS", SWARM)


def _hive_runtime() -> Mock:
    runtime = Mock()
    runtime.find.return_value = Outcome("reap", Status.OK, "local", "pane")
    runtime.reap.return_value = Outcome("reap", Status.OK, "local", "pane")
    return runtime


def _received(signer: nats_grant.Signer, hive: str, runtime: Mock) -> list[str]:
    hub = _bus(signer, _hub(signer), provision=True)
    try:
        hub.for_swarm(SWARM).stops.consumer(
            stop_names.durable(SWARM, hive), execution_stop.stop_subject(SWARM, hive), execution_stop.POLICY
        )
    finally:
        hub.close()
    member = _bus(signer, _member(signer, hive), provision=False)
    try:
        return execution_stop.Stopper(member, SWARM, hive, runtime).poll(3000)
    finally:
        member.close()


CASES = {
    "member-bind-first-stop-to-another-hive": (True, OTHER, False),
    "member-bind-stop-to-another-hive-with-consumer": (True, OTHER, True),
    "member-bind-stop-to-its-own-hive": (True, TICK_HOST, True),
    "controller-first-stop-to-another-hive": (False, OTHER, False),
}


@pytest.mark.parametrize("case", list(CASES))
def test_a_host_tick_retirement_sends_and_the_hive_receives_the_stop(
    signer: nats_grant.Signer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    bind, hive, consumer_first = CASES[case]
    agent = _agent(hive)
    _start(signer, agent, consumer_first)
    credentials = _member(signer, TICK_HOST) if bind else _hub(signer)
    _tick_environment(monkeypatch, tmp_path, signer, credentials, bind)
    monkeypatch.setattr(tick_module.reaper, "scratch_homes", lambda slug, task: [])
    unreachable = SimpleNamespace(retire=lambda agent, **kwargs: False)

    retired = tick_module._retire_or_stop(SWARM, unreachable, agent, "idle")
    log = capsys.readouterr().err.strip()
    runtime = _hive_runtime()
    received = _received(signer, hive, runtime)
    reaped = [call.args for call in runtime.reap.call_args_list]
    warnings.warn(f"{case}: tick retired={retired}; tick log: {log}; hive {hive} received={received} reaped={reaped}")

    prefix = f"hub execution stop for engineer in swarm {SWARM} after idle: "
    if bind:
        assert (retired, received, reaped) == (False, [], [])
        assert log.startswith(f"{prefix}{execution_stop.FAILED} (")
    else:
        assert (retired, log, received, reaped) == (
            True,
            f"{prefix}{execution_stop.SENT}",
            [execution_stop.STOPPED],
            [("engineer",)],
        )
