import pytest


@pytest.fixture(autouse=True)
def _serena_router_up(monkeypatch):
    from scripts import serena_router_daemon

    monkeypatch.setattr(serena_router_daemon, "ensure", lambda: True)
