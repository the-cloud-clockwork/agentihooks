from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from scripts.events import nats
from scripts.events.config import BusConfig
from scripts.hive import nats_grant
from scripts.hive.spoke import Spoke
from scripts.swarm import readiness
from tests.hive.test_nats_grant import _user_creds
from tests.hive.test_nats_grant import signer as signer
from tests.hive.test_spoke import reading
from tests.hive.test_spoke import rig as rig
from tests.hub.test_readiness import source

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("lane", ["eng", "ci", "plan"])
def test_chart_hub_offer_is_acknowledged_by_a_bound_scoped_spoke(signer, rig, monkeypatch, lane):
    chart = Path(__file__).resolve().parents[2] / "deploy/helm/agentihooks-swarm"
    values = yaml.safe_load((chart / "values.yaml").read_text())["hub"]["bus"]
    hub_env = {"AGENTIHOOKS_BUS": values["backend"], "AGENTIHOOKS_BUS_URL": signer.url}
    if values["scoped"]:
        hub_env["AGENTIHOOKS_BUS_SCOPED"] = "1"
    hub = nats.open_bus(replace(BusConfig.from_environ(hub_env), credentials=_user_creds(signer.account, "hub")))
    configured = replace(signer, member_swarms={"m1": ("rig",)})
    hive_env = {
        "AGENTIHOOKS_BUS": "nats",
        "AGENTIHOOKS_BUS_URL": signer.url,
        "AGENTIHOOKS_BUS_SCOPED": "1",
        "AGENTIHOOKS_BUS_BIND": "1",
        "AGENTIHOOKS_BUS_CREDENTIALS": nats_grant.mint(configured, "m1"),
    }
    client = nats.open_bus(BusConfig.from_environ(hive_env))
    try:
        monkeypatch.setattr("scripts.swarm.release_wait.checker", lambda _: lambda task: True)
        monkeypatch.setattr("scripts.swarm.apply_wait.checker", lambda *_: lambda task: True)
        row, store, ledger = source()
        row["lane"] = lane
        view = readiness.Source(store, ledger).snapshot("rig")
        offered = readiness.publish(hub, view, 12)
        assert offered == ["ready:rig:task-a:3"]
        own = hub.for_swarm("rig")
        audit = own.events.consumer("audit", "swarm.rig.execution.started")
        config = replace(rig.config, lane=lane, policy=hub._config.queue)
        spoke = Spoke(config, client, rig.runtime, rig.tasks, reading)
        assert spoke.poll(1000) == "started"
        rig.runtime.spawn.assert_called_once()
        [started] = audit.fetch(1, 1000)
        assert (started.event.key, started.event.version) == ("execution-a", 7)
        started.ack()
        queue = own.queue(lane, config.policy)
        assert queue.take(50) is None
        info = hub._loop.run(hub._js.consumer_info(queue._stream, lane))
        assert info.ack_floor.consumer_seq == 1
        assert info.num_ack_pending == 0
    finally:
        client.close()
        hub.close()
