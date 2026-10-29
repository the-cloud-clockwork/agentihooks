from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.events import dead
from scripts.events.envelope import Envelope

pytestmark = pytest.mark.unit


def test_dead_letters_retain_the_original_envelope_and_delivery_context(monkeypatch):
    monkeypatch.setattr(dead, "time", SimpleNamespace(time_ns=lambda: 1_234_567_890_000_000))
    original = Envelope("e1", "task.claimed", "s", "task", "t1", 1, 10, "worker", {"nested": [1]}, "parent")
    letter = dead.dead_letter(original, "queue.work", 3)
    assert letter.to_dict() == {
        "id": letter.id,
        "type": "dead_lettered",
        "swarm": "s",
        "entity": "task",
        "key": "t1",
        "version": 1,
        "time": 1_234_567_890,
        "author": "worker",
        "data": {"event": original.to_dict(), "source": "queue.work", "attempts": 3},
        "cause": "e1",
        "schema_version": 1,
    }
    assert len(letter.id) == 64
    assert int(letter.id, 16) >= 0
    assert dead.dead_letter(original, "queue.work", 4).id == letter.id
    assert dead.dead_letter(original, "queue.other", 3).id != letter.id
    assert dead.dead_letter(replace(original, id="e2"), "queue.work", 3).id != letter.id
