import pytest


@pytest.fixture(autouse=True)
def _serena_router_up(monkeypatch):
    from scripts import serena_router_daemon

    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: True)


@pytest.fixture(autouse=True)
def _no_headroom_thread(monkeypatch):
    from scripts.hive import daemon

    monkeypatch.setattr(daemon, "publish_headroom", lambda redis, waiter, local_id, stop: None)
    monkeypatch.setattr(daemon, "publish_herdr_stats", lambda redis, waiter, local_id, stop: None)


@pytest.fixture(autouse=True)
def _no_bus_scheduler_state(monkeypatch):
    from scripts.events import scheduler_state

    monkeypatch.setattr(scheduler_state, "open_scheduler_state", lambda bucket: pytest.fail(f"opened bus {bucket}"))
