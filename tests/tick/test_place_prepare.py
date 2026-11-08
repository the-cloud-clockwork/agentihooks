"""The prepare step of the spawn pass claims a task, builds its launch and hands every tick seam the pass values."""

from types import SimpleNamespace
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.tick.steps import place

pytestmark = pytest.mark.unit

RUNTIME = object()
LEDGER_SENTINEL = "ledger"
NAME = "engineer@1-1"


class _Store:
    """Records every call the prepare step makes on the swarm store."""

    def __init__(self, log: list[Any], claim: bool = True, lives: list[Any] | None = None) -> None:
        self.log = log
        self.claimed = claim
        self.lives = lives or []
        self.redis = SimpleNamespace(hget=self.hget)
        self.seats = SimpleNamespace(occupy=lambda *args: log.append(("occupy", *args)))

    def hget(self, key: str, field: str) -> None:
        self.log.append(("hget", key, field))

    def key(self, slug: str, name: str) -> str:
        return f"{slug}:{name}"

    def next_name(self, slug: str, lane: str, now_ms: int) -> str:
        self.log.append(("next_name", slug, lane, now_ms))
        return NAME

    def claim(self, slug: str, task_id: str, name: str, lease_ms: int) -> bool:
        self.log.append(("claim", slug, task_id, name, lease_ms))
        return self.claimed

    def handoff(self, slug: str, task_id: str) -> str:
        return ""

    def earlier_lives(self, slug: str, task_id: str) -> list[Any]:
        return self.lives

    def put_reclaim(self, slug: str, name: str, reclaim: Any) -> None:
        self.log.append(("put_reclaim", slug, name, reclaim))

    def handoff_seat(self, slug: str, task_id: str) -> str:
        return "eng-3@sw"

    def put_agent(self, slug: str, record: Any) -> None:
        self.log.append(("put_agent", slug, record.name))

    def release(self, slug: str, task_id: str, name: str) -> None:
        self.log.append(("release", slug, task_id, name))

    def drop_agent(self, slug: str, name: str) -> None:
        self.log.append(("drop_agent", slug, name))

    def record_launch(self, slug: str, record: Any, state: str) -> None:
        self.log.append(("record_launch", slug, record.name, state))


class _Ledger:
    """Answers the claim write with the row the test chooses."""

    def __init__(self, log: list[Any], claimant: str = NAME) -> None:
        self.log = log
        self.claimant = claimant

    def update_task(self, slug: str, task_id: str, fields: dict[str, Any], **conditions: Any) -> dict[str, Any]:
        self.log.append(("update_task", slug, task_id, dict(fields), conditions))
        return {"claimed_by": self.claimant, "state": "claimed"}


def _run(store: _Store, ledger: _Ledger) -> SimpleNamespace:
    rows = {
        "t1": {"id": "t1", "group_members": ["t2", "gone"]},
        "t2": {"id": "t2", "title": "T2", "description": "d2", "state": "open"},
    }
    config = SimpleNamespace(repo="/repo", lanes={"eng": {"kind": "code"}})
    return SimpleNamespace(
        slug="sw", config=config, store=store, ledger=ledger, runtime=RUNTIME, rows=rows, doc={"d": 1}, now_ms=1000
    )


def _seams(monkeypatch: pytest.MonkeyPatch, log: list[Any], blocks: dict[str, str], prime: Any = None) -> None:
    tick = place.tick

    def seam(label: str, result: Any) -> Any:
        return lambda *args: log.append((label, *args)) or result

    monkeypatch.setattr(place, "timing", SimpleNamespace(timed=lambda function, *a, **k: function(*a, **k)))
    for label in ("lives", "room", "held", "owner"):
        attr = {"lives": "_lives_spent", "room": "_room_wait", "held": "_held_back", "owner": "_owner_hold"}[label]
        monkeypatch.setattr(tick, attr, seam(label, blocks.get(label, "")))
    monkeypatch.setattr(tick, "_release_admission", seam("release_admission", None))
    monkeypatch.setattr(tick, "_pass_reclaim", lambda run: "pass-reclaim")
    monkeypatch.setattr(tick, "_stack_base", seam("stack_base", ["base"]))
    monkeypatch.setattr(tick, "_reported", seam("reported", ["peer-agent"]))
    monkeypatch.setattr(tick, "_sharing", seam("sharing", ["shared"]))
    monkeypatch.setattr(tick, "_free_seat", seam("free_seat", "eng-3@sw"))
    monkeypatch.setattr(tick, "primed", prime or seam("primed", {"p": 1}))
    monkeypatch.setattr(tick, "_steer", seam("steer", {"s": 1}))
    monkeypatch.setattr(tick, "_not_launched", seam("not_launched", ["not launched"]))
    monkeypatch.setattr(place.continuation, "resolve", seam("resolve", "resolved"))
    monkeypatch.setattr(place.ledger_workspace, "scaffold", seam("scaffold", "/work"))
    monkeypatch.setattr(place.transfers, "attach", seam("attach", "transfer"))


def _calls(log: list[Any], label: str) -> list[tuple[Any, ...]]:
    return [entry[1:] for entry in log if entry[0] == label]


@pytest.mark.parametrize(
    ("blocker", "asked"),
    [
        ("lives", ["lives"]),
        ("room", ["lives", "room"]),
        ("held", ["lives", "room", "held"]),
        ("owner", ["lives", "room", "held", "owner"]),
    ],
)
def test_the_first_blocking_check_stops_the_claim(
    monkeypatch: pytest.MonkeyPatch, blocker: str, asked: list[str]
) -> None:
    log: list[Any] = []
    _seams(monkeypatch, log, {blocker: f"{blocker} blocks"})
    store, ledger = _Store(log), _Ledger(log)
    run = _run(store, ledger)
    assert place._prepare(run, "eng", {"id": "t1"}, set(), "", set()) == (None, [f"{blocker} blocks"])
    task = run.rows["t1"]
    expected = {
        "lives": ("sw", store, ledger, run.rows, task),
        "room": (RUNTIME, "eng", task),
        "held": ("sw", ledger, run.rows, RUNTIME, task, 1000),
        "owner": ("sw", store, RUNTIME, task),
    }
    assert [entry[0] for entry in log] == asked
    assert [entry[1:] for entry in log] == [expected[label] for label in asked]


def test_a_lost_store_claim_releases_the_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[Any] = []
    _seams(monkeypatch, log, {})
    store = _Store(log, claim=False)
    run = _run(store, _Ledger(log))
    assert place._prepare(run, "eng", {"id": "t1"}, set(), "", set()) == (None, [])
    assert _calls(log, "claim") == [("sw", "t1", NAME, place.tick.LEASE_MS)]
    assert _calls(log, "release_admission") == [("sw", RUNTIME, run.rows["t1"])]


def test_a_claimed_task_carries_its_reclaim_overlaps_group_seat_and_primed_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[Any] = []
    _seams(monkeypatch, log, {})
    store, ledger = _Store(log, lives=["life"]), _Ledger(log)
    run = _run(store, ledger)
    taken: set[str] = set()
    launch, notes = place._prepare(run, "eng", dict(run.rows["t1"]), taken, "failing", {"peer"})
    task = run.rows["t1"]
    assert notes == []
    assert launch.task is task
    assert launch.record.name == NAME
    assert launch.record.seat == "eng-3@sw"
    assert _calls(log, "resolve") == [("/repo", task, run.rows, ["life"], {}, "pass-reclaim")]
    assert _calls(log, "put_reclaim") == [("sw", NAME, "resolved")]
    assert task["reclaim"] == "resolved"
    assert _calls(log, "stack_base") == [(task, run.rows, "/repo")]
    assert _calls(log, "reported") == [("sw", store, RUNTIME, {"peer"})]
    assert _calls(log, "sharing") == [(task, run.rows, ["peer-agent"])]
    assert task["overlaps"] == ["shared"]
    assert task["group"] == [{"id": "t2", "title": "T2", "description": "d2"}]
    assert _calls(log, "free_seat") == [("sw", "eng", taken, "eng-3@sw")]
    assert _calls(log, "steer") == [("sw", store, task, "failing")]
    assert launch.primed == {"p": 1, place.tick.ACK_LATER: True, "detached_launch": True, "s": 1}


def test_a_ledger_claim_held_by_another_agent_releases_the_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[Any] = []
    _seams(monkeypatch, log, {})
    store = _Store(log)
    run = _run(store, _Ledger(log, claimant="other"))
    result = place._prepare(run, "eng", {"id": "t1"}, set(), "", set())
    assert result == (None, ["task t1 is claimed on the ledger, not claimed"])
    assert _calls(log, "release_admission") == [("sw", RUNTIME, run.rows["t1"])]
    assert _calls(log, "release") == [("sw", "t1", NAME)]
    assert _calls(log, "drop_agent") == [("sw", NAME)]


def test_a_launch_that_fails_while_priming_reports_the_failed_record(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[Any] = []
    error = RuntimeError("primed broke")

    def broken(*args: Any) -> dict[str, Any]:
        raise error

    _seams(monkeypatch, log, {}, prime=broken)
    store, ledger = _Store(log), _Ledger(log)
    run = _run(store, ledger)
    assert place._prepare(run, "eng", {"id": "t1"}, set(), "", set()) == (None, ["not launched"])
    [(slug, seen_store, seen_ledger, runtime, rows, failed, now_ms)] = _calls(log, "not_launched")
    assert (slug, seen_store, seen_ledger, runtime, rows, now_ms) == ("sw", store, ledger, RUNTIME, run.rows, 1000)
    assert failed.record.name == NAME
    assert failed.task is run.rows["t1"]
    assert failed.error is error
