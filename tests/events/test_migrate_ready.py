import importlib
import json

import pytest

from scripts.events.config import CREDENTIALS_ENV, URL_ENV, QueuePolicy
from scripts.events.envelope import Envelope
from tests.events.test_nats_migration import buses as buses
from tests.hive.test_nats_grant import signer as signer

pytestmark = pytest.mark.unit


def test_command_inventories_then_keeps_every_ready_offer(buses, monkeypatch, capsys):
    legacy, scoped = buses
    policy = QueuePolicy()
    offers = {}
    for swarm, lane, count in [("doctor", "ci", 7), ("doctor", "eng", 3), ("rig", "plan", 2)]:
        queue = legacy.queue(f"swarm.{swarm}.ready.{lane}", policy)
        offers[swarm, lane] = [
            Envelope(f"{swarm}-{lane}-{index}", "task.ready", swarm, "task", str(index), 1, 0, "hub", {"lane": lane})
            for index in range(count)
        ]
        for offer in offers[swarm, lane]:
            queue.offer(offer)
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv(URL_ENV, scoped._config.url)
    monkeypatch.setenv(CREDENTIALS_ENV, scoped._config.credentials)
    monkeypatch.delenv("AGENTIHOOKS_BUS_BIND", raising=False)
    command = importlib.import_module("scripts.events.migrate_ready")

    assert command.main([]) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert inventory["retained"] == 12
    assert inventory["kept"] == 0
    assert inventory["applied"] is False
    assert command.main(["--apply", "--quiescent"]) == 0
    moved = json.loads(capsys.readouterr().out)
    assert moved["retained"] == moved["kept"] == 12
    assert moved["remaining"] == 0
    assert {(row["swarm"], row["lane"], row["kept"]) for row in moved["queues"]} == {
        ("doctor", "ci", 7),
        ("doctor", "eng", 3),
        ("rig", "plan", 2),
    }
    assert command.main(["--apply", "--quiescent"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["retained"] == repeated["kept"] == repeated["remaining"] == 0
    for (swarm, lane), expected in offers.items():
        target = scoped.for_swarm(swarm).queue(lane, policy)
        received = []
        for _ in expected:
            delivery = target.take(1000)
            assert delivery is not None
            received.append(delivery.event)
            delivery.ack()
        assert received == expected
        assert target.take(20) is None
