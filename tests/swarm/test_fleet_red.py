import inspect
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.swarm import fleet_red, panic
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import _claimable
from scripts.tick import engine
from scripts.tick.adapters.fakes import FakeSourceControl
from scripts.tick.deadline import Deadline
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.ports.source_control import OpenChange, RollupCheck
from scripts.tick.step import Context, View, Write
from scripts.tick.steps import actions, tasks

pytestmark = pytest.mark.unit

MINE = "https://github.com/o/r/pull/1"
NOW = "2026-10-28T02:00:00+00:00"
NOW_MS = int(datetime.fromisoformat(NOW).timestamp() * 1000)
STALE = "2026-10-27T19:59:59+00:00"
EDGE = "2026-10-27T20:00:00+00:00"
LIVE_LISTING_S = 16.6


def run(name: str, conclusion: str | None, at: str | None = NOW) -> RollupCheck:
    return RollupCheck(name, conclusion, at)


def status(context: str, state: str, at: str = NOW) -> RollupCheck:
    return RollupCheck(context, state, at)


def pull(number: int, *checks: RollupCheck) -> OpenChange:
    return OpenChange(f"https://github.com/o/r/pull/{number}", checks)


def fleet(mine: list[RollupCheck], *others: list[RollupCheck]) -> list[OpenChange]:
    return [pull(1, *mine), *[pull(n, *checks) for n, checks in enumerate(others, start=2)]]


def red_elsewhere() -> list[OpenChange]:
    return [OpenChange(f"https://github.com/x/y/pull/{n}", (run("Tests", "FAILURE"),)) for n in range(1, 5)]


def task(task_id, **fields):
    return {"id": task_id, "state": "open", "claimed_by": "", "lane": "eng", "territory": [f"area/{task_id}"], **fields}


def test_a_check_failing_on_most_other_pull_requests_is_shared():
    pulls = fleet(
        [run("Tests", "FAILURE"), run("Lint", "SUCCESS")],
        [run("Tests", "FAILURE")],
        [run("Tests", "FAILURE")],
        [run("Tests", "SUCCESS")],
    )
    assert fleet_red.shared(pulls, MINE, NOW_MS) == {"Tests"}


def test_a_check_failing_on_half_the_others_is_not_shared():
    pulls = fleet(
        [run("Tests", "FAILURE")],
        [run("Tests", "FAILURE")],
        [run("Tests", "FAILURE")],
        [run("Tests", "SUCCESS")],
        [run("Tests", "SUCCESS")],
    )
    assert fleet_red.shared(pulls, MINE, NOW_MS) == set()


def test_fewer_than_three_other_pull_requests_with_checks_share_nothing():
    pulls = fleet([run("Tests", "FAILURE")], [run("Tests", "FAILURE")], [run("Tests", "FAILURE")], [])
    assert fleet_red.shared(pulls, MINE, NOW_MS) == set()


def test_only_pull_requests_with_a_check_inside_the_window_count():
    fresh = [[run("Tests", "FAILURE")]] * 3
    pulls = fleet([run("Tests", "FAILURE")], *fresh, *[[run("Tests", "SUCCESS", STALE)]] * 3)
    assert fleet_red.shared(pulls, MINE, NOW_MS) == {"Tests"}
    pulls = fleet([run("Tests", "FAILURE")], *fresh, *[[run("Tests", "SUCCESS", EDGE)]] * 3)
    assert fleet_red.shared(pulls, MINE, NOW_MS) == set()


def test_a_check_without_a_time_does_not_place_a_pull_request_in_the_window():
    untimed = [[run("Tests", "FAILURE", None)]] * 3
    assert fleet_red.shared(fleet([run("Tests", "FAILURE")], *untimed), MINE, NOW_MS) == set()


def test_a_failing_status_context_counts_by_its_context_name():
    pulls = fleet(
        [status("semgrep", "ERROR")],
        [status("semgrep", "FAILURE")],
        [status("semgrep", "ERROR")],
        [status("semgrep", "PENDING")],
    )
    assert fleet_red.shared(pulls, MINE, NOW_MS) == {"semgrep"}


def test_a_pull_request_red_only_from_shared_checks_is_held():
    others = [[run("Tests", "FAILURE"), run("Semgrep", "FAILURE")]] * 3
    assert fleet_red.held(fleet([run("Tests", "FAILURE"), run("Semgrep", "FAILURE")], *others), MINE, NOW_MS)


def test_a_pull_request_with_its_own_failure_beside_a_shared_one_is_not_held():
    others = [[run("Tests", "FAILURE"), run("Lint", "SUCCESS")]] * 3
    assert not fleet_red.held(fleet([run("Tests", "FAILURE"), run("Lint", "FAILURE")], *others), MINE, NOW_MS)


def test_a_green_or_unlisted_pull_request_is_not_held():
    others = [[run("Tests", "FAILURE")]] * 3
    assert not fleet_red.held(fleet([run("Tests", "SUCCESS")], *others), MINE, NOW_MS)
    assert not fleet_red.held(fleet([run("Tests", "FAILURE")], *others), "https://github.com/o/r/pull/99", NOW_MS)


def test_the_checker_reads_open_pull_requests_once_and_only_for_an_open_task_with_a_pull_request():
    reads = []
    pulls = fleet([run("Tests", "FAILURE")], *[[run("Tests", "FAILURE")]] * 3)
    admits = fleet_red.checker(lambda repo: reads.append(repo) or pulls, lambda: NOW_MS / 1000)
    assert admits(task("plain"))
    assert admits(task("done", state="done", pr_url=MINE))
    assert admits(task("unparsed", pr_url="not a pull request"))
    assert reads == []
    assert not admits(task("red", pr_url=MINE))
    assert admits(task("other", pr_url="https://github.com/o/r/pull/99"))
    assert reads == ["o/r"]


def test_the_checker_judges_each_pull_request_against_its_own_repository_read_once():
    reads = []
    elsewhere = red_elsewhere()
    green = fleet([run("Tests", "FAILURE")], *[[run("Tests", "SUCCESS")]] * 3)
    listings = {"o/r": green, "x/y": elsewhere}
    admits = fleet_red.checker(lambda repo: reads.append(repo) or listings[repo], lambda: NOW_MS / 1000)
    assert admits(task("home", pr_url=MINE))
    assert not admits(task("away", pr_url="https://github.com/x/y/pull/1"))
    assert not admits(task("again", pr_url="https://github.com/x/y/pull/2"))
    assert reads == ["o/r", "x/y"]


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("https://github.com/o/r/pull/1", "o/r"),
        ("https://github.com/The-Org/some.repo/pull/12", "The-Org/some.repo"),
        ("https://gitlab.com/o/r/pull/1", ""),
        ("https://github.com/o/r/issues/1", ""),
        ("", ""),
    ],
)
def test_repository_names_the_owner_and_name_of_a_github_pull_request_url(url, repo):
    assert fleet_red.repository(url) == repo


def test_the_checker_judges_the_window_at_its_clock():
    pulls = fleet([run("Tests", "FAILURE")], *[[run("Tests", "FAILURE")]] * 3)
    later = (NOW_MS + fleet_red.WINDOW_MS + 1000) / 1000
    assert fleet_red.checker(lambda repo: pulls, lambda: later)(task("red", pr_url=MINE))


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("no auth"),
        KeyError("no auth"),
        OSError("no auth"),
        ValueError("no auth"),
        subprocess.TimeoutExpired("no auth", 60),
    ],
)
def test_a_failed_read_admits_every_task_and_says_why(error, capsys):
    reads = []

    def broken(repo):
        reads.append(repo)
        raise error

    admits = fleet_red.checker(broken)
    assert admits(task("red", pr_url=MINE))
    assert admits(task("again", pr_url=MINE))
    assert reads == ["o/r"]
    assert capsys.readouterr().err == f"fleet red hold skipped, listing open pull requests failed: {error}\n"


def test_a_failed_gh_listing_names_its_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    error = subprocess.CalledProcessError(1, ["gh", "api", "graphql"], "", "GraphQL: Resource not accessible\n")

    def broken(repo: str) -> fleet_red.Pulls:
        raise error

    assert fleet_red.checker(broken)(task("red", pr_url=MINE))
    assert capsys.readouterr().err == (
        f"fleet red hold skipped, listing open pull requests failed: {error}: GraphQL: Resource not accessible\n"
    )


def test_read_lists_open_pull_requests_through_the_repository_port() -> None:
    answer = (OpenChange("u", ()),)
    provider, ports = FakeSourceControl({"open_changes": answer}), []

    def port(repo: str) -> FakeSourceControl:
        ports.append(repo)
        return provider

    assert fleet_red.read("o/r", port) is answer
    assert ports == ["o/r"]
    [call] = provider.calls
    assert (call.operation, call.args) == ("open_changes", ())
    assert 59 < call.deadline.remaining <= fleet_red.SECONDS == 60


def test_read_reaches_each_repository_through_the_panic_repository_port() -> None:
    assert inspect.signature(fleet_red.read).parameters["ports"].default is panic.port


def test_read_errors_are_the_port_errors() -> None:
    from scripts.swarm.stack import PORT_ERRORS

    assert fleet_red.ERRORS == PORT_ERRORS


@pytest.fixture
def store(tmp_path):
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", str(tmp_path), max_eng=2, max_ci=0))
    return saved


class Ledger:
    def __init__(self):
        self.notices = []

    def notice(self, slug, task_id, text):
        self.notices.append((slug, task_id, text))


def _notice(store, ledger, rows, now_ms, reader=None):
    found, cleared, saves = fleet_red.holds("sw", store, rows, now_ms, reader)
    for save in saves:
        fleet_red.saver(store)(save)
    if cleared:
        store.redis.delete(*cleared)
    return [action for hold in found if (action := fleet_red.post("sw", store, ledger, *hold))]


def test_the_notice_pass_declares_each_hold_once_and_again_after_it_lifts(store):
    rows = {t["id"]: t for t in [task("red", pr_url=MINE), task("plain"), task("gone", state="done", pr_url=MINE)]}
    both = [run("Tests", "FAILURE"), run("Semgrep", "FAILURE")]
    red = fleet(both, *[both] * 3)
    ledger, reads = Ledger(), []

    def reader(repo):
        reads.append(repo)
        return red

    actions = _notice(store, ledger, rows, NOW_MS, reader)
    assert actions == [f"task red held unclaimed, {MINE} is red only from Semgrep, Tests"]
    assert ledger.notices == [("sw", "red", fleet_red.NOTICE.format(url=MINE, checks="Semgrep, Tests"))]
    assert reads == ["o/r"]
    assert store.redis.get(store.key("sw", "fleet-red:red")) == MINE
    assert _notice(store, ledger, rows, NOW_MS, reader) == []
    assert len(ledger.notices) == 1
    green = fleet([run("Tests", "FAILURE")], *[[run("Tests", "SUCCESS")]] * 3)
    assert _notice(store, ledger, rows, NOW_MS, lambda repo: green) == []
    assert store.redis.get(store.key("sw", "fleet-red:red")) is None
    assert _notice(store, ledger, rows, NOW_MS, reader) == actions
    assert len(ledger.notices) == 2


def test_the_notice_pass_skips_claimed_tasks_and_forgets_a_hold_once_its_task_leaves_open(store):
    both = [run("Tests", "FAILURE")]
    red = fleet(both, *[both] * 3)
    ledger = Ledger()
    rows = {"red": task("red", pr_url=MINE)}
    assert _notice(store, ledger, rows, NOW_MS, lambda repo: red)
    key = store.key("sw", "fleet-red:red")
    for moved in ({"state": "pr"}, {"claimed_by": "engineer@a"}, {"pr_url": ""}):
        store.redis.set(key, MINE)
        rows = {"red": {**task("red", pr_url=MINE), **moved}}
        assert _notice(store, ledger, rows, NOW_MS, lambda repo: red) == []
        assert store.redis.get(key) is None
    store.redis.set(key, MINE)
    store.redis.set(store.key("sw", "claim", "red"), "engineer@b")
    rows = {"red": task("red", pr_url=MINE)}
    assert _notice(store, ledger, rows, NOW_MS, lambda repo: red) == []
    assert store.redis.get(key) is None
    store.redis.delete(store.key("sw", "claim", "red"))
    assert _notice(store, ledger, rows, NOW_MS, lambda repo: red)
    assert len(ledger.notices) == 2


def test_the_notice_pass_reads_each_repository_once_and_judges_each_task_against_its_own(store):
    away = "https://github.com/x/y/pull/1"
    elsewhere = red_elsewhere()
    green = fleet([run("Tests", "FAILURE")], *[[run("Tests", "SUCCESS")]] * 3)
    listings, reads, ledger = {"o/r": green, "x/y": elsewhere}, [], Ledger()
    rows = {t["id"]: t for t in [task("home", pr_url=MINE), task("away", pr_url=away), task("odd", pr_url="x")]}
    actions = _notice(store, ledger, rows, NOW_MS, lambda repo: reads.append(repo) or listings[repo])
    assert actions == [f"task away held unclaimed, {away} is red only from Tests"]
    assert sorted(reads) == ["o/r", "x/y"]


def test_the_notice_pass_still_judges_a_repository_whose_neighbour_failed_to_read(store):
    away = "https://github.com/x/y/pull/1"
    elsewhere = red_elsewhere()

    def reader(repo: str) -> list[OpenChange]:
        if repo == "o/r":
            raise OSError("no auth")
        return elsewhere

    rows = {t["id"]: t for t in [task("home", pr_url=MINE), task("away", pr_url=away)]}
    actions = _notice(store, Ledger(), rows, NOW_MS, reader)
    assert actions == [f"task away held unclaimed, {away} is red only from Tests"]


def test_the_notice_pass_reads_nothing_without_an_open_task_with_a_pull_request(store):
    def reader(repo):
        raise AssertionError("read")

    rows = {"plain": task("plain"), "gone": task("gone", state="done", pr_url=MINE)}
    assert _notice(store, Ledger(), rows, NOW_MS, reader) == []


@pytest.mark.parametrize(
    "error", [TimeoutError("x"), KeyError("x"), OSError("x"), ValueError("x"), subprocess.SubprocessError()]
)
def test_the_notice_pass_declares_nothing_when_the_read_fails(store, error):
    def reader(repo):
        raise error

    ledger = Ledger()
    rows = {"red": task("red", pr_url=MINE)}
    key = store.key("sw", "fleet-red:red")
    store.redis.set(key, MINE)
    assert _notice(store, ledger, rows, NOW_MS, reader) == []
    assert ledger.notices == []
    assert store.redis.get(key) == MINE


def test_the_tick_holds_a_task_red_only_from_a_shared_check_and_releases_it_once_the_check_passes(store):
    rows = {t["id"]: t for t in [task("red", pr_url=MINE, awaiting="fix"), task("plain")]}
    doc = {"phases": [], "tasks": list(rows.values())}
    at = datetime.now(UTC).isoformat()
    others = [[run("Tests", "FAILURE", at)]] * 3
    listed(store, "sw", "o/r", lambda repo: fleet([run("Tests", "FAILURE", at)], *others))
    assert [t["id"] for t in _claimable("sw", store, rows, doc, "eng")] == ["plain"]
    store.redis.delete(store.key("sw", "fleet-red-pulls:o/r"))
    passed = [[run("Tests", "SUCCESS", at)]] * 3
    listed(store, "sw", "o/r", lambda repo: fleet([run("Tests", "FAILURE", at)], *passed))
    assert sorted(t["id"] for t in _claimable("sw", store, rows, doc, "eng")) == ["plain", "red"]


def test_an_unlisted_pull_request_owns_no_failing_checks():
    assert fleet_red.own(fleet([run("Tests", "FAILURE")]), "https://github.com/o/r/pull/99") == set()


def test_the_notice_pass_declares_a_hold_listed_after_a_task_that_is_not_held(store):
    both = [run("Tests", "FAILURE")]
    red = fleet(both, *[both] * 3)
    rows = {t["id"]: t for t in [task("green", pr_url="https://github.com/o/r/pull/99"), task("red", pr_url=MINE)]}
    actions = _notice(store, Ledger(), rows, NOW_MS, lambda repo: red)
    assert actions == [f"task red held unclaimed, {MINE} is red only from Tests"]


def test_the_notice_pass_reads_claims_and_clears_hold_marks_in_one_call_each_however_many_tasks(store, monkeypatch):
    rows = {
        t["id"]: t
        for t in [task(f"open{n}", pr_url=MINE) for n in range(4)] + [task(f"done{n}", state="done") for n in range(4)]
    }
    for task_id in rows:
        store.redis.set(store.key("sw", f"fleet-red:{task_id}"), MINE)
    store.redis.set(store.key("sw", "claim", "open0"), "engineer@b")
    calls = []
    for name in ("get", "mget", "delete", "set"):
        real = getattr(store.redis, name)
        monkeypatch.setattr(
            store.redis, name, lambda *a, _real=real, _name=name, **k: calls.append(_name) or _real(*a, **k)
        )
    green = fleet([run("Tests", "FAILURE")], *[[run("Tests", "SUCCESS")]] * 3)
    assert _notice(store, Ledger(), rows, NOW_MS, lambda repo: green) == []
    assert calls == ["mget", "delete"]
    assert [key for key in store.redis.scan_iter(store.key("sw", "fleet-red:*"))] == []


@pytest.mark.feature("fleet-red-claim-view-cache-only")
def test_the_claim_view_admits_a_task_on_a_cold_cache_without_listing_live(store, monkeypatch):
    reads = []
    monkeypatch.setattr(fleet_red, "read", lambda repo: reads.append(repo) or [])
    rows = {"red": task("red", pr_url=MINE, awaiting="fix")}

    assert _claimable("sw", store, rows, {"phases": [], "tasks": list(rows.values())}, "eng") == [rows["red"]]
    assert reads == []


def test_the_claim_view_judges_a_saved_listing_without_listing_again(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    monkeypatch.setattr(fleet_red, "read", lambda repo: reads.append(repo) or [])
    both = [run("Tests", "FAILURE", datetime.now(UTC).isoformat())]
    listed(store, "sw", "o/r", lambda repo: fleet(both, *[both] * 3))
    rows = {"red": task("red", pr_url=MINE)}

    assert _claimable("sw", store, rows, {"phases": [], "tasks": list(rows.values())}, "eng") == []
    assert reads == []


def test_the_claim_view_admits_every_task_when_the_saved_listing_records_a_failed_read(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    monkeypatch.setattr(fleet_red, "read", lambda repo: reads.append(repo) or [])
    store.redis.set(store.key("sw", "fleet-red-pulls:o/r"), "null")
    rows = {"red": task("red", pr_url=MINE)}

    assert _claimable("sw", store, rows, {"phases": [], "tasks": list(rows.values())}, "eng") == [rows["red"]]
    assert reads == []


def events_admits(store: RedisStore, tmp_path: Path, row: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    """Return the events readiness snapshot's admission of an events swarm whose ledger holds only ``row``."""
    from scripts.swarm import readiness

    store.create(SwarmConfig("ev", str(tmp_path), max_eng=2, max_ci=0, engine="events"))
    ledger = Mock()
    ledger.metadata.return_value = {"_meta": {"rev": 1}}
    ledger.state.return_value = {"phases": [], "tasks": [row]}
    return readiness.Source(store, ledger).snapshot("ev").admits


@pytest.mark.feature("events-admission-saved-fleet-red-listing")
def test_the_events_snapshot_admits_an_open_task_with_a_pull_request_without_listing_live(
    store: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    monkeypatch.setattr(fleet_red, "read", lambda repo: reads.append(repo) or [])
    row = task("red", pr_url=MINE)

    assert events_admits(store, tmp_path, row)(row) is True
    assert reads == []


@pytest.mark.feature("events-admission-saved-fleet-red-listing")
def test_the_events_snapshot_holds_a_task_from_the_saved_listing_without_listing_live(
    store: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    both = [run("Tests", "FAILURE", datetime.now(UTC).isoformat())]
    listed(store, "ev", "o/r", lambda repo: fleet(both, *[both] * 3))
    monkeypatch.setattr(fleet_red, "read", lambda repo: reads.append(repo) or [])
    row = task("red", pr_url=MINE)

    assert events_admits(store, tmp_path, row)(row) is False
    assert reads == []


@pytest.mark.feature("events-admission-saved-fleet-red-listing")
def test_the_tick_saves_the_fleet_red_listing_of_an_events_swarm(
    store: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store.create(SwarmConfig("ev", str(tmp_path), max_eng=2, max_ci=0, engine="events"))
    monkeypatch.setattr(fleet_red, "read", lambda repo: SAVED)
    tick.tick("ev", store, FakeLedger([task("a", pr_url=MINE)]), FakeRuntime(), now_ms=4_321)

    assert fleet_red.saved_reader(store, "ev")("o/r") == tuple(SAVED)


def test_the_notice_step_budget_outlasts_the_live_listing_so_its_save_is_kept() -> None:
    (step,) = [step for step in tasks.STEPS if step.name == "fleet red notice"]

    assert step.budget_s > LIVE_LISTING_S


def test_the_tick_hands_the_notice_pass_its_store_ledger_rows_and_clock(store, monkeypatch):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    seen = []
    lifted = store.key("sw", "fleet-red:gone")
    store.redis.set(lifted, MINE)
    monkeypatch.setattr(fleet_red, "holds", lambda *args: seen.append(args) or ([("a", MINE, "Tests")], [lifted], []))
    ledger = FakeLedger([{"id": "a"}])
    actions = tick.tick("sw", store, ledger, FakeRuntime(), now_ms=4_321)
    [(slug, got_store, rows, now_ms)] = seen
    assert (slug, got_store, list(rows), now_ms) == ("sw", store, ["a"], 4_321)
    assert f"task a held unclaimed, {MINE} is red only from Tests" in actions
    assert ("a", fleet_red.NOTICE.format(url=MINE, checks="Tests"), "swarm") in ledger.comments
    assert store.redis.get(lifted) is None


SAVED = [pull(1, run("Tests", "FAILURE")), pull(2, run("Lint", None, None), status("Gate", "SUCCESS"))]


def listed(store: RedisStore, slug: str, repo: str, reader: fleet_red.Reader | None = None) -> fleet_red.Pulls | None:
    """List through the cache and apply its save as the engine would, returning the listing."""
    pulls, saves = fleet_red.cached(store, slug, repo, reader)
    for save in saves:
        fleet_red.saver(store)(save)
    return pulls


def test_the_cache_returns_a_fresh_listing_with_its_save_and_writes_nothing(store: RedisStore) -> None:
    key = store.key("sw", "fleet-red-pulls:o/r")

    pulls, saves = fleet_red.cached(store, "sw", "o/r", lambda repo: SAVED)

    assert pulls == tuple(SAVED)
    listing = [[p.url, [[c.name, c.result, c.at] for c in p.checks]] for p in SAVED]
    assert saves == (Write(fleet_red.LISTINGS, key, json.dumps(listing)),)
    assert store.redis.get(key) is None


def test_a_saved_listing_returns_no_save(store: RedisStore) -> None:
    listed(store, "sw", "o/r", lambda repo: SAVED)

    assert fleet_red.cached(store, "sw", "o/r", lambda repo: []) == (tuple(SAVED), ())


def test_the_saver_keeps_a_listing_saved_since_the_step_read_none(store: RedisStore) -> None:
    key = store.key("sw", "fleet-red-pulls:o/r")
    store.redis.set(key, "[]", ex=60)

    with pytest.raises(
        RevisionConflict, match=f"^another pass saved the listing under {key} since the step read none$"
    ):
        fleet_red.saver(store)(Write(fleet_red.LISTINGS, key, "null"))

    assert store.redis.get(key) == "[]"
    assert store.redis.ttl(key) == 60


def test_the_saver_drops_its_write_on_a_redis_timeout() -> None:
    from redis.exceptions import TimeoutError as RedisTimeoutError

    def timed_out(*args: object, **kwargs: object) -> object:
        raise RedisTimeoutError("slow")

    store = cast(fleet_red.Listings, SimpleNamespace(redis=SimpleNamespace(set=timed_out)))

    with pytest.raises(RevisionConflict, match="^Redis timed out saving the listing under key$"):
        fleet_red.saver(store)(Write(fleet_red.LISTINGS, "key", "[]"))


def test_holds_carry_the_listing_saves_out_and_write_nothing(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = [run("Tests", "FAILURE")]
    monkeypatch.setattr(fleet_red, "read", lambda repo: fleet(both, *[both] * 3))
    rows = {"red": task("red", pr_url=MINE), "away": task("away", pr_url="https://github.com/o/r/pull/9")}
    key = store.key("sw", "fleet-red-pulls:o/r")

    found, cleared, saves = fleet_red.holds("sw", store, rows, NOW_MS)

    assert (found, cleared) == ([("red", MINE, "Tests")], [store.key("sw", "fleet-red:away")])
    assert [(save.domain, save.key) for save in saves] == [(fleet_red.LISTINGS, key)]
    assert json.loads(str(saves[0].value))[0][0] == MINE
    assert store.redis.get(key) is None


def test_the_engine_saves_the_listing_the_notice_step_returns(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = [run("Tests", "FAILURE"), run("Semgrep", "FAILURE")]
    reads: list[str] = []

    def read_once(repo: str) -> list[OpenChange]:
        reads.append(repo)
        return fleet(both, *[both] * 3)

    monkeypatch.setattr(fleet_red, "read", read_once)
    (step,) = [step for step in tasks.STEPS if step.name == "fleet red notice"]
    ledger = Ledger()
    state = {"slug": "sw", "store": store, "ledger": ledger, "rows": {"red": task("red", pr_url=MINE)}}
    state |= {"now_ms": NOW_MS, "box": {}}
    view = cast(View, SimpleNamespace(read=lambda domain, key: state.get(key)))
    listing = store.key("sw", "fleet-red-pulls:o/r")

    found = step.run(Context(view, Deadline.after(60.0)))
    assert fleet_red.LISTINGS in {write.domain for write in found.writes}
    assert store.redis.get(listing) is None

    (record,) = engine.run_group(
        tasks.REOPENS, Context(view, Deadline.after(60.0)), [step], apply=tasks.appliers(state)
    )

    assert record.dropped == ()
    assert json.loads(store.redis.get(listing))[0][0] == MINE
    assert store.redis.ttl(listing) == fleet_red.TTL
    assert ledger.notices == [("sw", "red", fleet_red.NOTICE.format(url=MINE, checks="Semgrep, Tests"))]
    assert actions([record]) == [f"task red held unclaimed, {MINE} is red only from Semgrep, Tests"]
    (again,) = engine.run_group(tasks.REOPENS, Context(view, Deadline.after(60.0)), [step], apply=tasks.appliers(state))
    assert again.result is not None
    assert fleet_red.LISTINGS not in {write.domain for write in again.result.writes}
    assert reads == ["o/r", "o/r"]
    assert len(ledger.notices) == 1


def test_the_cache_lists_once_and_keeps_the_listing_ten_minutes(store: RedisStore) -> None:
    reads: list[str] = []

    def reader(repo: str) -> list[OpenChange]:
        reads.append(repo)
        return SAVED

    assert listed(store, "sw", "o/r", reader) == tuple(SAVED)
    assert listed(store, "sw", "o/r", reader) == tuple(SAVED)
    assert reads == ["o/r"]
    assert store.redis.ttl(store.key("sw", "fleet-red-pulls:o/r")) == 600
    assert fleet_red.TTL == 600


def test_the_cache_keeps_each_swarm_and_repository_apart(store: RedisStore) -> None:
    reads: list[str] = []

    def reader(repo: str) -> list[OpenChange]:
        reads.append(repo)
        return SAVED

    listed(store, "sw", "o/r", reader)
    listed(store, "sw", "o/s", reader)
    listed(store, "other", "o/r", reader)
    assert reads == ["o/r", "o/s", "o/r"]


@pytest.mark.parametrize(
    "error", [TimeoutError("x"), KeyError("x"), OSError("x"), ValueError("x"), subprocess.SubprocessError()]
)
def test_the_cache_keeps_a_failed_listing_as_none_for_ten_minutes(store: RedisStore, error: Exception) -> None:
    reads: list[str] = []

    def reader(repo: str) -> list[OpenChange]:
        reads.append(repo)
        raise error

    assert listed(store, "sw", "o/r", reader) is None
    assert listed(store, "sw", "o/r", reader) is None
    assert reads == ["o/r"]
    assert store.redis.ttl(store.key("sw", "fleet-red-pulls:o/r")) == 600


def test_the_cache_keeps_an_empty_listing(store: RedisStore) -> None:
    reads: list[str] = []

    def reader(repo: str) -> list[OpenChange]:
        reads.append(repo)
        return []

    assert listed(store, "sw", "o/r", reader) == ()
    assert listed(store, "sw", "o/r", reader) == ()
    assert reads == ["o/r"]


def test_the_cache_lists_through_read_without_a_reader(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_red, "read", lambda repo: SAVED if repo == "o/r" else [])
    assert listed(store, "sw", "o/r") == tuple(SAVED)


def test_the_notice_pass_reads_through_the_cache_without_a_reader(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = {"red": task("red", pr_url=MINE)}
    both = [run("Tests", "FAILURE"), run("Semgrep", "FAILURE")]
    reads: list[str] = []

    def reader(repo: str) -> list[OpenChange]:
        reads.append(repo)
        return fleet(both, *[both] * 3)

    monkeypatch.setattr(fleet_red, "read", reader)
    ledger = Ledger()
    held = _notice(store, ledger, rows, NOW_MS)
    assert held == [f"task red held unclaimed, {MINE} is red only from Semgrep, Tests"]
    store.redis.delete(store.key("sw", "fleet-red:red"))
    assert _notice(store, ledger, rows, NOW_MS) == held
    assert reads == ["o/r"]


def test_the_notice_pass_declares_nothing_while_the_cache_holds_a_failed_listing(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reader(repo: str) -> list[OpenChange]:
        raise TimeoutError("x")

    monkeypatch.setattr(fleet_red, "read", reader)
    ledger = Ledger()
    assert _notice(store, ledger, {"red": task("red", pr_url=MINE)}, NOW_MS) == []
    assert ledger.notices == []


def test_holds_leave_out_a_noticed_task_and_post_notices_it_only_once(store):
    both = [run("Tests", "FAILURE")]
    red = fleet(both, *[both] * 3)
    rows = {"red": task("red", pr_url=MINE)}
    ledger = Ledger()
    assert fleet_red.holds("sw", store, rows, NOW_MS, lambda repo: red) == ([("red", MINE, "Tests")], [], [])
    assert store.redis.get(store.key("sw", "fleet-red:red")) is None
    assert (
        fleet_red.post("sw", store, ledger, "red", MINE, "Tests")
        == f"task red held unclaimed, {MINE} is red only from Tests"
    )
    assert fleet_red.post("sw", store, ledger, "red", MINE, "Tests") == ""
    assert ledger.notices == [("sw", "red", fleet_red.NOTICE.format(url=MINE, checks="Tests"))]
    assert fleet_red.holds("sw", store, rows, NOW_MS, lambda repo: red) == ([], [], [])
    assert store.redis.get(store.key("sw", "fleet-red:red")) == MINE
