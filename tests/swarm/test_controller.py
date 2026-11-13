"""Tick lock diagnostics: the stale error names its caller and holder, and each take and release is journalled."""

import json
from typing import Any

import pytest

from scripts.swarm import controller, lease
from scripts.swarm.store import RedisStore, SwarmError

pytestmark = pytest.mark.unit

HOLDER = json.dumps({"epoch": 7, "host": "box:boot", "pid": 42, "start": "99", "token": "abcdef0123456789"})
HOLDER_TEXT = "held by epoch 7 host box:boot pid 42 start 99 token abcdef01"
MINE = json.dumps({"epoch": 1, "token": "0011223344556677"})
Locked = tuple[RedisStore, lease.Lease]
Capture = pytest.CaptureFixture[str]


@pytest.fixture
def locked() -> Locked:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    held = lease.acquire(store, "sw", "host")
    assert held is not None
    return store, held


def journal(capsys: Capture) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("event") == "swarm_tick_lock"]


def conflicting(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from redis.exceptions import WatchError

    pipeline = store.redis.pipeline

    def raising() -> Any:
        pipe = pipeline()

        def execute() -> None:
            raise WatchError("conflict")

        monkeypatch.setattr(pipe, "execute", execute)
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", raising)


def test_a_stale_keep_names_the_caller_the_expected_token_and_an_absent_key(locked: Locked) -> None:
    store, held = locked
    with pytest.raises(SwarmError) as error:
        controller.keep_tick(store, "sw", held, MINE, 1000, "pass heartbeat")
    assert str(error.value) == "the tick lock is stale: pass heartbeat expected token 00112233, the key is absent"


def test_a_stale_keep_decodes_the_holder_that_replaced_it(locked: Locked) -> None:
    store, held = locked
    store.redis.set(store.key("sw", "tick-lock"), HOLDER, px=1000)
    with pytest.raises(SwarmError) as error:
        controller.keep_tick(store, "sw", held, MINE, 1000, "pass step")
    assert str(error.value) == f"the tick lock is stale: pass step expected token 00112233, {HOLDER_TEXT}"


def test_a_keep_without_a_caller_is_named_unnamed(locked: Locked) -> None:
    store, held = locked
    with pytest.raises(
        SwarmError, match="^the tick lock is stale: unnamed expected token 00112233, the key is absent$"
    ):
        controller.keep_tick(store, "sw", held, MINE, 1000)


def test_take_and_release_journal_taken_refused_released_and_another_holder(locked: Locked, capsys: Capture) -> None:
    store, held = locked
    token = controller.take_tick_lock(store, "sw", held, 600_000, "pass")
    assert token is not None
    assert controller.take_tick_lock(store, "sw", held, 600_000, "respawn master") is None
    controller.release_tick_lock(store, "sw", MINE, "rename")
    controller.release_tick_lock(store, "sw", token, "pass")
    controller.release_tick_lock(store, "sw", token, "pass stop")
    holder = controller._lock_holder(token)
    assert journal(capsys) == [
        {"event": "swarm_tick_lock", "slug": "sw", "caller": "pass", "outcome": "taken", "holder": "the key is absent"},
        {"event": "swarm_tick_lock", "slug": "sw", "caller": "respawn master", "outcome": "refused", "holder": holder},
        {
            "event": "swarm_tick_lock",
            "slug": "sw",
            "caller": "rename",
            "outcome": "found another holder",
            "holder": holder,
        },
        {"event": "swarm_tick_lock", "slug": "sw", "caller": "pass", "outcome": "released", "holder": holder},
        {
            "event": "swarm_tick_lock",
            "slug": "sw",
            "caller": "pass stop",
            "outcome": "found another holder",
            "holder": "the key is absent",
        },
    ]


def test_take_journals_a_replaced_dead_holder(locked: Locked, capsys: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    store, held = locked
    store.redis.set(store.key("sw", "tick-lock"), HOLDER, px=600_000)
    monkeypatch.setattr(controller, "_dead", lambda raw: raw == HOLDER)
    assert controller.take_tick_lock(store, "sw", held, 600_000, "respawn master") is not None
    assert journal(capsys) == [
        {
            "event": "swarm_tick_lock",
            "slug": "sw",
            "caller": "respawn master",
            "outcome": "replaced a dead holder",
            "holder": HOLDER_TEXT,
        }
    ]


def test_callers_default_to_unnamed_in_the_journal(locked: Locked, capsys: Capture) -> None:
    store, held = locked
    token = controller.take_tick_lock(store, "sw", held, 600_000)
    assert token is not None
    controller.release_tick_lock(store, "sw", token)
    assert [(line["caller"], line["outcome"]) for line in journal(capsys)] == [
        ("unnamed", "taken"),
        ("unnamed", "released"),
    ]


def test_a_conflicting_take_and_release_journal_the_current_holder(
    locked: Locked, capsys: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, held = locked
    store.redis.set(store.key("sw", "tick-lock"), HOLDER, px=600_000)
    monkeypatch.setattr(controller, "_dead", lambda raw: True)
    conflicting(store, monkeypatch)
    assert controller.take_tick_lock(store, "sw", held, 600_000, "pass") is None
    controller.release_tick_lock(store, "sw", HOLDER, "rename")
    assert store.redis.get(store.key("sw", "tick-lock")) == HOLDER
    assert journal(capsys) == [
        {"event": "swarm_tick_lock", "slug": "sw", "caller": "pass", "outcome": "refused", "holder": HOLDER_TEXT},
        {
            "event": "swarm_tick_lock",
            "slug": "sw",
            "caller": "rename",
            "outcome": "found another holder",
            "holder": HOLDER_TEXT,
        },
    ]


def test_an_undecoded_holder_value_still_names_its_prefix() -> None:
    assert controller._lock_holder("plain-token-value") == (
        "held by epoch unknown host unknown pid unknown start unknown token plain-to"
    )
