from concurrent.futures import Future
from typing import Any, Protocol

Key = tuple[str, int, str]
Entry = tuple[list[float], Future[tuple[str, str] | None]]


class Keeping(Protocol):
    def _fresh(self, now: float, entry: Entry, *, read: bool) -> bool: ...


class KeepsOnlyAFoundMerge(Keeping):
    def _fresh(self, now: float, entry: Entry, *, read: bool) -> bool:
        self: Any
        began, future = entry
        if not future.done():
            return True
        # ruleid: lab-merge-keep-rule-reads-the-answer
        if future.exception() is not None:
            return False
        # ruleid: lab-merge-keep-rule-reads-the-answer
        return future.result() is not None and now - began[0] < self.fresh_s


class KeepsByAgeAndWhetherItsHeadReadIt(Keeping):
    def _fresh(self, now: float, entry: Entry, *, read: bool) -> bool:
        self: Any
        began, future = entry
        # ok: lab-merge-keep-rule-reads-the-answer
        return not future.done() or now - began[0] < (self.fresh_s if read else self.kept_s)


def prunes_every_answer_that_found_no_merge(table: Any, key: Key) -> dict[Key, Entry]:
    return {
        each: (began, future)
        for each, (began, future) in table.resolutions.items()
        # ruleid: lab-merge-keep-rule-reads-the-answer
        if each == key or not future.done() or future.result() is not None
    }


def drops_every_failed_answer(table: Any, key: Key) -> None:
    for each, (_, future) in list(table.resolutions.items()):
        # ruleid: lab-merge-keep-rule-reads-the-answer
        if each != key and future.done() and future.exception() is not None:
            del table.resolutions[each]


def prunes_by_age_and_whether_its_head_read_it(table: Any, now: float) -> dict[Key, Entry]:
    return {
        each: (began, future)
        for each, (began, future) in table.resolutions.items()
        # ok: lab-merge-keep-rule-reads-the-answer
        if not future.done() or now - began[0] < (table.fresh_s if each in table.read else table.kept_s)
    }


def reads_the_answer_of_the_head_it_was_asked_for(table: Any, key: Key) -> tuple[str, str] | None:
    _, future = table.resolutions[key]
    # ok: lab-merge-keep-rule-reads-the-answer
    if future.exception() is not None:
        del table.resolutions[key]
        return None
    # ok: lab-merge-keep-rule-reads-the-answer
    return future.result()
