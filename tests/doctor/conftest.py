import pytest

from scripts.doctor import features, registry

READ_AT = "2026-10-07T19:06:40.000Z"


@pytest.fixture
def read_clock(monkeypatch):
    monkeypatch.setattr(registry, "wall", lambda: 1_791_400_000.0)
    return READ_AT


@pytest.fixture(autouse=True)
def speed_every_tick(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_DOCTOR_SPEED_MINUTES", "0")


@pytest.fixture(autouse=True)
def no_feature_map(tmp_path, monkeypatch):
    monkeypatch.setattr(features, "MAP", tmp_path / "no-feature-map.json")
