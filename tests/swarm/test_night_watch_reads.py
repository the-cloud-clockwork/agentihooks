from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm import claim_hold, freeze
from scripts.swarm.health import merge_stall, night_watch
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("tasks/held", "the freeze on task Held work holds it"),
        ("lane:eng", "the freeze on the eng lane holds it"),
        ("kind:code", "the freeze on code tasks holds it"),
    ],
)
def test_freeze_read_names_only_the_first_covering_record(monkeypatch, target, expected):
    task = {"id": "held", "title": "Held work", "lane": "eng", "kind": "code"}
    doc = {
        "tasks": [task],
        "freezes": [
            {"verb": "freeze", "target": "tasks/other"},
            {"verb": "focus", "target": target},
            {"verb": "freeze", "target": target},
            {"verb": "freeze", "target": "lane:eng"},
        ],
    }
    named = Mock(wraps=freeze._named)
    monkeypatch.setattr(freeze, "_named", named)

    assert claim_hold.freezes(task, doc, {}) == expected
    assert named.call_count == 1
    assert named.call_args.args[1] == target


def test_no_covering_freeze_never_reads_a_target_title(monkeypatch):
    doc = {"freezes": [{"verb": "freeze", "target": "tasks/other"}]}
    named = Mock(wraps=freeze._named)
    monkeypatch.setattr(freeze, "_named", named)

    assert claim_hold.freezes({"id": "held"}, doc, {}) == ""
    named.assert_not_called()


def test_night_watch_read_count_ignores_unrelated_freeze_history(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("watch", "/repo", max_eng=1, max_ci=0))
    store.update("watch", state="running")
    tasks = [
        {"id": str(index), "title": str(index), "state": "open", "lane": "eng", "kind": "code"} for index in range(32)
    ]
    doc = {
        "phases": [],
        "tasks": tasks,
        "freezes": [
            *({"verb": "freeze", "target": f"tasks/old{index}"} for index in range(100)),
            {"verb": "freeze", "target": "lane:eng"},
        ],
    }
    named = Mock(wraps=freeze._named)
    monkeypatch.setattr(freeze, "_named", named)
    monkeypatch.setattr(merge_stall, "findings", lambda *args: [])
    watch = night_watch.Watch(
        store, "watch", SimpleNamespace(), SimpleNamespace(live_names=lambda: set()), doc, 1_000, False, lambda: []
    )

    assert night_watch.run(watch) == []
    assert night_watch.read(store, "watch") == {"at": 1_000, night_watch.SEAT: {"since": 1_000}}
    assert [args.args[1] for args in named.call_args_list] == ["lane:eng"] * len(tasks)
