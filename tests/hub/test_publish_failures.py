from types import SimpleNamespace
from unittest import mock

import pytest

from scripts.events.config import BusConfig
from scripts.events.factory import open_bus
from scripts.hub import registry
from tests.hub.test_runner import event, hub

pytestmark = pytest.mark.unit


def test_refused_publish_and_failed_nak_do_not_stop_other_reactors(capsys):
    bus = open_bus(BusConfig())
    refused = mock.Mock(side_effect=OSError("publish refused"))
    healthy = mock.Mock()
    hosted = hub(
        bus,
        [
            registry.Reactor("refused", "swarm.*.task.>", refused),
            registry.Reactor("healthy", "swarm.*.task.>", healthy),
        ],
    )
    failed = SimpleNamespace(event=event("e1", 1), attempt=1, nak=mock.Mock(side_effect=OSError("nak refused")))
    hosted._consumers["refused"].fetch = mock.Mock(return_value=[failed])
    bus.events.publish("swarm.s.task.claimed", event("e1", 1))
    assert hosted.run_once(timeout_ms=0) == {"refused": ["failed"], "healthy": ["handled"]}
    healthy.assert_called_once_with(event("e1", 1))
    err = capsys.readouterr().err
    assert "publish refused" in err
    assert "hub: reactor refused transport failed\n" in err
    assert hosted.run_once(timeout_ms=0)["healthy"] == []
    hosted.stop()


def test_stop_logs_a_refused_release_and_releases_the_other_leases(capsys):
    hosted = hub(open_bus(BusConfig()), [])
    failed = mock.Mock(release=mock.Mock(side_effect=OSError("release refused")))
    healthy = mock.Mock()
    hosted._leases = {"failed": failed, "healthy": healthy}
    hosted.stop()
    healthy.release.assert_called_once_with()
    err = capsys.readouterr().err
    assert "release refused" in err
    assert "hub: reactor failed lease release failed\n" in err


def test_failed_transport_passes_wait_before_retrying(monkeypatch):
    from scripts.hub import runner

    hosted = mock.Mock()
    hosted.run_once.side_effect = [{"failed": ["failed"]}, KeyboardInterrupt]
    wait = mock.Mock()
    monkeypatch.setattr(runner, "scheduled_wait", wait)
    with pytest.raises(KeyboardInterrupt):
        runner._run(hosted, False)
    wait.assert_called_once_with(runner.IDLE_S)


def test_hub_command_enables_reconnect_on_its_nats_bus(monkeypatch):
    from scripts.hub import runner

    configs = []

    def opened(config):
        configs.append(config)
        raise OSError("configuration captured")

    monkeypatch.setattr(runner, "open_bus", opened)
    monkeypatch.setattr(runner, "_issued_credential", lambda holder: None)
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv("AGENTIHOOKS_BUS_URL", "nats://127.0.0.1:4222")
    with pytest.raises(OSError, match="configuration captured"):
        runner.main(["run", "--once"])
    assert len(configs) == 1
    assert configs[0].reconnect is True
    assert configs[0].url == "nats://127.0.0.1:4222"
