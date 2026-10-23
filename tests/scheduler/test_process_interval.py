from unittest.mock import Mock, call

import pytest


def test_an_interval_runs_its_callback_until_cancelled(monkeypatch):
    from scripts.scheduler import process_interval

    callback = Mock()
    interval = process_interval.Interval(callback, every=15)
    wait = Mock(side_effect=[False, False, True])
    monkeypatch.setattr(interval._stop, "wait", wait)
    interval._run()
    assert wait.call_args_list == [call(15), call(15), call(15)]
    assert callback.call_args_list == [call(), call()]
    assert interval.check() is None


def test_an_interval_starts_and_joins_its_owned_worker(monkeypatch):
    from scripts.scheduler import process_interval

    worker = Mock()
    worker.is_alive.return_value = False
    factory = Mock(return_value=worker)
    monkeypatch.setattr(process_interval.threading, "Thread", factory)
    interval = process_interval.Interval(Mock(), every=15)
    factory.assert_called_once_with(target=interval._run, daemon=True)
    assert interval.start() is None
    worker.start.assert_called_once_with()
    assert interval.close() is None
    assert interval._stop.is_set()
    worker.join.assert_called_once_with(timeout=15)


def test_callback_errors_reach_the_health_check_and_close(monkeypatch):
    from scripts.scheduler import process_interval

    failure = RuntimeError("callback failed")
    interval = process_interval.Interval(Mock(side_effect=failure), every=15)
    monkeypatch.setattr(interval._stop, "wait", Mock(side_effect=[False, True]))
    interval._run()
    with pytest.raises(RuntimeError) as raised:
        interval.check()
    assert raised.value is failure
    interval._worker = Mock()
    interval._worker.is_alive.return_value = False
    with pytest.raises(RuntimeError) as raised:
        interval.close()
    assert raised.value is failure
    assert interval._stop.is_set()
    interval._worker.join.assert_called_once_with(timeout=15)


def test_close_refuses_a_worker_that_did_not_stop():
    from scripts.scheduler import process_interval

    interval = process_interval.Interval(Mock(), every=15)
    interval._worker = Mock()
    interval._worker.is_alive.return_value = True
    with pytest.raises(RuntimeError) as raised:
        interval.close()
    assert str(raised.value) == "scheduled interval did not stop"
    assert interval._stop.is_set()
    interval._worker.join.assert_called_once_with(timeout=15)


@pytest.mark.parametrize("every", [0, -1, float("nan"), float("inf")])
def test_an_interval_requires_a_positive_finite_delay(every):
    from scripts.scheduler import process_interval

    with pytest.raises(ValueError) as raised:
        process_interval.Interval(Mock(), every=every)
    assert str(raised.value) == "an interval must be positive and finite"


def test_a_real_interval_stops_without_waiting_for_its_next_due_time():
    from scripts.scheduler import process_interval

    interval = process_interval.Interval(Mock(), every=60)
    interval.start()
    assert interval._worker.is_alive()
    interval.close()
    assert not interval._worker.is_alive()
