import pytest

from scripts.doctor import registry

READ_AT = "2026-10-07T19:06:40.000Z"


@pytest.fixture
def read_clock(monkeypatch):
    monkeypatch.setattr(registry, "wall", lambda: 1_791_400_000.0)
    return READ_AT
