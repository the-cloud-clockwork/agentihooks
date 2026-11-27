from concurrent.futures import Future
from typing import Any

Fetches = dict[tuple[str, str], tuple[float, Future[bytes | None]]]


def forgets_every_answer_on_the_fresh_window(cache: Any, now: float) -> Fetches:
    return {
        key: (started, future)
        for key, (started, future) in cache.fetches.items()
        # ruleid: lab-manifest-cache-forgets-absent-answers
        if not future.done() or now - started < cache.fresh_s
    }


def drops_every_answer_past_the_fresh_window(cache: Any, now: float) -> None:
    for key, (started, future) in list(cache.fetches.items()):
        # ruleid: lab-manifest-cache-forgets-absent-answers
        if future.done() and now - started >= cache.fresh_s:
            del cache.fetches[key]


def kept_s(cache: Any, future: Future[bytes | None]) -> float:
    absent = future.exception() is None and future.result() is None
    return cache.absent_s if absent else cache.fresh_s


def keeps_each_answer_for_its_own_window(cache: Any, now: float) -> Fetches:
    return {
        key: (started, future)
        for key, (started, future) in cache.fetches.items()
        # ok: lab-manifest-cache-forgets-absent-answers
        if not future.done() or now - started < kept_s(cache, future)
    }
