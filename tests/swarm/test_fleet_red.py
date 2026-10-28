import subprocess
from datetime import UTC, datetime

import pytest

from scripts.swarm import fleet_red
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import _claimable

pytestmark = pytest.mark.unit

MINE = "https://github.com/o/r/pull/1"
NOW = "2026-10-28T02:00:00+00:00"
NOW_MS = int(datetime.fromisoformat(NOW).timestamp() * 1000)
STALE = "2026-10-27T19:59:59+00:00"
EDGE = "2026-10-27T20:00:00+00:00"


def run(name, conclusion, at=NOW):
    return {"__typename": "CheckRun", "name": name, "conclusion": conclusion, "completedAt": at}


def status(context, state, at=NOW):
    return {"__typename": "StatusContext", "context": context, "state": state, "startedAt": at}


def pull(number, *checks):
    return {"url": f"https://github.com/o/r/pull/{number}", "statusCheckRollup": list(checks)}


def fleet(mine, *others):
    return [pull(1, *mine), *[pull(n, *checks) for n, checks in enumerate(others, start=2)]]


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
    untimed = [[{"name": "Tests", "conclusion": "FAILURE"}]] * 3
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
    admits = fleet_red.checker("/repo", lambda repo: reads.append(repo) or pulls, lambda: NOW_MS / 1000)
    assert admits(task("plain"))
    assert admits(task("done", state="done", pr_url=MINE))
    assert reads == []
    assert not admits(task("red", pr_url=MINE))
    assert admits(task("other", pr_url="https://github.com/o/r/pull/99"))
    assert reads == ["/repo"]


def test_the_checker_judges_the_window_at_its_clock():
    pulls = fleet([run("Tests", "FAILURE")], *[[run("Tests", "FAILURE")]] * 3)
    later = (NOW_MS + fleet_red.WINDOW_MS + 1000) / 1000
    assert fleet_red.checker("/repo", lambda repo: pulls, lambda: later)(task("red", pr_url=MINE))


@pytest.mark.parametrize(
    "error",
    [
        fleet_red.ReadError("no auth"),
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

    admits = fleet_red.checker("/repo", broken)
    assert admits(task("red", pr_url=MINE))
    assert admits(task("again", pr_url=MINE))
    assert reads == ["/repo"]
    assert capsys.readouterr().err == f"fleet red hold skipped, listing open pull requests failed: {error}\n"


def test_read_lists_open_pull_requests_with_their_check_rollup():
    calls = []

    class Done:
        returncode, stdout, stderr = 0, '[{"url": "u", "statusCheckRollup": []}]', ""

    found = fleet_red.read("/repo", lambda *args, **kwargs: calls.append((args, kwargs)) or Done())
    assert found == [{"url": "u", "statusCheckRollup": []}]
    ((args, kwargs),) = calls
    assert args == (["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", "url,statusCheckRollup"],)
    assert kwargs == {"cwd": "/repo", "capture_output": True, "text": True, "timeout": 60}


@pytest.mark.parametrize(("stderr", "why"), [(" no auth\n", "^no auth$"), ("", "^gh pr list exited 4$")])
def test_read_raises_a_read_error_on_a_failed_listing(stderr, why):
    class Failed:
        returncode, stdout = 4, ""

    Failed.stderr = stderr
    with pytest.raises(fleet_red.ReadError, match=why):
        fleet_red.read("/repo", lambda *args, **kwargs: Failed())


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

    config = store.config("sw")
    actions = fleet_red.notice_pass("sw", config, store, ledger, rows, NOW_MS, reader)
    assert actions == [f"task red held unclaimed, {MINE} is red only from Semgrep, Tests"]
    assert ledger.notices == [("sw", "red", fleet_red.NOTICE.format(url=MINE, checks="Semgrep, Tests"))]
    assert reads == [config.repo]
    assert store.redis.get(store.key("sw", "fleet-red:red")) == MINE
    assert fleet_red.notice_pass("sw", config, store, ledger, rows, NOW_MS, reader) == []
    assert len(ledger.notices) == 1
    green = fleet([run("Tests", "FAILURE")], *[[run("Tests", "SUCCESS")]] * 3)
    assert fleet_red.notice_pass("sw", config, store, ledger, rows, NOW_MS, lambda repo: green) == []
    assert store.redis.get(store.key("sw", "fleet-red:red")) is None
    assert fleet_red.notice_pass("sw", config, store, ledger, rows, NOW_MS, reader) == actions
    assert len(ledger.notices) == 2


def test_the_notice_pass_reads_nothing_without_an_open_task_with_a_pull_request(store):
    def reader(repo):
        raise AssertionError("read")

    rows = {"plain": task("plain"), "gone": task("gone", state="done", pr_url=MINE)}
    assert fleet_red.notice_pass("sw", store.config("sw"), store, Ledger(), rows, NOW_MS, reader) == []


@pytest.mark.parametrize(
    "error", [fleet_red.ReadError("x"), OSError("x"), ValueError("x"), subprocess.SubprocessError()]
)
def test_the_notice_pass_declares_nothing_when_the_read_fails(store, error):
    def reader(repo):
        raise error

    ledger = Ledger()
    rows = {"red": task("red", pr_url=MINE)}
    assert fleet_red.notice_pass("sw", store.config("sw"), store, ledger, rows, NOW_MS, reader) == []
    assert ledger.notices == []


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
