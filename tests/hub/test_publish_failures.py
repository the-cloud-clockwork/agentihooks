from types import SimpleNamespace
from unittest import mock

import pytest

from scripts.events import BusConfig, open_bus
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
    assert "publish refused" in capsys.readouterr().err
    assert hosted.run_once(timeout_ms=0)["healthy"] == []
    hosted.stop()


def test_stop_logs_a_refused_release_and_releases_the_other_leases(capsys):
    hosted = hub(open_bus(BusConfig()), [])
    failed = mock.Mock(release=mock.Mock(side_effect=OSError("release refused")))
    healthy = mock.Mock()
    hosted._leases = {"failed": failed, "healthy": healthy}
    hosted.stop()
    healthy.release.assert_called_once_with()
    assert "release refused" in capsys.readouterr().err
