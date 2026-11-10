from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm_v2.api import health

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.feature("controller-busy-node-probes")
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
@pytest.mark.feature("controller-busy-node-probes")
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
    failure_callback = Mock()
    assert options["retry"].call_with_retry(lambda: True, failure_callback) is True
    failure_callback.assert_not_called()
    assert options["health_check_interval"] == 0
    assert options["retry_on_timeout"] is False
    assert options["retry_on_error"] == []
    client_factory.assert_called_once_with(connection_pool=pool)
    bounded.ping.assert_called_once_with()
    pool.disconnect.assert_called_once_with()


@pytest.mark.feature("controller-busy-node-probes")
def test_readiness_deadline_covers_stalled_setup_without_spawning_more_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm_v2.api import readiness

    completion = Mock()
    completion.wait.return_value = False
    thread = Mock()
    threads = Mock(return_value=thread)
    client = Mock()
    monkeypatch.setattr(readiness.threading, "Event", Mock(return_value=completion))
    monkeypatch.setattr(readiness.threading, "Thread", threads)
    ping = Mock(return_value=True)
    monkeypatch.setattr(readiness, "available", ping)
    checker = Mock()
    checker.store.redis = client
    api = health.HealthAPI(checker)
    running = api.readiness.running

    def acquire(*, blocking: bool) -> bool:
        assert blocking is False
        return running.acquire(blocking=False)

    monkeypatch.setattr(api.readiness, "running", SimpleNamespace(acquire=acquire, release=running.release))

    assert api.route("GET", "/v2/health/ready", "", None) == (503, {"redis": "unavailable"})
    completion.wait.assert_called_once_with(4)
    thread.start.assert_called_once_with()
    assert threads.call_args.kwargs["daemon"] is True
    assert api.route("GET", "/v2/health/ready", "", None) == (503, {"redis": "unavailable"})
    assert threads.call_count == 1
    ping.assert_not_called()

    threads.call_args.kwargs["target"]()
    ping.assert_called_once_with(client)
    completion.set.assert_called_once_with()
    completion.wait.return_value = True
    threads.side_effect = lambda **kwargs: SimpleNamespace(start=kwargs["target"])

    assert api.route("GET", "/v2/health/ready", "", None) == (200, {"redis": "ok"})
    assert threads.call_count == 2
    assert api.route("GET", health.LIVE_PATH, "", None)[0] == 200
