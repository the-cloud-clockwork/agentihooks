import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from nats.errors import TimeoutError as NatsTimeout

from scripts.events import nats
from scripts.events.config import QueuePolicy


@pytest.mark.parametrize("error", [TimeoutError, NatsTimeout])
def test_an_expired_hub_pull_returns_no_deliveries(error):
    subscription = Mock(fetch=AsyncMock(side_effect=error))
    consumer = nats.NatsConsumer(Mock(run=asyncio.run), subscription, Mock(), QueuePolicy(), "readiness")

    assert consumer.fetch(64, 1) == []
    subscription.fetch.assert_awaited_once_with(64, timeout=0.01)


def test_a_hub_pull_preserves_non_timeout_errors():
    subscription = Mock(fetch=AsyncMock(side_effect=ValueError("invalid consumer")))
    consumer = nats.NatsConsumer(Mock(run=asyncio.run), subscription, Mock(), QueuePolicy(), "readiness")

    with pytest.raises(ValueError, match="invalid consumer"):
        consumer.fetch(64, 1)
