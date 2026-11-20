import asyncio
import concurrent.futures
import queue
import subprocess
import threading
from concurrent.futures import Future, wait
from threading import Event

import pytest

HELD_S = 2


def test_the_tick_reaches_its_snapshot_step(entered: threading.Event) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    assert entered.wait(5)


def test_a_wait_with_a_message_and_a_keyword_bound(entered: threading.Event) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    assert entered.wait(timeout=9.5), "the tick never reached the snapshot step"


def test_a_bare_wait_before_the_state_it_guards(ready: threading.Event, seen: list[str]) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    ready.wait(1)
    assert seen == ["ready"]


def test_a_named_constant_bound(entered: threading.Event) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    assert entered.wait(HELD_S)


def test_a_failed_wait_fails_the_test(ready: threading.Event) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    if not ready.wait(0.5):
        pytest.fail("the worker never became ready")


def test_a_condition_predicate(changed: threading.Condition, items: list[str]) -> None:
    with changed:
        # ruleid: test-wait-bound-under-ten-seconds
        assert changed.wait_for(lambda: items, 3)
        # ruleid: test-wait-bound-under-ten-seconds
        assert changed.wait_for(lambda: items, timeout=3)


def test_a_worker_finishes(worker: threading.Thread) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    worker.join(5)
    assert not worker.is_alive()


def test_a_worker_finishes_by_keyword(worker: threading.Thread) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    worker.join(timeout=2.0)
    # ruleid: test-wait-bound-under-ten-seconds
    assert worker.join(5) is None


def test_a_future_answers(answer: Future[int | None]) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    assert answer.result(5) == 1
    # ruleid: test-wait-bound-under-ten-seconds
    assert answer.result(timeout=5) is None


def test_a_queue_answers(answers: queue.Queue[str]) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    assert answers.get(timeout=2) == "done"
    # ruleid: test-wait-bound-under-ten-seconds
    assert answers.get(True, 2) == "done"
    # ruleid: test-wait-bound-under-ten-seconds
    assert answers.get(block=True, timeout=2) == "done"
    # ruleid: test-wait-bound-under-ten-seconds
    assert answers.get(True, timeout=2) == "done"
    # ruleid: test-wait-bound-under-ten-seconds
    assert not answers.get(timeout=2)


def test_a_lock_is_released(entered: threading.Semaphore) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    assert entered.acquire(timeout=5)
    # ruleid: test-wait-bound-under-ten-seconds
    assert entered.acquire(True, 5)
    # ruleid: test-wait-bound-under-ten-seconds
    assert entered.acquire(True, timeout=5)
    # ruleid: test-wait-bound-under-ten-seconds
    assert entered.acquire(blocking=True, timeout=5)


def test_a_wait_beside_an_expected_timeout(ready: threading.Event) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    assert ready.wait(5) and not ready.wait(0.1)


def test_every_future_finishes(pending: list[Future[int]]) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    assert not concurrent.futures.wait(pending, timeout=5).not_done
    # ruleid: test-wait-bound-under-ten-seconds
    assert not wait(pending, timeout=5).not_done


def test_a_child_exits(child: subprocess.Popen[bytes]) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    assert child.wait(timeout=5) == 0
    # ruleid: test-wait-bound-under-ten-seconds
    child.communicate(timeout=5)


async def test_a_task_answers(answer: asyncio.Future[int]) -> None:
    # ruleid: test-wait-bound-under-ten-seconds
    assert await asyncio.wait_for(answer, 2) == 1
    # ruleid: test-wait-bound-under-ten-seconds
    assert not await asyncio.wait_for(answer, timeout=2)


def test_a_bound_of_ten_seconds_or_more(entered: threading.Event, worker: threading.Thread) -> None:
    # ok: test-wait-bound-under-ten-seconds
    assert entered.wait(10)
    # ok: test-wait-bound-under-ten-seconds
    assert entered.wait(timeout=30.0)
    # ok: test-wait-bound-under-ten-seconds
    worker.join(60)


def test_a_bound_that_is_not_a_literal(entered: threading.Event, worker: threading.Thread, budget_s: float) -> None:
    # ok: test-wait-bound-under-ten-seconds
    assert entered.wait(budget_s)
    # ok: test-wait-bound-under-ten-seconds
    worker.join(timeout=None)


def test_a_poll_with_no_wait(entered: threading.Event) -> None:
    # ok: test-wait-bound-under-ten-seconds
    assert entered.wait(0)


def test_a_wait_that_must_run_out(
    done: threading.Event,
    answer: Future[int],
    held: threading.Lock,
    changed: threading.Condition,
    items: list[str],
) -> None:
    # ok: test-wait-bound-under-ten-seconds
    assert not held.acquire(timeout=0.2)
    # ok: test-wait-bound-under-ten-seconds
    assert not held.acquire(blocking=True, timeout=0.2)
    with changed:
        # ok: test-wait-bound-under-ten-seconds
        assert not changed.wait_for(lambda: items, 0.2)
    # ok: test-wait-bound-under-ten-seconds
    assert not done.wait(0.2)
    # ok: test-wait-bound-under-ten-seconds
    assert not done.wait(0.3), "concurrent requests duplicate the whole ledger load"
    # ok: test-wait-bound-under-ten-seconds
    assert not done.wait(0.2) and not answer.done()
    # ok: test-wait-bound-under-ten-seconds
    assert done.wait(timeout=0.1) is None
    # ok: test-wait-bound-under-ten-seconds
    assert done.wait(0.1) is False


def test_a_timeout_the_test_expects(answers: queue.Queue[str], child: subprocess.Popen[bytes]) -> None:
    with pytest.raises(queue.Empty):
        # ok: test-wait-bound-under-ten-seconds
        answers.get(timeout=0.1)
    with pytest.raises(subprocess.TimeoutExpired):
        # ok: test-wait-bound-under-ten-seconds
        child.wait(timeout=0.5)


def test_a_pause_on_an_event_nobody_sets() -> None:
    # ok: test-wait-bound-under-ten-seconds
    threading.Event().wait(0.05)
    # ok: test-wait-bound-under-ten-seconds
    Event().wait(0.05)


def test_a_worker_still_blocked(worker: threading.Thread, second: threading.Thread, ready: list[str]) -> None:
    # ok: test-wait-bound-under-ten-seconds
    worker.join(0.2)
    assert worker.is_alive()
    # ok: test-wait-bound-under-ten-seconds
    worker.join(timeout=0.3)
    assert worker.is_alive() and ready == []
    # ok: test-wait-bound-under-ten-seconds
    second.join(0.5)
    blocked = second.is_alive()
    assert blocked


def test_calls_that_only_share_a_name(names: list[str], cache: dict[str, int]) -> None:
    # ok: test-wait-bound-under-ten-seconds
    assert ", ".join(names)
    # ok: test-wait-bound-under-ten-seconds
    assert cache.get("held", 5)
