import pytest

from scripts.doctor import ci_speed_read, cli, loop
from tests.doctor.test_ci_speed import NOW
from tests.doctor.test_ci_speed_timer import DOCTOR, snapshot, store  # noqa: F401

pytestmark = pytest.mark.unit
MINUTE = 60000


def test_the_speed_read_runs_once_per_speed_interval_not_every_tick(store, monkeypatch):  # noqa: F811
    monkeypatch.setenv(loop.SPEED_ENV, "5")
    reads = []
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: reads.append(args) or snapshot())
    for minute in range(11):
        cli.timer(store, DOCTOR, NOW + minute * MINUTE, False)
    assert len(reads) == 3


def test_the_default_speed_interval_is_five_minutes(store, monkeypatch):  # noqa: F811
    monkeypatch.delenv(loop.SPEED_ENV)
    assert loop.speed_due(store, DOCTOR, NOW)
    assert not loop.speed_due(store, DOCTOR, NOW + 5 * MINUTE - 1)
    assert loop.speed_due(store, DOCTOR, NOW + 5 * MINUTE)


def test_the_speed_read_time_is_kept_under_the_doctor_key(store, monkeypatch):  # noqa: F811
    monkeypatch.delenv(loop.SPEED_ENV)
    assert loop.speed_due(store, DOCTOR, NOW)
    assert int(store.redis.get(f"{store.key(DOCTOR)}:ci-speed-read-at")) == NOW
