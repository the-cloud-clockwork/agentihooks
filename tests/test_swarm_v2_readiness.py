from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm_v2.api import health

pytestmark = [pytest.mark.unit, pytest.mark.feature("controller-busy-node-probes")]


@pytest.mark.parametrize("available", [True, False])
def test_readiness_is_open_and_reports_redis_without_deep_diagnostics(available: bool) -> None:
    from scripts.swarm_v2.api import readiness

    checker = Mock()
    checker.store.redis = Mock()
    api = health.HealthAPI(checker, deep=False)
    with pytest.MonkeyPatch.context() as patch:
        ping = Mock(return_value=available)
        patch.setattr(readiness, "available", ping)
        assert api.route("GET", "/v2/health/ready", "None", None) == (
            200 if available else 503,
            {"redis": "ok" if available else "unavailable"},
        )
    ping.assert_called_once_with(checker.store.redis)
    checker.diagnose.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_readiness_redis_uses_bounded_sockets_without_retries(failure: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    import redis

    from scripts.swarm_v2.api import readiness

    pool = Mock()
    factory = Mock(return_value=pool)
    bounded = Mock()
    if failure:
        bounded.ping.side_effect = redis.TimeoutError("private connection details")
    else:
        bounded.ping.return_value = True
    client_factory = Mock(return_value=bounded)
    monkeypatch.setattr(redis, "ConnectionPool", factory)
    monkeypatch.setattr(redis, "Redis", client_factory)
    source = SimpleNamespace(
        connection_pool=SimpleNamespace(connection_class=object, connection_kwargs={"host": "redis"})
    )

    assert readiness.available(source) is (not failure)

    options = factory.call_args.kwargs
    assert options["connection_class"] is object
    assert options["host"] == "redis"
    assert options["socket_timeout"] == 2
    assert options["socket_connect_timeout"] == 2
    assert options["retry"].get_retries() == 0
    assert options["health_check_interval"] == 0
    assert options["retry_on_timeout"] is False
    assert options["retry_on_error"] == []
    client_factory.assert_called_once_with(connection_pool=pool)
    bounded.ping.assert_called_once_with()
    pool.disconnect.assert_called_once_with()
