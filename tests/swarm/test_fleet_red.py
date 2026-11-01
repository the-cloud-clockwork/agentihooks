import inspect
import subprocess
from datetime import UTC, datetime

import pytest

from scripts.swarm import fleet_red, panic
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import _claimable
from scripts.tick.adapters.fakes import FakeSourceControl
from scripts.tick.ports.source_control import OpenChange, RollupCheck

pytestmark = pytest.mark.unit

MINE = "https://github.com/o/r/pull/1"
NOW = "2026-10-28T02:00:00+00:00"
NOW_MS = int(datetime.fromisoformat(NOW).timestamp() * 1000)
STALE = "2026-10-27T19:59:59+00:00"
EDGE = "2026-10-27T20:00:00+00:00"


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


def test_the_notice_pass_declares_each_hold_once_and_again_after_it_lifts(store):
    rows = {t["id"]: t for t in [task("red", pr_url=MINE), task("plain"), task("gone", state="done", pr_url=MINE)]}
    both = [run("Tests", "FAILURE"), run("Semgrep", "FAILURE")]
    red = fleet(both, *[both] * 3)
    ledger, reads = Ledger(), []

    def reader(repo):
        reads.append(repo)
        return red

    actions = fleet_red.notice_pass("sw", store, ledger, rows, NOW_MS, reader)
    assert actions == [f"task red held unclaimed, {MINE} is red only from Semgrep, Tests"]
    assert ledger.notices == [("sw", "red", fleet_red.NOTICE.format(url=MINE, checks="Semgrep, Tests"))]
    assert reads == ["o/r"]
    assert store.redis.get(store.key("sw", "fleet-red:red")) == MINE
    assert fleet_red.notice_pass("sw", store, ledger, rows, NOW_MS, reader) == []
    assert len(ledger.notices) == 1
    green = fleet([run("Tests", "FAILURE")], *[[run("Tests", "SUCCESS")]] * 3)
    assert fleet_red.notice_pass("sw", store, ledger, rows, NOW_MS, lambda repo: green) == []
    assert store.redis.get(store.key("sw", "fleet-red:red")) is None
    assert fleet_red.notice_pass("sw", store, ledger, rows, NOW_MS, reader) == actions
    assert len(ledger.notices) == 2


def test_the_notice_pass_skips_claimed_tasks_and_forgets_a_hold_once_its_task_leaves_open(store):
    both = [run("Tests", "FAILURE")]
    red = fleet(both, *[both] * 3)
    ledger = Ledger()
    rows = {"red": task("red", pr_url=MINE)}
    assert fleet_red.notice_pass("sw", store, ledger, rows, NOW_MS, lambda repo: red)
    key = store.key("sw", "fleet-red:red")
    for moved in ({"state": "pr"}, {"claimed_by": "engineer@a"}, {"pr_url": ""}):
        store.redis.set(key, MINE)
        rows = {"red": {**task("red", pr_url=MINE), **moved}}
        assert fleet_red.notice_pass("sw", store, ledger, rows, NOW_MS, lambda repo: red) == []
        assert store.redis.get(key) is None
    store.redis.set(key, MINE)
    store.redis.set(store.key("sw", "claim", "red"), "engineer@b")
    rows = {"red": task("red", pr_url=MINE)}
    assert fleet_red.notice_pass("sw", store, ledger, rows, NOW_MS, lambda repo: red) == []
    assert store.redis.get(key) is None
    store.redis.delete(store.key("sw", "claim", "red"))
    assert fleet_red.notice_pass("sw", store, ledger, rows, NOW_MS, lambda repo: red)
    assert len(ledger.notices) == 2


def test_the_notice_pass_reads_each_repository_once_and_judges_each_task_against_its_own(store):
    away = "https://github.com/x/y/pull/1"
    elsewhere = red_elsewhere()
    green = fleet([run("Tests", "FAILURE")], *[[run("Tests", "SUCCESS")]] * 3)
    listings, reads, ledger = {"o/r": green, "x/y": elsewhere}, [], Ledger()
    rows = {t["id"]: t for t in [task("home", pr_url=MINE), task("away", pr_url=away), task("odd", pr_url="x")]}
    actions = fleet_red.notice_pass(
        "sw", store, ledger, rows, NOW_MS, lambda repo: reads.append(repo) or listings[repo]
    )
    assert actions == [f"task away held unclaimed, {away} is red only from Tests"]
    assert sorted(reads) == ["o/r", "x/y"]


def test_the_notice_pass_still_judges_a_repository_whose_neighbour_failed_to_read(store):
    away = "https://github.com/x/y/pull/1"
    elsewhere = red_elsewhere()

    def reader(repo):
        if repo == "o/r":
            raise OSError("no auth")
        return elsewhere

    rows = {t["id"]: t for t in [task("home", pr_url=MINE), task("away", pr_url=away)]}
    actions = fleet_red.notice_pass("sw", store, Ledger(), rows, NOW_MS, reader)
    assert actions == [f"task away held unclaimed, {away} is red only from Tests"]


def test_the_notice_pass_reads_nothing_without_an_open_task_with_a_pull_request(store):
    def reader(repo):
        raise AssertionError("read")

    rows = {"plain": task("plain"), "gone": task("gone", state="done", pr_url=MINE)}
    assert fleet_red.notice_pass("sw", store, Ledger(), rows, NOW_MS, reader) == []


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
    assert fleet_red.notice_pass("sw", store, ledger, rows, NOW_MS, reader) == []
    assert ledger.notices == []
    assert store.redis.get(key) == MINE


def test_the_tick_holds_a_task_red_only_from_a_shared_check_and_releases_it_once_the_check_passes(store, monkeypatch):
    rows = {t["id"]: t for t in [task("red", pr_url=MINE, awaiting="fix"), task("plain")]}
    doc = {"phases": [], "tasks": list(rows.values())}
    at = datetime.now(UTC).isoformat()
    others = [[run("Tests", "FAILURE", at)]] * 3
    monkeypatch.setattr(fleet_red, "read", lambda repo: fleet([run("Tests", "FAILURE", at)], *others))
    assert [t["id"] for t in _claimable("sw", store, rows, doc, "eng")] == ["plain"]
    passed = [[run("Tests", "SUCCESS", at)]] * 3
    monkeypatch.setattr(fleet_red, "read", lambda repo: fleet([run("Tests", "FAILURE", at)], *passed))
    assert sorted(t["id"] for t in _claimable("sw", store, rows, doc, "eng")) == ["plain", "red"]


def test_an_unlisted_pull_request_owns_no_failing_checks():
    assert fleet_red.own(fleet([run("Tests", "FAILURE")]), "https://github.com/o/r/pull/99") == set()


def test_the_notice_pass_declares_a_hold_listed_after_a_task_that_is_not_held(store):
    both = [run("Tests", "FAILURE")]
    red = fleet(both, *[both] * 3)
    rows = {t["id"]: t for t in [task("green", pr_url="https://github.com/o/r/pull/99"), task("red", pr_url=MINE)]}
    actions = fleet_red.notice_pass("sw", store, Ledger(), rows, NOW_MS, lambda repo: red)
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
    assert fleet_red.notice_pass("sw", store, Ledger(), rows, NOW_MS, lambda repo: green) == []
    assert calls == ["mget", "delete"]
    assert [key for key in store.redis.scan_iter(store.key("sw", "fleet-red:*"))] == []


def test_the_claim_view_lists_open_pull_requests_in_the_task_pull_request_repo(store, monkeypatch):
    reads = []
    monkeypatch.setattr(fleet_red, "read", lambda repo: reads.append(repo) or [])
    rows = {"red": task("red", pr_url=MINE, awaiting="fix")}
    _claimable("sw", store, rows, {"phases": [], "tasks": list(rows.values())}, "eng")
    assert reads == ["o/r"]


def test_the_tick_hands_the_notice_pass_its_store_ledger_rows_and_clock(store, monkeypatch):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    seen = []
    monkeypatch.setattr(fleet_red, "notice_pass", lambda *args: seen.append(args) or ["noticed"])
    ledger = FakeLedger([{"id": "a"}])
    actions = tick.tick("sw", store, ledger, FakeRuntime(), now_ms=4_321)
    [(slug, got_store, got_ledger, rows, now_ms)] = seen
    assert (slug, got_store, got_ledger, list(rows), now_ms) == ("sw", store, ledger, ["a"], 4_321)
    assert "noticed" in actions
