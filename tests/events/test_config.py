import pytest

from scripts.events import config
from scripts.events.config import BusConfig, QueuePolicy

pytestmark = pytest.mark.unit


def test_queue_policy_defaults():
    policy = QueuePolicy()
    assert (policy.max_deliver, policy.ack_wait_ms, policy.backoff_ms) == (5, 30_000, ())


def test_queue_policy_keeps_a_backoff_no_longer_than_its_deliveries():
    policy = QueuePolicy(max_deliver=3, ack_wait_ms=1, backoff_ms=(0, 1000, 5000))
    assert (policy.max_deliver, policy.ack_wait_ms, policy.backoff_ms) == (3, 1, (0, 1000, 5000))


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "3"])
def test_queue_policy_refuses_a_max_deliver_below_one(value):
    with pytest.raises(ValueError, match=r"^queue max deliver must be a positive integer$"):
        QueuePolicy(max_deliver=value)


@pytest.mark.parametrize("value", [0, -1, 1.5, False])
def test_queue_policy_refuses_an_ack_wait_below_one(value):
    with pytest.raises(ValueError, match=r"^queue ack wait must be a positive integer$"):
        QueuePolicy(ack_wait_ms=value)


@pytest.mark.parametrize("backoff", [[0], (-1,), (1.5,), (True,), ("1",)])
def test_queue_policy_refuses_a_backoff_that_is_not_a_tuple_of_counts(backoff):
    with pytest.raises(ValueError, match=r"^queue backoff must be a tuple of non negative integers$"):
        QueuePolicy(backoff_ms=backoff)


def test_queue_policy_refuses_a_backoff_longer_than_its_deliveries():
    with pytest.raises(ValueError, match=r"^queue backoff has 3 steps for 2 deliveries$"):
        QueuePolicy(max_deliver=2, backoff_ms=(0, 0, 0))


def test_bus_config_defaults():
    bus = BusConfig()
    assert (bus.backend, bus.url, bus.dedupe_window_ms, bus.queue) == ("memory", None, 120_000, QueuePolicy())


@pytest.mark.parametrize("backend", ["", None, 3])
def test_bus_config_refuses_a_backend_that_is_not_a_name(backend):
    with pytest.raises(ValueError, match=r"^bus backend must be non empty text$"):
        BusConfig(backend=backend)


@pytest.mark.parametrize("url", ["", 3])
def test_bus_config_refuses_a_url_that_is_not_text(url):
    with pytest.raises(ValueError, match=r"^bus url must be non empty text or None$"):
        BusConfig(url=url)


@pytest.mark.parametrize("value", [0, -1, True])
def test_bus_config_refuses_a_dedupe_window_below_one(value):
    with pytest.raises(ValueError, match=r"^bus dedupe window must be a positive integer$"):
        BusConfig(dedupe_window_ms=value)


def test_bus_config_refuses_a_queue_that_is_not_a_policy():
    with pytest.raises(ValueError, match=r"^bus queue must be a QueuePolicy$"):
        BusConfig(queue={})


def test_bus_config_reads_the_bus_setting():
    assert config.BACKEND_ENV == "AGENTIHOOKS_BUS"
    assert config.URL_ENV == "AGENTIHOOKS_BUS_URL"
    bus = BusConfig.from_environ({"AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_URL": "nats://hub:4222"})
    assert (bus.backend, bus.url) == ("nats", "nats://hub:4222")


@pytest.mark.parametrize("environ", [{}, {"AGENTIHOOKS_BUS": "", "AGENTIHOOKS_BUS_URL": ""}])
def test_bus_config_falls_back_to_the_default_backend_without_a_url(environ):
    bus = BusConfig.from_environ(environ)
    assert (bus.backend, bus.url) == ("memory", None)
