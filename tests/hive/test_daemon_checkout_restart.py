"""The root hive ends its run between beats when its checkout moved, so its unit starts it again on the new code."""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.hive import daemon, dispatch

CHECKOUT = Path(daemon.__file__).resolve().parents[2]


def _hive(
    monkeypatch: pytest.MonkeyPatch, revisions: list[str], wait: Mock, busy: list[bool] | None = None
) -> tuple[list[Path], list[str]]:
    """Run the beat loop on stubs: each revision read takes the next of revisions, each beat records the hive id and
    each order worker check after a beat takes the next of busy, idle when busy is not given."""
    roots: list[Path] = []
    beats: list[str] = []
    readings = iter(revisions)
    workers = iter(busy) if busy is not None else None

    def revision(root: Path) -> str:
        roots.append(root)
        return next(readings)

    monkeypatch.delenv("HIVE_READY_FILE", raising=False)
    monkeypatch.setattr(daemon, "revision", revision)
    monkeypatch.setattr(daemon.dispatch, "busy", lambda: next(workers) if workers is not None else False)
    monkeypatch.setattr(daemon, "hive_id", lambda: "root")
    monkeypatch.setattr(daemon, "beat", lambda redis, hive: beats.append(hive))
    monkeypatch.setattr(daemon.quota_refresh, "refresh", lambda environ: None)
    monkeypatch.setattr(daemon.quota_refresh, "start", lambda environ, worker, attempted_at: (None, attempted_at))
    monkeypatch.setattr(daemon.github_app_cli, "renew", lambda renewed, now: renewed)
    monkeypatch.setattr(daemon.bundle_refresh, "refresh", lambda last, now, environ: last)
    monkeypatch.setattr(daemon, "timer_state", lambda waiter: waiter)
    monkeypatch.setattr(daemon, "scheduled_wait", wait)
    return roots, beats


def _connect() -> Mock:
    connection = object()
    return Mock(side_effect=[(connection, connection), AssertionError("reconnected")])


@pytest.mark.feature("hive-restart-on-checkout-move")
def test_a_moved_checkout_ends_the_hive_run_after_its_beat(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wait = Mock(return_value=None)
    roots, beats = _hive(monkeypatch, ["loaded", "loaded", "moved"], wait)
    daemon.run(_connect())
    assert beats == ["root", "root"]
    assert wait.call_count == 1
    assert roots == [CHECKOUT, CHECKOUT, CHECKOUT]
    assert (
        f"the hive {os.getpid()} restarts on the new code: the checkout moved from loaded to moved"
        in capsys.readouterr().err.splitlines()
    )


@pytest.mark.feature("hive-restart-on-checkout-move")
def test_an_unchanged_checkout_keeps_the_hive_beating(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wait = Mock(side_effect=[None, None, KeyboardInterrupt])
    roots, beats = _hive(monkeypatch, ["loaded"] * 4, wait)
    with pytest.raises(KeyboardInterrupt):
        daemon.run(_connect())
    assert beats == ["root", "root", "root"]
    assert wait.call_count == 3
    assert roots == [CHECKOUT] * 4
    assert "restarts on the new code" not in capsys.readouterr().err


@pytest.mark.feature("hive-restart-on-checkout-move")
def test_an_unreadable_checkout_keeps_the_hive_beating(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wait = Mock(side_effect=[None, KeyboardInterrupt])
    _, beats = _hive(monkeypatch, ["loaded", "", "loaded"], wait)
    with pytest.raises(KeyboardInterrupt):
        daemon.run(_connect())
    assert beats == ["root", "root"]
    assert "restarts on the new code" not in capsys.readouterr().err


@pytest.mark.feature("hive-restart-on-checkout-move")
def test_a_checkout_unread_at_start_restarts_the_hive_once_it_reads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wait = Mock(side_effect=AssertionError("waited after the checkout became readable"))
    _, beats = _hive(monkeypatch, ["", "abc"], wait)
    daemon.run(_connect())
    assert beats == ["root"]
    assert (
        f"the hive {os.getpid()} restarts on the new code: the checkout moved from  to abc"
        in capsys.readouterr().err.splitlines()
    )


@pytest.mark.feature("hive-restart-on-checkout-move")
def test_a_moved_checkout_waits_for_running_order_workers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wait = Mock(return_value=None)
    roots, beats = _hive(monkeypatch, ["loaded", "moved"], wait, busy=[True, False])
    daemon.run(_connect())
    assert beats == ["root", "root"]
    assert wait.call_count == 1
    assert roots == [CHECKOUT, CHECKOUT]
    assert capsys.readouterr().err.count("restarts on the new code") == 1


@pytest.mark.feature("hive-restart-on-checkout-move")
def test_busy_counts_only_order_workers_still_running(monkeypatch: pytest.MonkeyPatch) -> None:
    running, finished = Mock(), Mock()
    running.poll.return_value = None
    finished.poll.return_value = 0
    monkeypatch.setattr(dispatch, "_workers", {("root", "1-0"): finished})
    assert dispatch.busy() is False
    monkeypatch.setattr(dispatch, "_workers", {("root", "1-0"): finished, ("root", "2-0"): running})
    assert dispatch.busy() is True
    monkeypatch.setattr(dispatch, "_workers", {})
    assert dispatch.busy() is False
