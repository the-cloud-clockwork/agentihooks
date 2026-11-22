from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from scripts.inbox.seats import SeatRegistry
from scripts.scheduler import commands
from scripts.scheduler.lease import Busy, held
from scripts.scheduler.slots import InboxSlot
from scripts.swarm import cli, safety_nudge, seat_crons
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.adapters import safety_nudge as nudge_adapter
from tests.swarm import test_cli

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore
    from tests.swarm.test_tick import FakeRuntime

    Env = tuple[RedisStore, Any, FakeRuntime]

SLUG = "sw"
SEAT = "master@sw"
BACKEND_SEAT = "master-2@sw"
FRONT = "master@a1b2c3-0001"
BACKEND = "master@a1b2c3-0002"
LOOP = "3,23,53 * * * *"
DRIFT = "7,37 * * * *"
SHEET = "11,41 * * * *"
EMAIL = "13,43 * * * *"
NUDGE = "17 * * * *"
CREW = "9,39 * * * *"
ALWAYS = [NUDGE]
HOOKS = {name: getattr(seat_crons, name) for name in ("on_start", "on_stop", "on_join")}
env, run = test_cli.env, test_cli.run


@pytest.fixture(autouse=True)
def fake_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real seat cron hooks, over one fake scheduler and swarm store per test in place of this host's."""
    import fakeredis

    from scripts.events.redis_state import RedisStateStore
    from scripts.scheduler.core import Scheduler
    from scripts.swarm.store import RedisStore

    redis = fakeredis.FakeRedis(decode_responses=True)
    for name, hook in HOOKS.items():
        monkeypatch.setattr(seat_crons, name, hook)
    monkeypatch.setattr(
        seat_crons, "scheduler", lambda: Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    )
    monkeypatch.setattr(seat_crons, "swarms", lambda: RedisStore(redis))


def aimed(seat: str = SEAT) -> list[dict[str, Any]]:
    """The open crons on the test scheduler aimed at the seat's inbox."""
    target = {"kind": "inbox", "address": seat}
    return [view for view in commands.entries(seat_crons.scheduler(), "cron") if target in view["targets"]]


def schedules(seat: str = SEAT) -> list[str]:
    return sorted(view["expression"] for view in aimed(seat))


def ids(seat: str = SEAT) -> list[str]:
    return sorted(view["id"] for view in aimed(seat))


def payload(expression: str) -> str:
    return next(view["payload"] for view in aimed() if view["expression"] == expression)


def by_hand(expression: str, seat: str = SEAT, owner: str = "master@323133-0991") -> str:
    """A cron a master added by hand with agentihooks cron add, owned by the master and aimed at the seat."""
    made = commands.add_cron(seat_crons.scheduler(), owner, InboxSlot(seat), "by hand", expression=expression)
    return made["id"]


def join(env: Env, monkeypatch: pytest.MonkeyPatch, name: str, role: str = "orchestrator") -> None:
    store = env[0]
    monkeypatch.setattr(ledger, "send", lambda *args, **kwargs: {})
    monkeypatch.setattr(nudge_adapter, "record_master", lambda environ: None)
    monkeypatch.setattr(seat_crons, "swarms", lambda: store)
    ledger.cmd_join(SimpleNamespace(slug=SLUG, name=name, role=role))


def created(repo: str = "/work/repo") -> None:
    assert run(SLUG, "create", "--repo", repo) == 0


def closable(env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A started swarm whose fake ledger takes the close summary and mark and whose snapshot lands in the test folder."""
    fake = env[1]
    fake.summarize = lambda slug, note, by: None
    fake.mark_closed = lambda slug, by: None
    monkeypatch.setattr(cli.snapshot, "path", lambda slug: tmp_path / slug / "snapshot.json")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    created()
    run(SLUG, "start")


@pytest.mark.feature("master-seat-durable-crons")
def test_start_puts_the_standing_seat_crons_on_the_scheduler(env: Env) -> None:
    created()
    assert schedules() == []
    assert run(SLUG, "start") == 0
    views = aimed()
    assert sorted(view["expression"] for view in views) == ALWAYS
    assert {view["owner"] for view in views} == {"agentihooks master seat crons"}
    assert {view["timezone"] for view in views} == {"UTC"}
    assert payload(NUDGE).startswith(f"{safety_nudge.TEXT}, read agentihooks swarm sw status")
    assert all(view["payload"].endswith("Write nothing to the operator.") for view in views)


@pytest.mark.feature("master-seat-durable-crons")
def test_a_second_start_keeps_the_same_seat_crons(env: Env) -> None:
    created()
    run(SLUG, "start")
    first = ids()
    assert run(SLUG, "start") == 0
    assert ids() == first and len(first) == 1


@pytest.mark.feature("master-seat-durable-crons")
@pytest.mark.parametrize("seated", [True, False])
def test_the_front_door_master_join_ensures_one_set(env: Env, monkeypatch: pytest.MonkeyPatch, seated: bool) -> None:
    created()
    if seated:
        SeatRegistry(env[0].redis).occupy(SEAT, FRONT, 1)
    join(env, monkeypatch, FRONT)
    first = ids()
    join(env, monkeypatch, FRONT)
    assert schedules() == ALWAYS and ids() == first


@pytest.mark.feature("master-seat-durable-crons")
@pytest.mark.parametrize(
    ("name", "role", "seat"),
    [(BACKEND, "orchestrator", BACKEND_SEAT), (FRONT, "member", SEAT), ("boss", "orchestrator", "")],
)
def test_only_the_front_door_master_join_puts_seat_crons(
    env: Env, monkeypatch: pytest.MonkeyPatch, name: str, role: str, seat: str
) -> None:
    created()
    if seat:
        SeatRegistry(env[0].redis).occupy(seat, name, 1)
    join(env, monkeypatch, name, role)
    assert aimed() == [] and aimed(BACKEND_SEAT) == []


@pytest.mark.feature("master-seat-durable-crons")
def test_hand_made_seat_crons_are_adopted_and_their_duplicates_cancelled(env: Env) -> None:
    created()
    nudge, extra, crew = by_hand(NUDGE), by_hand(NUDGE), by_hand(CREW)
    other = by_hand(NUDGE, "master@other")
    run(SLUG, "start")
    run(SLUG, "start")
    found: dict[str, list[str]] = {}
    for view in aimed():
        found.setdefault(view["expression"], []).append(view["id"])
    assert found[CREW] == [crew] and sorted(found) == sorted([*ALWAYS, CREW])
    assert len(found[NUDGE]) == 1 and found[NUDGE][0] in (nudge, extra)
    dropped = extra if found[NUDGE] == [nudge] else nudge
    assert seat_crons.scheduler().get(dropped).state == "cancelled"
    assert ids("master@other") == [other]


@pytest.mark.feature("master-seat-durable-crons")
@pytest.mark.parametrize("via", ["start", "join"])
def test_a_start_or_join_on_a_swarm_holding_the_old_loop_and_drift_crons_leaves_only_the_wall_clock_crons(
    env: Env, monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    created()
    old = [by_hand(LOOP, owner=seat_crons.OWNER), by_hand(DRIFT, owner=seat_crons.OWNER), by_hand(LOOP)]
    crew = by_hand(CREW)
    other = by_hand(LOOP, "master@other", seat_crons.OWNER)
    if via == "start":
        assert run(SLUG, "start") == 0
    else:
        SeatRegistry(env[0].redis).occupy(SEAT, FRONT, 1)
        join(env, monkeypatch, FRONT)
    assert schedules() == sorted([*ALWAYS, CREW]) and crew in ids()
    assert [seat_crons.scheduler().get(cron).state for cron in old] == ["cancelled"] * 3
    assert ids("master@other") == [other]


@pytest.mark.feature("master-seat-durable-crons")
def test_a_fresh_swarm_gets_only_the_wall_clock_crons(env: Env) -> None:
    created()
    script = Path.home() / "scratchpad" / "repo" / "sw-master" / "progress-sheet" / "progress_sheet.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    state = Path.home() / ".swarm-progress-notification-email" / "repo" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"enabled": True, "cadence_hours": 1}))
    assert run(SLUG, "start") == 0
    assert schedules() == sorted([NUDGE, SHEET, EMAIL])


@pytest.mark.feature("master-seat-durable-crons")
def test_the_progress_sheet_cron_follows_the_master_seat_script(env: Env) -> None:
    created()
    run(SLUG, "start")
    assert SHEET not in schedules()
    script = Path.home() / "scratchpad" / "repo" / "sw-master" / "progress-sheet" / "progress_sheet.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    run(SLUG, "start")
    assert schedules() == sorted([*ALWAYS, SHEET])
    assert payload(SHEET).startswith(f"Progress sheet (seat cron): cd {script.parent} and run python3 {script} run")
    script.unlink()
    run(SLUG, "start")
    assert schedules() == ALWAYS


@pytest.mark.feature("master-seat-durable-crons")
def test_the_progress_email_cron_follows_its_enabled_state(env: Env) -> None:
    created()
    state = Path.home() / ".swarm-progress-notification-email" / "repo" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"enabled": True, "cadence_hours": 1}))
    run(SLUG, "start")
    assert EMAIL in schedules()
    assert payload(EMAIL).startswith(
        f"Progress email due check (seat cron): run the swarm-progress-notification-email skill's due_check.sh with {state.parent}."
    )
    state.write_text(json.dumps({"enabled": False, "cadence_hours": None}))
    run(SLUG, "start")
    assert schedules() == ALWAYS


@pytest.mark.feature("master-seat-durable-crons")
@pytest.mark.parametrize("text", ["not json", "[]", "null", json.dumps({"enabled": "yes"})])
def test_an_unreadable_or_unset_email_state_wants_no_email_cron(env: Env, text: str) -> None:
    created()
    state = Path.home() / ".swarm-progress-notification-email" / "repo" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(text)
    assert run(SLUG, "start") == 0
    assert schedules() == ALWAYS


@pytest.mark.feature("master-seat-durable-crons")
@pytest.mark.parametrize("argv", [("stop",), ("stop", "--now"), ("close", "--now")])
def test_stop_and_close_leave_no_seat_cron(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: tuple[str, ...]
) -> None:
    closable(env, monkeypatch, tmp_path)
    by_hand(NUDGE)
    by_hand(CREW)
    other = by_hand(CREW, "master@other")
    assert run(SLUG, *argv) == 0
    assert schedules() == []
    assert ids("master@other") == [other]


@pytest.mark.feature("master-seat-durable-crons")
def test_a_close_handed_to_the_live_master_keeps_the_seat_crons(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    closable(env, monkeypatch, tmp_path)
    master = next(agent for agent in env[0].agents(SLUG) if agent.lane == "master")
    env[2].live.add(master.name)
    capsys.readouterr()
    assert run(SLUG, "close") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"swarm": SLUG, "asked": master.name}
    assert schedules() == ALWAYS


@pytest.mark.feature("master-seat-durable-crons")
@pytest.mark.parametrize("state", ["stopping", "stopped"])
def test_a_join_on_an_ended_swarm_cancels_the_seat_crons(env: Env, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    created()
    run(SLUG, "start")
    assert schedules() == ALWAYS
    env[0].update(SLUG, state=state)
    join(env, monkeypatch, FRONT)
    assert schedules() == []


@pytest.mark.feature("master-seat-durable-crons")
def test_ensure_holds_the_seat_crons_lock(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    created()
    on = seat_crons.scheduler()
    monkeypatch.setattr(seat_crons, "WAIT_S", 0)
    with held(on.state, on.name("seat-crons-lock"), 0):
        with pytest.raises(Busy):
            seat_crons.ensure(on, env[0].config(SLUG), Path.home())
        with pytest.raises(Busy):
            seat_crons.cancel(on, SLUG)


@pytest.mark.feature("master-seat-durable-crons")
@pytest.mark.parametrize(
    "error",
    [OSError, Busy, commands.CommandError, SwarmError],
)
def test_an_unreachable_scheduler_warns_and_start_and_stop_go_on(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: type[Exception]
) -> None:
    def down() -> Any:
        raise error("scheduler down")

    created()
    monkeypatch.setattr(seat_crons, "scheduler", down)
    capsys.readouterr()
    assert run(SLUG, "start") == 0
    assert "warning: the master seat crons of sw were not ensured: scheduler down" in capsys.readouterr().err
    assert run(SLUG, "stop") == 0
    assert "warning: the master seat crons of sw were not cancelled: scheduler down" in capsys.readouterr().err


@pytest.mark.feature("master-seat-durable-crons")
def test_an_unreachable_swarm_store_warns_and_the_join_goes_on(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def down() -> Any:
        raise SwarmError("Redis is unreachable")

    created()
    monkeypatch.setattr(ledger, "send", lambda *args, **kwargs: {})
    monkeypatch.setattr(nudge_adapter, "record_master", lambda environ: None)
    monkeypatch.setattr(seat_crons, "swarms", down)
    capsys.readouterr()
    ledger.cmd_join(SimpleNamespace(slug=SLUG, name=FRONT, role="orchestrator"))
    out = capsys.readouterr()
    assert json.loads(out.out) == {"joined": FRONT, "slug": SLUG, "role": "orchestrator"}
    assert "warning: the master seat crons of sw were not ensured: Redis is unreachable" in out.err
    assert aimed() == []
