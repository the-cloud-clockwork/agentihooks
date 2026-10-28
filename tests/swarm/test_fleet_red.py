import pytest

from scripts.swarm import fleet_red
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import _claimable

pytestmark = pytest.mark.unit

MINE = "https://github.com/o/r/pull/1"


def run(name, conclusion):
    return {"__typename": "CheckRun", "name": name, "conclusion": conclusion, "status": "COMPLETED"}


def status(context, state):
    return {"__typename": "StatusContext", "context": context, "state": state}


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
    assert fleet_red.shared(pulls, MINE) == {"Tests"}


def test_a_check_failing_on_half_the_others_is_not_shared():
    pulls = fleet(
        [run("Tests", "FAILURE")],
        [run("Tests", "FAILURE")],
        [run("Tests", "FAILURE")],
        [run("Tests", "SUCCESS")],
        [run("Tests", "SUCCESS")],
    )
    assert fleet_red.shared(pulls, MINE) == set()


def test_fewer_than_three_other_pull_requests_with_checks_share_nothing():
    pulls = fleet([run("Tests", "FAILURE")], [run("Tests", "FAILURE")], [run("Tests", "FAILURE")], [])
    assert fleet_red.shared(pulls, MINE) == set()


def test_a_failing_status_context_counts_by_its_context_name():
    pulls = fleet(
        [status("semgrep", "ERROR")],
        [status("semgrep", "FAILURE")],
        [status("semgrep", "ERROR")],
        [status("semgrep", "PENDING")],
    )
    assert fleet_red.shared(pulls, MINE) == {"semgrep"}


def test_a_pull_request_red_only_from_shared_checks_is_held():
    others = [[run("Tests", "FAILURE"), run("Semgrep", "FAILURE")]] * 3
    assert fleet_red.held(fleet([run("Tests", "FAILURE"), run("Semgrep", "FAILURE")], *others), MINE)


def test_a_pull_request_with_its_own_failure_beside_a_shared_one_is_not_held():
    others = [[run("Tests", "FAILURE"), run("Lint", "SUCCESS")]] * 3
    assert not fleet_red.held(fleet([run("Tests", "FAILURE"), run("Lint", "FAILURE")], *others), MINE)


def test_a_green_or_unlisted_pull_request_is_not_held():
    others = [[run("Tests", "FAILURE")]] * 3
    assert not fleet_red.held(fleet([run("Tests", "SUCCESS")], *others), MINE)
    assert not fleet_red.held(fleet([run("Tests", "FAILURE")], *others), "https://github.com/o/r/pull/99")


def test_the_checker_reads_open_pull_requests_once_and_only_for_an_open_task_with_a_pull_request():
    reads = []
    pulls = fleet([run("Tests", "FAILURE")], *[[run("Tests", "FAILURE")]] * 3)
    admits = fleet_red.checker("/repo", lambda repo: reads.append(repo) or pulls)
    assert admits(task("plain"))
    assert admits(task("done", state="done", pr_url=MINE))
    assert reads == []
    assert not admits(task("red", pr_url=MINE))
    assert admits(task("other", pr_url="https://github.com/o/r/pull/3"))
    assert reads == ["/repo"]


def test_a_failed_read_admits_every_task():
    def broken(repo):
        raise fleet_red.ReadError("gh pr list exited 1")

    assert fleet_red.checker("/repo", broken)(task("red", pr_url=MINE))


def test_read_lists_open_pull_requests_with_their_check_rollup():
    calls = []

    class Done:
        returncode, stdout, stderr = 0, '[{"url": "u", "statusCheckRollup": []}]', ""

    found = fleet_red.read("/repo", lambda *args, **kwargs: calls.append((args, kwargs)) or Done())
    assert found == [{"url": "u", "statusCheckRollup": []}]
    ((args, kwargs),) = calls
    assert args == (["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", "url,statusCheckRollup"],)
    assert kwargs["cwd"] == "/repo" and kwargs["timeout"] == 60


def test_read_raises_a_read_error_on_a_failed_listing():
    class Failed:
        returncode, stdout, stderr = 1, "", "no auth"

    with pytest.raises(fleet_red.ReadError, match="no auth"):
        fleet_red.read("/repo", lambda *args, **kwargs: Failed())


@pytest.fixture
def store(tmp_path):
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", str(tmp_path), max_eng=2, max_ci=0))
    return saved


def test_the_tick_holds_a_task_red_only_from_a_shared_check_and_releases_it_once_the_check_passes(store, monkeypatch):
    rows = {t["id"]: t for t in [task("red", pr_url=MINE, awaiting="fix"), task("plain")]}
    doc = {"phases": [], "tasks": list(rows.values())}
    others = [[run("Tests", "FAILURE")]] * 3
    monkeypatch.setattr(fleet_red, "read", lambda repo: fleet([run("Tests", "FAILURE")], *others))
    assert [t["id"] for t in _claimable("sw", store, rows, doc, "eng")] == ["plain"]
    passed = [[run("Tests", "SUCCESS")]] * 3
    monkeypatch.setattr(fleet_red, "read", lambda repo: fleet([run("Tests", "FAILURE")], *passed))
    assert sorted(t["id"] for t in _claimable("sw", store, rows, doc, "eng")) == ["plain", "red"]
