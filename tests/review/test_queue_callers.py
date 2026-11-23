from types import SimpleNamespace

import pytest

from scripts.review import record
from scripts.swarm import cli
from tests.review.fakes import AUTHOR, REVIEWER, SLUG, URL, FakeForge, swarm
from tests.review.test_queue_gate import _queue

pytestmark = pytest.mark.unit


@pytest.mark.feature("review-findings-queue-gate")
@pytest.mark.parametrize("actor", ["expert", "hotfix", "stalled_master"])
@pytest.mark.parametrize(
    ("mode", "read", "code", "queued", "rows"),
    [
        ("observe", False, 0, [URL], [("review", "observe")]),
        ("enforce", False, 1, [], [("review", "deny")]),
        ("enforce", True, 0, [URL], []),
    ],
)
def test_every_queue_caller_requires_the_independent_review(
    monkeypatch: pytest.MonkeyPatch,
    actor: str,
    mode: str,
    read: bool,
    code: int,
    queued: list[str],
    rows: list[tuple[str, str]],
) -> None:
    found = swarm({"review": mode})
    caller = SimpleNamespace(name=AUTHOR, lane="expert" if actor == "expert" else "master", task="t1")
    monkeypatch.setattr(cli, "_me", lambda *args: caller)
    monkeypatch.setattr(cli, "_hotfix_master", lambda *args: actor == "hotfix")
    monkeypatch.setattr(cli.master_queue, "route", lambda *args: {"task": "t1"})
    if read:
        forge = FakeForge()
        record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First"])
        record.mark(found, SLUG, forge, URL, AUTHOR, ["f1"], "")
    actual_code, actual_queued, actual_rows = _queue(monkeypatch, found)
    assert actual_code == code
    assert actual_queued == queued
    assert [(row.gate, row.kind) for row in actual_rows] == rows
