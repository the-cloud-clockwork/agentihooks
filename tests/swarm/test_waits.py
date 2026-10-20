import json

import pytest

from scripts.gates import intent
from scripts.gates.verdicts import Verdicts
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import cli, idle, merge_queue, waits
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.ledger_events import head as github_head
from scripts.swarm.ledger_events import view as github_view
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_ledger_events import APP_SUITE, QUEUED_TESTS, SKIPPED_ONLY

REAL_REQUIRED_GREEN = waits.required_green


ME = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/7"
NO_SUITES = {"nodes": [], "pageInfo": {"hasNextPage": False}}


@pytest.fixture
def started(env, monkeypatch):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setattr(cli, "now_ms", lambda: 1_000)
    ledger.tasks = lambda slug: list(ledger.rows.values()) if slug == "sw" else []
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: PullRequest("MERGED", 2, 1, False, head="first"))
    monkeypatch.setattr(cli.ledger_events, "head", lambda url: "first")
    return store, ledger


def held(store):
    return idle.wait(store.redis, "sw", ME)


def test_a_wait_on_checks_lasts_until_the_tick_ends_it(started, capsys):
    store, _ = started
    assert run("sw", "--as", ME, "wait", "--on", "checks", URL, "--reason", "tests") == 0
    assert held(store) == {
        "until": 1_000 + waits.CHECKED_MINUTES * 60_000,
        "reason": "tests",
        "at": 1_000,
        "on": {"kind": "checks", "target": URL, "head": "first"},
    }
    assert json.loads(capsys.readouterr().out) == {
        "agent": ME,
        "until": "1970-01-01T12:00:01+00:00",
        "on": {"kind": "checks", "target": URL, "head": "first"},
    }


def test_the_wait_usage_names_its_kind_and_target(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "wait", "--help"])
    assert "--on KIND TARGET" in capsys.readouterr().out


def test_minutes_bound_a_checked_wait(started):
    store, _ = started
    assert run("sw", "--as", ME, "wait", "90", "--on", "task", "t2") == 0
    assert held(store)["until"] == 1_000 + 90 * 60_000


def test_a_wait_on_a_reply_names_an_inbox_item_that_exists(started, capsys):
    store, _ = started
    item = InboxStore(store.redis).send(ME, "master@sw", "which way?")
    assert run("sw", "--as", ME, "wait", "--on", "reply", item.id) == 0
    assert held(store)["on"] == {"kind": "reply", "target": item.id}
    assert run("sw", "--as", ME, "wait", "--on", "reply", "nosuchitem") == 1
    assert "no inbox item nosuchitem" in capsys.readouterr().err


def test_checked_wait_targets_are_checked(started, capsys):
    assert run("sw", "--as", ME, "wait", "--on", "checks", "7") == 1
    assert "a pull request url" in capsys.readouterr().err
    assert run("sw", "--as", ME, "wait", "--on", "task", "t9") == 1
    assert "no task t9 on the ledger" in capsys.readouterr().err
    assert run("sw", "--as", ME, "wait", "--on", "task", "t1") == 1
    assert "task t1 is your own task" in capsys.readouterr().err
    assert run("sw", "--as", ME, "wait", "--on", "deploy", "x") == 1
    assert "wait on one of: checks, merge, reply, task" in capsys.readouterr().err


def test_a_bare_wait_is_capped_at_sixty_minutes(started, capsys):
    store, _ = started
    assert run("sw", "--as", ME, "wait", "1") == 0
    assert held(store)["until"] == 1_000 + 60_000
    assert run("sw", "--as", ME, "wait", "60") == 0
    assert held(store)["until"] == 1_000 + 60 * 60_000 and "on" not in held(store)
    assert run("sw", "--as", ME, "wait", "61") == 1
    assert "a bare wait lasts at most 60 minutes" in capsys.readouterr().err
    assert run("sw", "--as", ME, "wait") == 1
    assert "a wait lasts a whole number of minutes above zero" in capsys.readouterr().err


@pytest.mark.parametrize(
    "on, outcome", [(("checks", URL), f"pull request {URL}, now merged"), (("task", "t2"), "task t2, now done")]
)
def test_the_tick_ends_a_checked_wait_whose_thing_resolved(started, on, outcome):
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeRuntime

    store, ledger = started
    assert run("sw", "--as", ME, "wait", "--on", *on) == 0
    ledger.rows["t2"].update(state="done")
    actions = cli.run_tick(store, "sw", ledger, FakeRuntime(), FakeHerdr({}))
    assert f"ended the wait of {ME}: {outcome}" in actions
    assert held(store) is None


def test_target_problem_reads_each_kind():
    rows = {"t2": {"id": "t2", "state": "open"}, "t3": {"id": "t3", "state": "done"}}

    def get(item_id):
        if item_id != "abc":
            raise InboxError(f"no message {item_id}")

    assert waits.target_problem("checks", URL, "t1", rows, get) == ""
    assert waits.target_problem("checks", "http://github.com/o/r/pull/7", "t1", rows, get)
    assert waits.target_problem("checks", URL + "/files", "t1", rows, get)
    assert waits.target_problem("task", "t2", "t1", rows, get) == ""
    assert waits.target_problem("task", "t3", "t1", rows, get) == "task t3 is already done"
    assert waits.target_problem("reply", "abc", "t1", rows, get) == ""


@pytest.fixture
def tick():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    inbox = InboxStore(store.redis)
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
    pulls = {}

    def hold(kind, target):
        held = {"kind": kind, "target": target, **({"head": "first"} if kind == "checks" else {})}
        idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on=held)

    fresh = {}

    def reread(url):
        return fresh[url] if url in fresh else pulls.get(url)

    def end(rows=None, now=5_000):
        return waits.end_pass(store, "sw", rows or {}, inbox, pulls.get, now, reread)

    def told():
        return [item.text for item in inbox.inbox("eng-1@sw")]

    rig = type("Rig", (), {})()
    rig.store, rig.inbox, rig.pulls, rig.hold, rig.end, rig.told = store, inbox, pulls, hold, end, told
    rig.fresh = fresh
    return rig


@pytest.mark.parametrize(
    "pull, outcome",
    [
        (PullRequest("OPEN", None, 1, False, True, head="first"), f"checks on {URL}, now green"),
        (PullRequest("OPEN", None, 1, True, True, head="first"), f"checks on {URL}, now red"),
        (PullRequest("MERGED", 2, 1, False, True), f"pull request {URL}, now merged"),
        (PullRequest("CLOSED", None, 1, False, False), f"pull request {URL}, now closed"),
    ],
)
def test_the_tick_ends_a_checks_wait_once_and_tells_the_agent(tick, pull, outcome):
    tick.hold("checks", URL)
    tick.pulls[URL] = pull
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert idle.waited(tick.store.redis, "sw", ME) == 5_000
    assert idle.BEAT_TTL_S - 5 < tick.store.redis.ttl(idle.key("sw", "waited", ME)) <= idle.BEAT_TTL_S
    assert tick.told() == [
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    ]
    assert tick.end() == []
    assert len(tick.told()) == 1


@pytest.mark.parametrize("pull", [None, PullRequest("OPEN", None, 1, False, False, head="first")])
def test_a_checks_wait_stays_while_checks_run_or_github_cannot_answer(tick, pull):
    tick.hold("checks", URL)
    if pull:
        tick.pulls[URL] = pull
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["target"] == URL
    assert tick.told() == []


@pytest.mark.parametrize(
    "state, outcome",
    [
        ("OPEN", "now green; dropped from the merge queue; queue it again"),
        ("CLOSED", "now closed unmerged; reopen it or record why it was closed"),
    ],
)
def test_a_merge_wait_reports_when_the_queued_pull_request_drops_out(tick, state, outcome):
    tick.hold("merge", URL)
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, resolved=True, queued=True)
    assert tick.end() == []
    tick.pulls[URL] = None
    assert tick.end() == []
    tick.pulls[URL] = PullRequest(state, None, 1, False, resolved=True)
    assert tick.end() == [f"ended the wait of {ME}: pull request {URL}, {outcome}"]
    assert held(tick.store) is None
    assert outcome in tick.told()[0]
    assert tick.end() == []


def test_the_tick_accepts_a_merge_wait_only_after_the_pull_request_lands(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, resolved=True, queued=True)
    assert tick.end() == []
    assert held(tick.store)["on"] == {"kind": "merge", "target": URL, "queued": True}
    tick.pulls[URL] = PullRequest("MERGED", 2, 1, False)
    assert tick.end() == [f"ended the wait of {ME}: pull request {URL}, now merged"]
    assert held(tick.store) is None
    assert f"Your wait on pull request {URL}, now merged has ended." in tick.told()[0]
    assert tick.end() == []


def test_an_open_dependency_pull_request_holds_even_when_its_checks_are_red(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = PullRequest("OPEN", None, 1, True, resolved=True)
    rows = {"t1": {"id": "t1", "pr_url": URL + "0"}}
    assert tick.end(rows, now=1_000_000) == []
    assert held(tick.store)["on"] == {"kind": "merge", "target": URL}
    tick.pulls[URL] = PullRequest("MERGED", 2, 1, False)
    assert tick.end(rows) == [f"ended the wait of {ME}: pull request {URL}, now merged"]
    assert held(tick.store) is None


LEFT = f"pull request {URL}, now green; dropped from the merge queue; queue it again"
UNLISTED = PullRequest("OPEN", None, 1, False, resolved=True, head="first")
LISTED = PullRequest("OPEN", None, 1, False, resolved=True, head="first", queued=True)


def test_a_queue_read_that_lags_right_after_enqueue_keeps_the_merge_wait(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = UNLISTED
    tick.fresh[URL] = LISTED
    assert tick.end() == []
    assert held(tick.store)["on"] == {"kind": "merge", "target": URL, "queued": True}
    assert tick.told() == []


def test_a_fresh_entry_not_yet_visible_stays_queued_until_its_grace_runs_out(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = UNLISTED
    assert tick.end() == []
    assert tick.end(now=waits.FRESH_MS) == []
    assert held(tick.store)["on"] == {"kind": "merge", "target": URL}
    assert tick.end(now=1 + waits.FRESH_MS) == [f"ended the wait of {ME}: {LEFT}"]
    assert held(tick.store) is None


def test_a_fresh_entry_grace_lasts_five_minutes():
    assert waits.FRESH_MS == 300_000


@pytest.mark.parametrize(
    "pull, fresh, outcome",
    [
        (None, PullRequest("MERGED", 2, 1, False), ""),
        (PullRequest("MERGED", 2, 1, False), LISTED, f"pull request {URL}, now merged"),
        (LISTED, UNLISTED, ""),
    ],
)
def test_the_merge_wait_rereads_only_an_open_pull_request_the_queue_does_not_list(pull, fresh, outcome):
    on = {"kind": "merge", "target": URL}
    assert waits.resolution(on, {}, None, {URL: pull}.get, {URL: fresh}.get, False) == outcome
    assert on == {"kind": "merge", "target": URL, **({"queued": True} if pull is LISTED else {})}


@pytest.mark.parametrize(
    "current, expected",
    [
        (PullRequest("MERGED", 2, 1, False), "now merged"),
        (
            PullRequest("OPEN", None, 1, True, resolved=True, failed=("Unit tests", "Lint")),
            "now red; failed: Unit tests, Lint; fix the failing checks and queue it again",
        ),
        (
            PullRequest("OPEN", None, 1, True, resolved=True, unpassed_gate="Gate — Required"),
            "now red; failed: Gate — Required; fix the failing checks and queue it again",
        ),
        (
            PullRequest("OPEN", None, 1, True, resolved=True),
            "now red; failed: a required check; fix the failing checks and queue it again",
        ),
        (PullRequest("OPEN", None, 1, False, resolved=True), "now green; dropped from the merge queue; queue it again"),
        (
            PullRequest("OPEN", None, 1, True, resolved=True, failed=("Unit tests",)),
            "now red; failed: Unit tests; fix the failing checks and queue it again",
        ),
        (
            PullRequest("CLOSED", None, 1, False, resolved=True),
            "now closed unmerged; reopen it or record why it was closed",
        ),
    ],
)
def test_a_merge_wait_reports_the_current_outcome_and_next_step(current, expected):
    held = {"kind": "merge", "target": URL, "queued": True}
    stale = PullRequest("OPEN", None, 1, True, resolved=True, failed=("Old check",))
    assert waits.resolution(held, {}, None, lambda _: stale, lambda _: current, False) == (
        f"pull request {URL}, {expected}"
    )


def test_a_seen_entry_gets_no_fresh_grace_once_the_queue_drops_it():
    on = {"kind": "merge", "target": URL}
    assert waits.resolution(on, {}, None, {URL: UNLISTED}.get, {URL: UNLISTED}.get, True) == ""
    assert waits.resolution(on, {}, None, {URL: UNLISTED}.get, {URL: UNLISTED}.get, False) == LEFT
    on["queued"] = True
    assert waits.resolution(on, {}, None, {URL: UNLISTED}.get, {URL: UNLISTED}.get, True) == LEFT


def test_a_merge_wait_holds_while_the_unlisted_pull_request_still_awaits_checks(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = LISTED
    assert tick.end() == []
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, resolved=False, head="first")
    assert tick.end() == []
    assert held(tick.store)["on"] == {"kind": "merge", "target": URL, "queued": True}
    assert tick.told() == []


def test_a_pull_request_removed_after_a_failed_group_reports_left_once_the_reread_agrees(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = LISTED
    assert tick.end() == []
    tick.pulls[URL] = UNLISTED
    tick.fresh[URL] = None
    assert tick.end() == []
    tick.fresh[URL] = UNLISTED
    assert tick.end() == [f"ended the wait of {ME}: {LEFT}"]
    assert held(tick.store) is None


def test_a_reread_that_finds_the_pull_request_merged_reports_merged(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = UNLISTED
    tick.fresh[URL] = PullRequest("MERGED", 2, 1, False)
    assert tick.end() == [f"ended the wait of {ME}: pull request {URL}, now merged"]


def test_the_tick_rereads_the_queue_with_an_uncached_view(started, monkeypatch):
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeRuntime

    store, ledger = started
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    monkeypatch.setattr(cli.ledger_events, "tick_view", lambda *args: {URL: UNLISTED}.get)
    monkeypatch.setattr(cli.ledger_events, "view", {URL: LISTED}.get)
    cli.run_tick(store, "sw", ledger, FakeRuntime(), FakeHerdr({}))
    assert held(store)["on"] == {"kind": "merge", "target": URL, "queued": True}


def test_cli_records_a_checked_merge_wait_with_a_pull_request_url(started, capsys):
    store, _ = started
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert held(store)["on"] == {"kind": "merge", "target": URL}
    assert run("sw", "--as", ME, "wait", "--on", "merge", "bad-url") == 1
    assert "wait on merge needs a pull request url" in capsys.readouterr().err


@pytest.mark.parametrize(
    "check, outcome",
    [
        ({"conclusion": "CANCELLED"}, ""),
        ({"conclusion": "SKIPPED"}, "green"),
        ({"conclusion": "NEUTRAL"}, ""),
        ({"conclusion": "STALE"}, ""),
        ({"conclusion": ""}, ""),
        ({"status": "IN_PROGRESS"}, ""),
        ({"state": "PENDING"}, ""),
        ({"state": "EXPECTED"}, ""),
        ({"conclusion": "FAILURE"}, "red"),
        ({"conclusion": "ERROR"}, "red"),
        ({"conclusion": "TIMED_OUT"}, ""),
        ({"conclusion": "STARTUP_FAILURE"}, "red"),
        ({"conclusion": "ACTION_REQUIRED"}, "red"),
        ({"conclusion": "SUCCESS"}, "green"),
        ({"state": "SUCCESS"}, "green"),
    ],
)
@pytest.mark.parametrize("position", [0, 1])
def test_checks_wait_requires_success_or_failure_from_the_status_rollup(tick, check, outcome, position):
    from scripts.swarm.ledger_events import pull_request

    tick.hold("checks", URL)
    checks = [{"name": "lint", "conclusion": "SUCCESS"}]
    checks.insert(position, {"name": "mutation", **check})
    tick.pulls[URL] = pull_request({"state": "OPEN", "headRefOid": "first", "statusCheckRollup": checks})
    if not outcome:
        assert tick.end() == []
        assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "first"}
        assert tick.told() == []
        return
    outcome += "; failed: mutation" if outcome == "red" else ""
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [
        f"Your wait on checks on {URL}, now {outcome} has ended. Pick task t1 back up: "
        "agentihooks swarm sw done, block, or wait on the next thing."
    ]


@pytest.mark.parametrize("conclusion", [None, "CANCELLED", "SUCCESS", "FAILURE"])
@pytest.mark.parametrize("legacy_status", [False, True])
@pytest.mark.parametrize("obsolete", ["CANCELLED", "FAILURE"])
@pytest.mark.parametrize("reverse", [False, True])
def test_checks_wait_uses_only_the_newest_run_of_each_check(tick, conclusion, obsolete, reverse, legacy_status):
    from scripts.swarm.ledger_events import pull_request

    tick.hold("checks", URL)
    checks = [
        {"name": "Gate — Required", "databaseId": 1, "conclusion": obsolete},
        {"name": "Gate — Required", "databaseId": 2, "conclusion": conclusion},
    ]
    if legacy_status:
        checks = [
            {"context": "Gate — Required", "createdAt": f"2026-10-13T10:00:0{index}Z", "state": row["conclusion"]}
            for index, row in enumerate(checks)
        ]
    if reverse:
        checks.reverse()
    tick.pulls[URL] = pull_request({"state": "OPEN", "headRefOid": "first", "gated": True, "statusCheckRollup": checks})
    if conclusion in (None, "CANCELLED"):
        assert tick.end() == []
        assert idle.wait(tick.store.redis, "sw", ME) is not None
        assert tick.told() == []
    else:
        outcome = "green" if conclusion == "SUCCESS" else "red; failed: Gate — Required"
        assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now {outcome}"]
        assert idle.wait(tick.store.redis, "sw", ME) is None


def _run_info(run_id, workflow="W1", event="pull_request"):
    return {
        "databaseId": run_id,
        "event": event,
        "workflow": {"id": workflow, "name": "Tests", "resourcePath": f"/o/r/actions/workflows/{workflow}.yml"},
    }


def _run_check(name, check_id, run_id, conclusion, **run):
    return {
        "name": name,
        "databaseId": check_id,
        "conclusion": conclusion,
        "checkSuite": {"workflowRun": _run_info(run_id, **run)},
    }


def _suite(run_id, status, conclusion=None, **run):
    return {
        "status": status,
        "conclusion": conclusion,
        "workflowRun": {**_run_info(run_id, **run), "createdAt": "2026-10-17T10:00:00Z"},
    }


CANCELLED_RUN = [
    {
        "name": "lint",
        "conclusion": "SUCCESS",
        "checkSuite": {"workflowRun": {"event": "pull_request", "workflow": {"id": "W1", "name": "Tests"}}},
    },
    _run_check("unit", 10, 1, "CANCELLED"),
    _run_check("Gate — Required", 11, 1, "FAILURE"),
    {"context": "sonar", "state": "SUCCESS"},
]


def _gated(checks, suites, reverse, gated=True, workflows=()):
    from scripts.swarm.ledger_events import pull_request

    if reverse:
        checks, suites = checks[::-1], suites[::-1]
    return pull_request(
        {
            "state": "OPEN",
            "headRefOid": "first",
            "gated": gated,
            "gate_workflows": workflows,
            "statusCheckRollup": checks,
            "checkSuites": suites,
        }
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_a_cancelled_older_run_holds_the_wait_until_the_newer_run_reports_its_gate(tick, reverse):
    tick.hold("checks", URL)
    checks = CANCELLED_RUN + [_run_check("unit", 20, 2, "SUCCESS")]
    suites = [_suite(1, "COMPLETED", "CANCELLED"), _suite(2, "IN_PROGRESS")]
    tick.pulls[URL] = _gated(checks, suites, reverse)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) is not None


@pytest.mark.parametrize("reverse", [False, True])
def test_a_cancelled_older_run_and_a_green_newer_run_end_the_wait_green(tick, reverse):
    tick.hold("checks", URL)
    checks = CANCELLED_RUN + [_run_check("unit", 20, 2, "SUCCESS"), _run_check("Gate — Required", 9, 2, "SUCCESS")]
    suites = [_suite(1, "COMPLETED", "CANCELLED"), _suite(2, "COMPLETED", "SUCCESS")]
    tick.pulls[URL] = _gated(checks, suites, reverse)
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]
    assert idle.wait(tick.store.redis, "sw", ME) is None


def test_a_newer_run_known_only_from_its_suite_holds_the_wait(tick):
    tick.hold("checks", URL)
    checks = [_run_check("Gate — Required", 20, 2, "SUCCESS")]
    tick.pulls[URL] = _gated(checks, [_suite(2, "COMPLETED", "SUCCESS"), _suite(3, "QUEUED")], False)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) is not None


def test_a_check_whose_run_names_no_workflow_is_never_superseded(tick):
    from scripts.swarm.ledger_events import pull_request

    tick.hold("checks", URL)
    run = {"databaseId": 4, "event": "pull_request"}
    lint = {"name": "lint", "databaseId": 1, "conclusion": "FAILURE", "checkSuite": {"workflowRun": run}}
    suites = [{"status": "COMPLETED", "workflowRun": {**run, "databaseId": 5}}]
    tick.pulls[URL] = pull_request(
        {"state": "OPEN", "headRefOid": "first", "statusCheckRollup": [lint], "checkSuites": suites}
    )
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now red; failed: lint"]


@pytest.mark.parametrize("newer", [{"event": "push"}, {"workflow": "W2"}])
def test_a_newer_run_of_another_event_or_workflow_keeps_the_older_red_check(tick, newer):
    tick.hold("checks", URL)
    checks = [_run_check("e2e", 10, 5, "FAILURE"), _run_check("unit", 20, 6, "SUCCESS", **newer)]
    checks.append(_run_check("e2e", 30, 6, "SUCCESS", **newer))
    suites = [_suite(5, "COMPLETED", "FAILURE"), _suite(6, "COMPLETED", "SUCCESS", **newer)]
    tick.pulls[URL] = _gated(checks, suites, False, gated=False)
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now red; failed: e2e"]


def test_a_superseded_run_still_in_progress_does_not_hold_a_green_wait(tick):
    from scripts.swarm.ledger_events import pull_request

    tick.hold("checks", URL)
    checks = [_run_check("unit", 10, 1, None), _run_check("unit", 20, 2, "SUCCESS")]
    suites = [_suite(1, "IN_PROGRESS"), _suite(2, "COMPLETED", "SUCCESS")]
    tick.pulls[URL] = pull_request(
        {"state": "OPEN", "headRefOid": "first", "statusCheckRollup": checks, "checkSuites": suites}
    )
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]


@pytest.mark.parametrize("reverse", [False, True])
def test_a_rerun_attempt_of_one_run_is_judged_by_its_newest_check(tick, reverse):
    tick.hold("checks", URL)
    checks = [_run_check("unit", 10, 4, "FAILURE"), _run_check("unit", 20, 4, "SUCCESS")]
    tick.pulls[URL] = _gated(checks, [_suite(4, "COMPLETED", "SUCCESS")], reverse, gated=False)
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]


def test_the_queue_reading_of_required_checks_skips_a_superseded_run():
    from scripts.swarm.ledger_events import _required_pull_request

    checks = [
        {**_run_check("Gate — Required", 11, 1, "FAILURE"), "isRequired": True},
        {**_run_check("Gate — Required", 9, 2, "SUCCESS"), "isRequired": True},
    ]
    raw = {
        "state": "OPEN",
        "headRefOid": "first",
        "statusCheckRollup": checks,
        "checkSuites": [_suite(1, "COMPLETED", "CANCELLED"), _suite(2, "COMPLETED", "SUCCESS")],
    }
    pull = _required_pull_request(raw, {"Gate — Required": None})
    assert (pull.resolved, pull.red, pull.failed) == (True, False, ())


def test_checks_wait_with_no_checks_stays_unresolved(tick):
    from scripts.swarm.ledger_events import pull_request

    tick.hold("checks", URL)
    tick.pulls[URL] = pull_request({"state": "OPEN", "headRefOid": "first", "statusCheckRollup": []})
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "first"}
    assert tick.told() == []


@pytest.mark.parametrize("unresolved", ["CANCELLED", "TIMED_OUT", "PENDING"])
@pytest.mark.parametrize("failure", ["FAILURE", "ERROR", "STARTUP_FAILURE", "ACTION_REQUIRED"])
def test_a_failure_ends_the_checks_wait_even_with_an_unresolved_check(tick, unresolved, failure):
    from scripts.swarm.ledger_events import pull_request

    tick.hold("checks", URL)
    tick.pulls[URL] = pull_request(
        {
            "state": "OPEN",
            "headRefOid": "first",
            "statusCheckRollup": [
                {"name": "mutation", "conclusion": unresolved},
                {"name": "lint", "conclusion": failure},
            ],
        }
    )
    failed = "mutation, lint" if unresolved == "TIMED_OUT" else "lint"
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now red; failed: {failed}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [
        f"Your wait on checks on {URL}, now red; failed: {failed} has ended. Pick task t1 back up: "
        "agentihooks swarm sw done, block, or wait on the next thing."
    ]


@pytest.mark.parametrize("state, ends", [("done", True), ("blocked", True), ("claimed", False), ("pr", False)])
def test_a_task_wait_ends_when_the_task_is_done_or_blocked(tick, state, ends):
    tick.hold("task", "t2")
    ended = tick.end({"t2": {"id": "t2", "state": state}})
    assert ended == ([f"ended the wait of {ME}: task t2, now {state}"] if ends else [])


def test_a_task_wait_ends_when_the_task_leaves_the_ledger(tick):
    tick.hold("task", "t2")
    assert tick.end({}) == [f"ended the wait of {ME}: task t2, gone from the ledger"]


def test_a_reply_wait_ends_when_its_item_closes(tick):
    item = tick.inbox.send(ME, "master@sw", "which way?")
    tick.hold("reply", item.id)
    assert tick.end() == []
    tick.inbox.reply(item.id, "master@sw", "left")
    assert tick.end() == [f"ended the wait of {ME}: message {item.id}, now done"]


def test_a_reply_wait_on_a_vanished_item_ends(tick):
    tick.hold("reply", "gone")
    assert tick.end() == [f"ended the wait of {ME}: message gone, gone from the inbox"]


def test_bare_waits_and_finished_agents_are_left_alone(tick):
    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "deploy", 1)
    assert tick.end() == []
    tick.hold("task", "t2")
    tick.store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw", state="finished"))
    assert tick.end({}) == []


def test_a_wait_end_is_kept_a_day_past_the_wait(tick):
    redis, day = tick.store.redis, idle.BEAT_TTL_S * 1000
    idle.declare_wait(redis, "sw", ME, 61_000, "deploy", 1_000)
    assert idle.waited(redis, "sw", ME) == 61_000
    assert day + 55_000 < redis.pttl(idle.key("sw", "waited", ME)) <= day + 60_000
    idle.end_wait(redis, "sw", ME, 2_000)
    assert (idle.wait(redis, "sw", ME), idle.waited(redis, "sw", ME)) == (None, 2_000)
    assert day - 5_000 < redis.pttl(idle.key("sw", "waited", ME)) <= day


@pytest.mark.parametrize(
    "entry, named",
    [
        ({"reason": "deploy"}, True),
        ({"reason": "", "on": {"kind": "task", "target": "t2"}}, True),
        ({"on": {"kind": "reply", "target": "abc"}}, True),
        ({"reason": "  "}, False),
        ({"reason": ""}, False),
        ({}, False),
    ],
)
def test_a_wait_is_named_by_its_target_or_its_reason(entry, named):
    assert idle.named(entry) is named


def test_a_skipped_agent_never_stops_the_pass_for_the_next(tick):
    bare, running, done = (f"engineer@a1b2c3-000{n}" for n in (0, 2, 3))
    for name in (bare, running, done):
        tick.store.put_agent("sw", AgentRecord(name=name, lane="eng", task="t1"))
    tick.store.drop_agent("sw", ME)
    idle.declare_wait(tick.store.redis, "sw", bare, 10_000_000, "deploy", 1)
    idle.declare_wait(tick.store.redis, "sw", running, 10_000_000, "", 1, on={"kind": "task", "target": "t2"})
    idle.declare_wait(tick.store.redis, "sw", done, 10_000_000, "", 1, on={"kind": "task", "target": "t3"})
    rows = {"t2": {"id": "t2", "state": "claimed"}, "t3": {"id": "t3", "state": "done"}}
    assert tick.end(rows) == [f"ended the wait of {done}: task t3, now done"]


def test_an_agent_without_a_seat_is_told_by_name(tick):
    tick.store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1"))
    tick.hold("task", "t2")
    tick.end({})
    assert [item.text for item in tick.inbox.inbox(ME)][0].startswith("Your wait on task t2")


def test_cli_wait_refuses_a_checked_wait_for_an_unknown_kind():
    with pytest.raises(SwarmError, match="wait on one of: checks, merge, reply, task"):
        waits.on("deploy", "x")


def test_inbox_wait_returns_pending_seat_work_without_a_pane_prompt(started, capsys):
    store, _ = started
    capsys.readouterr()
    store.seats.occupy("master@sw", ME, at=1)
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", "master@sw", "inspect the result")
    assert run("sw", "--as", ME, "wait", "--inbox") == 0
    result = json.loads(capsys.readouterr().out)
    returned = {entry["id"]: entry for entry in result["items"]}
    assert returned[item.id]["text"] == item.text
    assert result["timed_out"] is False
    assert held(store) is None


@pytest.mark.parametrize("minutes", ["0", "-1", "61"])
def test_inbox_wait_refuses_unbounded_duration(started, minutes, capsys, monkeypatch):
    from scripts.inbox import receive

    monkeypatch.setattr(receive, "receive", lambda *args: [])
    capsys.readouterr()
    assert run("sw", "--as", ME, "wait", minutes, "--inbox") == 1
    assert (
        capsys.readouterr().err
        == "swarm: an inbox wait takes one to sixty minutes and cannot also wait on a dependency\n"
    )


def test_inbox_wait_cannot_also_wait_on_checks(started, monkeypatch):
    from scripts.inbox import receive

    def unexpected(*args):
        pytest.fail("a conflicting dependency wait must be refused before receiving")

    monkeypatch.setattr(receive, "receive", unexpected)
    assert run("sw", "--as", ME, "wait", "--inbox", "--on", "checks", URL) == 1


def test_inbox_wait_timeout_and_failure_clear_the_declared_wait(started, monkeypatch, capsys):
    from scripts.inbox import receive

    store, _ = started
    capsys.readouterr()
    calls = []

    def empty(inbox, me, timeout):
        calls.append((me, timeout, held(store)))
        return []

    monkeypatch.setattr(receive, "receive", empty)
    assert run("sw", "--as", ME, "wait", "2", "--inbox") == 0
    assert json.loads(capsys.readouterr().out) == {"items": [], "timed_out": True}
    assert calls[0][:2] == (ME, 120)
    assert calls == [(ME, 120, {"until": 121_000, "reason": "inbox work", "at": 1_000})]
    calls.clear()
    assert run("sw", "--as", ME, "wait", "--inbox") == 0
    capsys.readouterr()
    assert calls == [(ME, 60, {"until": 61_000, "reason": "inbox work", "at": 1_000})]
    calls.clear()
    assert run("sw", "--as", ME, "wait", "60", "--inbox") == 0
    capsys.readouterr()
    assert calls == [(ME, 3600, {"until": 3_601_000, "reason": "inbox work", "at": 1_000})]
    assert held(store) is None
    assert idle.waited(store.redis, "sw", ME) == 1_000

    def failed(*args):
        raise InboxError("store unavailable")

    monkeypatch.setattr(receive, "receive", failed)
    assert run("sw", "--as", ME, "wait", "--inbox") == 1
    assert held(store) is None


@pytest.mark.parametrize(
    "command, action",
    [
        (("done", "--pr", URL), "done"),
        (("block", "needs a token"), "a block"),
        (("wait", "--on", "checks", URL), "a new wait"),
        (("progress", "--doing", "fixing", "--ends-when", "green"), "progress"),
    ],
)
def test_acting_on_the_task_closes_its_wait_ended_notice_with_the_action(started, command, action):
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeRuntime

    store, ledger = started
    inbox = InboxStore(store.redis)
    [agent] = [a for a in store.agents("sw") if a.name == ME]
    address = agent.seat or agent.name
    earlier = inbox.send("master@sw", address, "an unrelated question")
    assert run("sw", "--as", ME, "wait", "--on", "task", "t2") == 0
    ledger.rows["t2"].update(state="done")
    cli.run_tick(store, "sw", ledger, FakeRuntime(), FakeHerdr({}))
    [notice] = [item for item in inbox.inbox(address) if item.text.startswith("Your wait on")]
    inbox.deliver(notice.id, ME)
    quoted = inbox.send("master@sw", address, "You were told: Pick task t1 back up: then?")
    assert run("sw", "--as", ME, *command) == 0
    closed = inbox.get(notice.id)
    assert (closed.state, closed.reason) == ("done", f"done: {ME} recorded {action} on task t1")
    assert [inbox.get(item.id).state for item in (earlier, quoted)] == ["pending", "pending"]


def test_a_wait_ended_notice_for_another_task_stays_open(tick):
    tick.hold("task", "t2")
    tick.end({})
    [notice] = tick.inbox.inbox("eng-1@sw")
    tick.store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t11", seat="eng-1@sw"))
    [agent] = tick.store.agents("sw")
    waits.settle_notices(tick.inbox, agent, "progress")
    assert tick.inbox.get(notice.id).state == "pending"


def test_a_handoff_closes_its_wait_ended_notice_before_the_seat_passes_on(started, tmp_path):
    from tests.swarm.test_cli import _handoff_doc
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeRuntime

    store, ledger = started
    inbox = InboxStore(store.redis)
    [agent] = [a for a in store.agents("sw") if a.name == ME]
    assert run("sw", "--as", ME, "wait", "--on", "task", "t2") == 0
    ledger.rows["t2"].update(state="done")
    cli.run_tick(store, "sw", ledger, FakeRuntime(), FakeHerdr({}))
    [notice] = [item for item in inbox.inbox(agent.seat) if item.text.startswith("Your wait on")]
    assert waits.notice_task(notice) == "t1"
    inbox.deliver(notice.id, ME)
    assert run("sw", "--as", ME, "handoff", str(_handoff_doc(tmp_path))) == 0
    closed = inbox.get(notice.id)
    assert (closed.state, closed.reason) == ("done", f"done: {ME} recorded a handoff on task t1")


def test_notice_task_names_the_task_only_a_swarm_wait_ended_notice_asks_back():
    from scripts.inbox.store import Item

    text = f"Your wait on checks on {URL}, now red has ended. Pick task t13 back up: agentihooks swarm sw done"
    notice = Item(id="n", sender="swarm", address="eng-1@sw", text=text, state="delivered", created_at=1, updated_at=1)
    assert waits.notice_task(notice) == "t13"
    assert waits.notice_task(Item(**{**notice.__dict__, "sender": "master@sw"})) == ""
    assert waits.notice_task(Item(**{**notice.__dict__, "text": "Pick task t13 back up: then"})) == ""


def test_checks_declaration_records_the_remote_head(started, monkeypatch, capsys):
    store, _ = started
    calls = []

    def head(url):
        calls.append(url)
        return "first"

    monkeypatch.setattr(cli.ledger_events, "head", head)
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("the checks view was read"))
    assert run("sw", "--as", ME, "wait", "--on", "checks", URL) == 0
    assert held(store)["on"] == {"kind": "checks", "target": URL, "head": "first"}
    assert json.loads(capsys.readouterr().out)["on"] == held(store)["on"]
    assert calls == [URL]


def test_checks_declaration_refuses_an_unknown_head(started, monkeypatch, capsys):
    store, _ = started
    monkeypatch.setattr(cli.ledger_events, "head", lambda url: "")
    assert run("sw", "--as", ME, "wait", "--on", "checks", URL) == 1
    assert held(store) is None
    assert capsys.readouterr().err == "swarm: cannot read the pull request head; retry the checks wait\n"


GH_2_4 = """#!/usr/bin/env bash
case "$1 $2" in
  "api graphql") echo "HTTP 502: Bad Gateway (https://api.github.com/graphql)" >&2; exit 1 ;;
  "pr view") [[ "$*" == *headRefOid* ]] && { echo 'Unknown JSON field: "headRefOid"' >&2; exit 1; } ;;
  "api repos/o/r/pulls/7") echo '{"state": "open", "head": {"sha": "first", "ref": "branch"}}' ;;
  *) echo "unexpected gh $*" >&2; exit 1 ;;
esac
"""


def test_a_checks_wait_reads_the_head_through_rest_on_gh_2_4(started, monkeypatch, tmp_path, capsys):
    import os

    store, _ = started
    gh = tmp_path / "gh"
    gh.write_text(GH_2_4)
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cli.ledger_events, "view", github_view)
    monkeypatch.setattr(cli.ledger_events, "head", github_head)
    assert run("sw", "--as", ME, "wait", "--on", "checks", URL) == 0, capsys.readouterr().err
    assert held(store)["on"] == {"kind": "checks", "target": URL, "head": "first"}


@pytest.mark.parametrize(
    ("code", "out", "found"),
    [
        (0, '{"head": {"sha": "first"}}', "first"),
        (1, '{"head": {"sha": "first"}}', ""),
        (0, "not json", ""),
        (0, '{"head": {}}', ""),
        (0, '{"head": null}', ""),
    ],
)
def test_the_head_read_asks_the_rest_pulls_endpoint(code, out, found):
    from types import SimpleNamespace

    from scripts.swarm.ledger_events import head

    calls = []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=code, stdout=out, stderr="refused")

    assert head(URL, run) == found
    assert calls == [(["gh", "api", "repos/o/r/pulls/7"], {"capture_output": True, "text": True, "timeout": 20})]


def test_the_head_read_is_empty_when_gh_cannot_run():
    import subprocess

    from scripts.swarm.ledger_events import head

    def run(command, **options):
        raise subprocess.TimeoutExpired(command, 20)

    assert head(URL, run) == ""


def test_pull_request_reads_the_head_commit():
    from scripts.swarm.ledger_events import pull_request

    assert pull_request({"state": "OPEN", "headRefOid": "first"}).head == "first"
    assert pull_request({"state": "OPEN"}).head == ""


@pytest.mark.parametrize("red", [False, True])
def test_a_new_head_resets_checks_before_current_head_resolution(tick, red):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    tick.pulls[URL] = SimpleNamespace(state="OPEN", head="first", resolved=False, red=False, unpassed_gate="")
    assert tick.end() == []
    before = idle.wait(tick.store.redis, "sw", ME)
    assert before["on"]["head"] == "first"
    tick.pulls[URL] = SimpleNamespace(state="OPEN", head="second", resolved=True, red=red, unpassed_gate="", failed=())
    assert tick.end() == []
    after = idle.wait(tick.store.redis, "sw", ME)
    assert after == {**before, "on": {"kind": "checks", "target": URL, "head": "second"}}
    assert tick.told() == []
    outcome = "red" if red else "green"
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None


@pytest.mark.parametrize("confirmation", [None, ("second", True), ("first", False), ("", True)])
def test_a_push_or_missing_checks_during_resolution_keeps_the_wait(tick, confirmation):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    current = SimpleNamespace(state="OPEN", head="first", resolved=False, red=False, unpassed_gate="")
    tick.pulls[URL] = current
    assert tick.end() == []
    current.resolved = True
    latest = (
        SimpleNamespace(state="OPEN", head=confirmation[0], resolved=confirmation[1], red=False, unpassed_gate="")
        if confirmation
        else None
    )
    replies = iter([current, latest])
    assert waits.end_pass(tick.store, "sw", {}, tick.inbox, lambda url: next(replies), 5_000, None) == []
    assert tick.told() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["head"] == (
        "second" if confirmation and confirmation[0] == "second" else "first"
    )


def test_the_probe_requests_the_head_with_its_check_rollup():
    from types import SimpleNamespace

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "resource": {
                            "state": "OPEN",
                            "headRefOid": "first",
                            "mergeQueueEntry": {"id": "entry"},
                            "commits": {
                                "nodes": [
                                    {
                                        "commit": {
                                            "committedDate": "2026-10-07T17:00:00Z",
                                            "statusCheckRollup": {
                                                "contexts": {
                                                    "nodes": [{"name": "lint", "conclusion": "SUCCESS"}],
                                                    "pageInfo": {"hasNextPage": False},
                                                }
                                            },
                                            "checkSuites": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                                        }
                                    }
                                ]
                            },
                        }
                    }
                }
            ),
        )

    pull = github_view(URL, run)
    assert pull.head == "first"
    assert pull.resolved is True
    assert pull.red is False
    assert pull.pushed_at == 1791392400000
    assert pull.queued is True
    assert calls == [
        (
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "query=query($url:URI!){resource(url:$url){...on PullRequest{state mergedAt headRefOid mergeQueueEntry{id state} mergeable isDraft body "
                "timelineItems(last:1,itemTypes:[REOPENED_EVENT,ADDED_TO_MERGE_QUEUE_EVENT])"
                "{nodes{...on ReopenedEvent{createdAt} ...on AddedToMergeQueueEvent{createdAt}}} "
                "commits(last:1){nodes{commit{id oid committedDate "
                'file(path:".github/workflows"){object{...on Tree{entries{name object{...on Blob{text}}}}}} '
                "statusCheckRollup{contexts(first:100){"
                "nodes{...on CheckRun{databaseId name conclusion startedAt completedAt isRequired(pullRequestNumber:7) "
                "checkSuite{app{databaseId} workflowRun{databaseId event workflow{id name resourcePath}}}} "
                "...on StatusContext{context state createdAt isRequired(pullRequestNumber:7)}} "
                "pageInfo{hasNextPage endCursor}}} checkSuites(first:100){"
                "nodes{status conclusion workflowRun{databaseId createdAt event workflow{id resourcePath}}} "
                "pageInfo{hasNextPage endCursor}}}}}}}}",
                "-f",
                f"url={URL}",
            ],
            {"capture_output": True, "text": True, "timeout": 20},
        )
    ]


@pytest.mark.parametrize(
    "commits",
    [[], [{"commit": {"committedDate": "2026-10-07T17:00:00Z", "statusCheckRollup": None, "checkSuites": NO_SUITES}}]],
)
def test_the_probe_without_checks_is_unresolved(commits):
    from types import SimpleNamespace

    raw = {"data": {"resource": {"state": "OPEN", "headRefOid": "first", "commits": {"nodes": commits}}}}
    pull = github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(raw)))
    assert pull.head == "first"
    assert pull.resolved is False


def probe(rollup, suites, suites_more=False):
    from types import SimpleNamespace

    commit = {
        "committedDate": "2026-10-07T17:00:00Z",
        "statusCheckRollup": {"contexts": {"nodes": rollup, "pageInfo": {"hasNextPage": False}}},
        "checkSuites": {"nodes": suites, "pageInfo": {"hasNextPage": suites_more}},
    }
    raw = {"data": {"resource": {"state": "OPEN", "headRefOid": "second", "commits": {"nodes": [{"commit": commit}]}}}}
    return github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(raw)))


def test_the_probe_keeps_a_head_with_only_skipped_checks_and_a_queued_run_unresolved():
    pull = probe(SKIPPED_ONLY, [QUEUED_TESTS, APP_SUITE])
    assert pull.resolved is False


@pytest.mark.parametrize("suites", [None, {"nodes": None, "pageInfo": {"hasNextPage": False}}, {"nodes": []}])
def test_the_probe_refuses_unreadable_check_suites(suites):
    from types import SimpleNamespace

    commit = {
        "committedDate": "2026-10-07T17:00:00Z",
        "statusCheckRollup": {"contexts": {"nodes": SKIPPED_ONLY, "pageInfo": {"hasNextPage": False}}},
        "checkSuites": suites,
    }
    raw = {"data": {"resource": {"state": "OPEN", "headRefOid": "second", "commits": {"nodes": [{"commit": commit}]}}}}
    assert github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(raw))) is None


def test_the_probe_ignores_a_queued_suite_without_a_workflow_run():
    pull = probe(SKIPPED_ONLY + [{"name": "unit", "conclusion": "SUCCESS"}], [APP_SUITE])
    assert pull.resolved is True
    assert pull.red is False


def test_the_probe_refuses_a_partial_list_of_check_suites():
    assert probe(SKIPPED_ONLY, [APP_SUITE], suites_more=True) is None


def paged(*pages):
    from types import SimpleNamespace

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps(pages[len(calls) - 1]))

    return run, calls


def big_head(contexts, suites):
    commit = {
        "id": "C_head",
        "committedDate": "2026-10-07T17:00:00Z",
        "statusCheckRollup": {"contexts": contexts},
        "checkSuites": suites,
    }
    return {"data": {"resource": {"state": "OPEN", "headRefOid": "second", "commits": {"nodes": [{"commit": commit}]}}}}


@pytest.mark.parametrize("last", ["SUCCESS", "FAILURE"])
def test_the_probe_pages_through_more_than_one_hundred_contexts(last):
    first = [{"name": f"job {n}", "conclusion": "SUCCESS", "databaseId": n} for n in range(100)]
    rest = [{"name": f"job {n}", "conclusion": "SUCCESS", "databaseId": n} for n in range(100, 129)]
    rest.append({"name": "job 129", "conclusion": last, "databaseId": 129})
    run, calls = paged(
        big_head(
            {"nodes": first, "pageInfo": {"hasNextPage": True, "endCursor": "c100"}},
            {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "s100"}},
        ),
        {"data": {"node": {"statusCheckRollup": {"contexts": {"nodes": rest, "pageInfo": {"hasNextPage": False}}}}}},
        {"data": {"node": {"checkSuites": {"nodes": [APP_SUITE], "pageInfo": {"hasNextPage": False}}}}},
    )
    pull = github_view(URL, run)
    assert pull.resolved is True
    assert pull.red is (last == "FAILURE")
    assert pull.failed == (("job 129",) if last == "FAILURE" else ())
    assert len(calls) == 3
    assert calls[1][5:] == ["-f", "id=C_head", "-f", "after=c100"]
    assert calls[2][5:] == ["-f", "id=C_head", "-f", "after=s100"]
    assert "contexts(first:100,after:$after)" in calls[1][4] and "checkSuites(first:100,after:$after)" in calls[2][4]


@pytest.mark.parametrize(
    "page",
    [
        {"errors": [{"message": "rate limited"}]},
        {"data": {"node": None}},
        {"data": {"node": {"statusCheckRollup": {"contexts": {"nodes": None, "pageInfo": {"hasNextPage": False}}}}}},
    ],
)
def test_the_probe_refuses_a_context_page_it_cannot_read(page):
    first = [{"name": "unit", "conclusion": "SUCCESS", "databaseId": 1}]
    run, _ = paged(
        big_head({"nodes": first, "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}, NO_SUITES),
        page,
    )
    assert github_view(URL, run) is None


def test_the_probe_refuses_a_next_page_without_a_cursor():
    first = [{"name": "unit", "conclusion": "SUCCESS", "databaseId": 1}]
    run, calls = paged(big_head({"nodes": first, "pageInfo": {"hasNextPage": True}}, NO_SUITES))
    assert github_view(URL, run) is None
    assert len(calls) == 1


@pytest.mark.parametrize("more", [False, True])
def test_the_probe_reads_up_to_its_page_cap_and_refuses_beyond(more):
    from scripts.swarm.ledger_events import MAX_PAGES

    def page(n, last):
        info = {"hasNextPage": not last or more, "endCursor": f"c{n}"}
        nodes = [{"name": f"job {n}", "conclusion": "SUCCESS", "databaseId": n}]
        return {"data": {"node": {"statusCheckRollup": {"contexts": {"nodes": nodes, "pageInfo": info}}}}}

    first = {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "c0"}}
    pages = [page(n, n == MAX_PAGES) for n in range(1, MAX_PAGES + 1)]
    run, calls = paged(big_head(first, NO_SUITES), *pages)
    pull = github_view(URL, run)
    assert len(calls) == MAX_PAGES + 1
    assert (pull is None) is more
    assert more or pull.resolved is True


TESTS_SUITE = {"workflowRun": {"workflow": {"name": "Tests"}}}
DOCS_SUITE = {"workflowRun": {"workflow": {"name": "Docs"}}}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "older, newer, red",
    [("FAILURE", "SUCCESS", False), ("SUCCESS", "FAILURE", True), ("TIMED_OUT", "SUCCESS", False)],
)
def test_the_probe_judges_only_the_newest_run_of_each_check(reverse, older, newer, red):
    rollup = [
        {"name": "unit", "conclusion": older, "databaseId": 11, "checkSuite": TESTS_SUITE},
        {"name": "unit", "conclusion": newer, "databaseId": 12, "checkSuite": TESTS_SUITE},
        {"name": "unit", "conclusion": "SUCCESS", "databaseId": 10, "checkSuite": DOCS_SUITE},
    ]
    pull = probe(rollup[::-1] if reverse else rollup, [APP_SUITE])
    assert pull.resolved is True
    assert pull.red is red
    assert pull.failed == (("unit",) if red else ())


def test_the_probe_keeps_the_same_check_name_of_another_workflow():
    rollup = [
        {"name": "unit", "conclusion": "SUCCESS", "databaseId": 12, "checkSuite": TESTS_SUITE},
        {"name": "unit", "conclusion": "FAILURE", "databaseId": 11, "checkSuite": DOCS_SUITE},
    ]
    assert probe(rollup, [APP_SUITE]).failed == ("unit",)


@pytest.mark.parametrize(
    "rollup, red",
    [
        (
            [
                {"context": "ci/ext", "state": "SUCCESS", "createdAt": "2026-10-07T18:00:00Z"},
                {"context": "ci/ext", "state": "FAILURE", "createdAt": "2026-10-07T17:00:00Z"},
            ],
            False,
        ),
        (
            [
                {"context": "ci/ext", "state": "FAILURE", "createdAt": "2026-10-07T18:00:00Z"},
                {"context": "ci/ext", "state": "SUCCESS", "createdAt": "2026-10-07T17:00:00Z"},
            ],
            True,
        ),
        (
            [
                {"context": "ci/ext", "state": "SUCCESS", "createdAt": "2026-10-07T17:00:00Z"},
                {"context": "ci/ext", "state": "FAILURE"},
            ],
            False,
        ),
        (
            [{"name": "unit", "conclusion": "SUCCESS", "databaseId": 1}, {"name": "unit", "conclusion": "FAILURE"}],
            False,
        ),
    ],
)
def test_the_newest_status_and_check_win_over_rollup_position(rollup, red):
    pull = probe(rollup, [APP_SUITE])
    assert pull.resolved is True
    assert pull.red is red


def test_the_probe_keeps_the_rollup_order_of_newest_failures():
    rollup = [
        {"name": "b", "conclusion": "FAILURE", "databaseId": 5},
        {"name": "a", "conclusion": "FAILURE", "databaseId": 2},
        {"name": "c", "conclusion": "SUCCESS", "databaseId": 1},
        {"name": "a", "conclusion": "FAILURE", "databaseId": 3},
    ]
    assert probe(rollup, [APP_SUITE]).failed == ("b", "a")


@pytest.mark.parametrize("later, resolved", [(QUEUED_TESTS, False), (APP_SUITE, True)])
def test_the_probe_reads_a_running_suite_from_a_later_page(later, resolved):
    passed = [{"name": "unit", "conclusion": "SUCCESS", "databaseId": 1}]
    run, _ = paged(
        big_head(
            {"nodes": passed, "pageInfo": {"hasNextPage": False}},
            {"nodes": [APP_SUITE], "pageInfo": {"hasNextPage": True, "endCursor": "s1"}},
        ),
        {"data": {"node": {"checkSuites": {"nodes": [later], "pageInfo": {"hasNextPage": False}}}}},
    )
    assert github_view(URL, run).resolved is resolved


@pytest.mark.parametrize(
    "older, newer, told",
    [
        ("FAILURE", None, []),
        ("FAILURE", "SUCCESS", [f"ended the wait of {ME}: checks on {URL}, now green"]),
        ("SUCCESS", "FAILURE", [f"ended the wait of {ME}: checks on {URL}, now red; failed: unit"]),
    ],
)
def test_a_superseded_failure_never_ends_a_checks_wait(tick, older, newer, told):
    rollup = [
        {"name": "unit", "conclusion": older, "databaseId": 1, "checkSuite": TESTS_SUITE},
        {"name": "lint", "conclusion": "SUCCESS", "databaseId": 2, "checkSuite": TESTS_SUITE},
        {"name": "unit", "conclusion": newer, "databaseId": 3, "checkSuite": TESTS_SUITE},
    ]
    tick.hold("checks", URL)
    tick.pulls[URL] = probe(rollup, [APP_SUITE])
    assert tick.end() == []
    assert tick.end() == told
    assert (idle.wait(tick.store.redis, "sw", ME) is None) is bool(told)


def test_a_checks_wait_stays_held_while_the_new_heads_run_is_queued(tick):
    tick.hold("checks", URL)
    tick.pulls[URL] = probe(SKIPPED_ONLY, [QUEUED_TESTS])
    assert tick.end() == []
    assert tick.end() == []
    assert tick.told() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["head"] == "second"


GATE_WORKFLOW = "jobs:\n  gate-required:\n    name: Gate — Required\n    needs: [unit]\n"
UNIT_PASSED = SKIPPED_ONLY + [{"name": "unit", "conclusion": "SUCCESS"}]


def workflows(*texts):
    return {"object": {"entries": [{"name": f"w{n}.yml", "object": {"text": text}} for n, text in enumerate(texts)]}}


def gated_probe(rollup, suites, tree):
    from types import SimpleNamespace

    entries = ((tree or {}).get("object") or {}).get("entries") or []
    path = f"/o/r/actions/workflows/{entries[-1]['name']}" if entries else ""
    run = {"workflow": {"resourcePath": path}}
    rollup = [
        {**check, "checkSuite": {"workflowRun": run}} if "name" in check and not check.get("checkSuite") else check
        for check in rollup
    ]
    commit = {
        "committedDate": "2026-10-07T17:00:00Z",
        "statusCheckRollup": {"contexts": {"nodes": rollup, "pageInfo": {"hasNextPage": False}}},
        "checkSuites": {"nodes": suites, "pageInfo": {"hasNextPage": False}},
        "file": tree,
    }
    raw = {"data": {"resource": {"state": "OPEN", "headRefOid": "second", "commits": {"nodes": [{"commit": commit}]}}}}
    return github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(raw)))


@pytest.mark.parametrize(
    "text",
    [
        GATE_WORKFLOW,
        "jobs:\n  gate:\n    name: 'Gate — Required'\n",
        'jobs:\n  gate:\n    name: "Gate — Required"  \n',
        "jobs:\n  gate:\n\tname:\tGate — Required\t\n\n",
    ],
)
def test_the_probe_reads_a_declared_gate_from_the_head_workflows(text):
    assert gated_probe(UNIT_PASSED, [], workflows("name: Docs\n", text)).unpassed_gate == "Gate — Required"


@pytest.mark.parametrize(
    "tree",
    [
        None,
        {"object": None},
        {"object": {"entries": None}},
        workflows("name: Docs\n", None),
        workflows("jobs:\n  gate:\n    name: Gate — Required later\n"),
        workflows("jobs:\n  gate:\n    # name: Gate — Required\n"),
        workflows("jobs:\n  gate:\n    name: 'Gate — Required\"\n"),
        {"object": {"entries": [{"name": "x.yml", "object": None}]}},
        workflows("name: Gate — Required\njobs:\n  unit:\n    runs-on: ubuntu-latest\n"),
        workflows("on: push\n\nname: Gate — Required\n"),
        workflows("jobs:\n  gate:\n    name:\n      Gate — Required\n"),
    ],
)
def test_the_probe_without_a_declared_gate_resolves_on_every_check(tree):
    pull = gated_probe(UNIT_PASSED, [], tree)
    assert pull.resolved is True
    assert pull.red is False


def test_the_probe_reads_the_gate_from_the_last_commit():
    from types import SimpleNamespace

    def commit(tree):
        checks = [
            {
                **check,
                "checkSuite": {"workflowRun": {"workflow": {"resourcePath": "/o/r/actions/workflows/w0.yml"}}},
            }
            for check in UNIT_PASSED
        ]
        return {
            "commit": {
                "committedDate": "2026-10-07T17:00:00Z",
                "statusCheckRollup": {"contexts": {"nodes": checks, "pageInfo": {"hasNextPage": False}}},
                "checkSuites": NO_SUITES,
                "file": tree,
            }
        }

    nodes = [commit(None), commit(workflows(GATE_WORKFLOW))]
    raw = {"data": {"resource": {"state": "OPEN", "headRefOid": "second", "commits": {"nodes": nodes}}}}
    pull = github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(raw)))
    assert pull.unpassed_gate == "Gate — Required"


@pytest.mark.parametrize(
    ("rollup", "outcome"),
    [
        (UNIT_PASSED, []),
        (UNIT_PASSED + [{"name": "Gate — Required", "conclusion": "SUCCESS"}], [f"checks on {URL}, now green"]),
        (
            UNIT_PASSED + [{"name": "Gate — Required", "conclusion": "FAILURE"}],
            [f"checks on {URL}, now red; failed: Gate — Required"],
        ),
    ],
)
def test_a_gated_checks_wait_ends_only_on_its_gate(tick, rollup, outcome):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(rollup, [], workflows(GATE_WORKFLOW))
    assert tick.end() == []
    assert tick.end() == [f"ended the wait of {ME}: {line}" for line in outcome]
    assert (idle.wait(tick.store.redis, "sw", ME) is None) is bool(outcome)


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("conclusion", [None, "FAILURE", "SUCCESS"])
@pytest.mark.parametrize("event", [None, "pull_request"])
def test_checks_wait_judges_every_job_of_the_current_required_run(tick, required, conclusion, event):
    tick.hold("checks", URL)
    gate = {**_run_check("Gate — Required", 20, 2, "SUCCESS", event=event), "isRequired": True}
    job = {**_run_check("selected tests", 21, 2, conclusion, event=event), "isRequired": required}
    tick.pulls[URL] = _gated([gate, job], [_suite(2, "COMPLETED", event=event)], False)
    if conclusion is None:
        assert tick.end() == []
        assert idle.wait(tick.store.redis, "sw", ME) is not None
    else:
        outcome = "red; failed: selected tests" if conclusion == "FAILURE" else "green"
        assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now {outcome}"]
        assert idle.wait(tick.store.redis, "sw", ME) is None


@pytest.mark.parametrize("status", ["IN_PROGRESS", "COMPLETED"])
@pytest.mark.parametrize("conclusion", [None, "FAILURE", "SUCCESS"])
@pytest.mark.parametrize("event", [None, "pull_request"])
@pytest.mark.parametrize("other_required", [False, True])
def test_a_status_gate_cannot_hide_the_current_workflow_jobs(tick, status, conclusion, event, other_required):
    tick.hold("checks", URL)
    gate = {"context": "Gate — Required", "state": "SUCCESS", "isRequired": True}
    job = {**_run_check("selected tests", 20, 2, conclusion, event=event), "isRequired": False}
    checks = [gate, job]
    if other_required:
        checks.append({**_run_check("security", 10, 1, "SUCCESS", workflow="W2"), "isRequired": True})
    tick.pulls[URL] = _gated(checks, [_suite(2, status, event=event)], False, workflows=["W1.yml"])
    if conclusion == "FAILURE":
        assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now red; failed: selected tests"]
    elif conclusion == "SUCCESS" and status == "COMPLETED":
        assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]
    else:
        assert tick.end() == []
        assert idle.wait(tick.store.redis, "sw", ME) is not None


@pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS", "COMPLETED"])
@pytest.mark.parametrize("reverse", [False, True])
def test_a_status_gate_holds_a_superseding_run_without_current_jobs(tick, status, reverse):
    tick.hold("checks", URL)
    gate = {"context": "Gate — Required", "state": "SUCCESS", "isRequired": True}
    old = {**_run_check("selected tests", 10, 1, "SUCCESS"), "isRequired": False}
    suites = [_suite(1, "COMPLETED", "SUCCESS"), _suite(2, status)]
    tick.pulls[URL] = _gated([gate, old], suites, reverse, workflows=["W1.yml"])
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) is not None
    current = {**_run_check("selected tests", 20, 2, "SUCCESS"), "isRequired": False}
    suites[-1] = _suite(2, "COMPLETED", "SUCCESS")
    tick.pulls[URL] = _gated([gate, old, current], suites, reverse, workflows=["W1.yml"])
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]


@pytest.mark.parametrize("conclusion", [None, "FAILURE"])
def test_a_status_gate_ignores_workflows_without_a_declared_gate(tick, conclusion):
    tick.hold("checks", URL)
    gate = {"context": "Gate — Required", "state": "SUCCESS", "isRequired": True}
    unit = {**_run_check("unit", 10, 1, "SUCCESS"), "isRequired": False}
    optional = {**_run_check("measure", 20, 2, conclusion, workflow="W2"), "isRequired": False}
    suites = [_suite(1, "COMPLETED", "SUCCESS"), _suite(2, "IN_PROGRESS", workflow="W2")]
    tick.pulls[URL] = _gated([gate, unit, optional], suites, False, workflows=["W1.yml"])
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]


@pytest.mark.parametrize("conclusion", ["SKIPPED", "NEUTRAL"])
def test_a_status_success_cannot_replace_a_gate_check_that_did_not_pass(tick, conclusion):
    tick.hold("checks", URL)
    status = {"context": "Gate — Required", "state": "SUCCESS", "isRequired": True}
    gate = {**_run_check("Gate — Required", 10, 1, conclusion), "isRequired": True}
    tick.pulls[URL] = _gated([status, gate], [_suite(1, "COMPLETED")], False)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) is not None


@pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS"])
@pytest.mark.parametrize("reverse", [False, True])
def test_checks_wait_holds_the_new_required_run_after_an_old_green_gate(tick, status, reverse):
    tick.hold("checks", URL)
    checks = [_run_check("Gate — Required", 10, 1, "SUCCESS")]
    suites = [_suite(1, "COMPLETED", "SUCCESS"), _suite(2, status)]
    tick.pulls[URL] = _gated(checks, suites, reverse)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) is not None
    checks += [_run_check("Gate — Required", 20, 2, "SUCCESS"), _run_check("lint", 21, 2, None)]
    tick.pulls[URL] = _gated(checks, suites, reverse)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) is not None


@pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS", "COMPLETED"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("sibling", [False, True])
@pytest.mark.parametrize("name", ["security", "Gate — Required"])
def test_checks_wait_retains_a_separate_required_workflow_after_superseding_its_check(
    tick, status, reverse, sibling, name
):
    tick.hold("checks", URL)
    gate = {**_run_check("Gate — Required", 10, 1, "SUCCESS"), "isRequired": True}
    old = {**_run_check(name, 20, 2, "SUCCESS", workflow="W2"), "isRequired": name == "security"}
    suites = [_suite(1, "COMPLETED", "SUCCESS"), _suite(3, status, workflow="W2")]
    checks = [gate, old]
    if sibling:
        checks.append({**_run_check("measure", 29, 3, "SUCCESS", workflow="W2"), "isRequired": False})
    tick.pulls[URL] = _gated(checks, suites, reverse)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) is not None
    current = {**_run_check(name, 30, 3, "SUCCESS", workflow="W2"), "isRequired": name == "security"}
    suites[-1] = _suite(3, "COMPLETED", "SUCCESS", workflow="W2")
    tick.pulls[URL] = _gated([*checks, current], suites, reverse)
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]
    assert idle.wait(tick.store.redis, "sw", ME) is None


@pytest.mark.parametrize("conclusion", [None, "FAILURE"])
@pytest.mark.parametrize("event", [None, "pull_request"])
def test_an_optional_workflow_cannot_hold_or_fail_the_required_run(tick, conclusion, event):
    tick.hold("checks", URL)
    gate = {**_run_check("Gate — Required", 10, 1, "SUCCESS", event=event), "isRequired": True}
    optional = {**_run_check("measure", 20, 2, conclusion, workflow="W2"), "isRequired": False}
    suites = [_suite(1, "COMPLETED", "SUCCESS", event=event), _suite(2, "IN_PROGRESS", workflow="W2")]
    tick.pulls[URL] = _gated([gate, optional], suites, False)
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]
    assert idle.wait(tick.store.redis, "sw", ME) is None


@pytest.mark.parametrize("conclusion", ["SUCCESS", "FAILURE", "TIMED_OUT"])
def test_a_required_gate_holds_green_or_ends_red_beside_a_pending_jobless_workflow(tick, conclusion):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(
        [{"name": "Gate — Required", "conclusion": conclusion}],
        [QUEUED_TESTS],
        workflows(GATE_WORKFLOW),
    )
    assert tick.end() == []
    if conclusion == "SUCCESS":
        assert tick.end() == []
        assert idle.wait(tick.store.redis, "sw", ME) is not None
        assert tick.told() == []
    else:
        outcome = "red; failed: Gate — Required"
        assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now {outcome}"]
        assert idle.wait(tick.store.redis, "sw", ME) is None
        assert tick.told() == [
            f"Your wait on checks on {URL}, now {outcome} has ended. Pick task t1 back up: "
            "agentihooks swarm sw done, block, or wait on the next thing."
        ]


@pytest.mark.parametrize("conclusion", ["FAILURE", "ERROR", "STARTUP_FAILURE", "ACTION_REQUIRED", "TIMED_OUT"])
@pytest.mark.parametrize("gate", [None, "SUCCESS"])
def test_a_failed_required_check_ends_wait_beside_a_pending_jobless_workflow(tick, conclusion, gate):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(
        [{"name": "unit", "conclusion": conclusion}, {"name": "Gate — Required", "conclusion": gate}],
        [QUEUED_TESTS],
        workflows(GATE_WORKFLOW),
    )
    assert tick.end() == []
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now red; failed: unit"]
    assert idle.wait(tick.store.redis, "sw", ME) is None


@pytest.mark.parametrize("conclusion", ["FAILURE", "ERROR", "STARTUP_FAILURE", "ACTION_REQUIRED", "TIMED_OUT"])
def test_the_first_failed_job_ends_the_wait_and_names_it_while_siblings_run(tick, conclusion, capsys):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(
        [
            {"name": "stats (1)", "conclusion": conclusion},
            {"name": "stats (2)", "status": "IN_PROGRESS"},
            {"name": "Gate — Required", "status": "IN_PROGRESS"},
        ],
        [QUEUED_TESTS],
        workflows(GATE_WORKFLOW),
    )
    assert tick.end() == []
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now red; failed: stats (1)"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert "now red; failed: stats (1) has ended" in tick.told()[0]
    assert tick.end() == []
    assert len(tick.told()) == 1
    with capsys.disabled():
        print(
            f"Scratch wait ended on {conclusion}: stats (1) failed; stats (2) and Gate Required still running; owner notified once."
        )


def test_the_failure_notice_names_only_jobs_from_the_confirming_read(tick):
    tick.hold("checks", URL)
    replies = iter(
        [
            PullRequest("OPEN", None, 1, True, True, ("stats (1)",), head="first"),
            PullRequest("OPEN", None, 1, True, True, ("stats (2)",), head="first"),
        ]
    )
    assert waits.end_pass(tick.store, "sw", {}, tick.inbox, lambda url: next(replies), 5_000, None) == [
        f"ended the wait of {ME}: checks on {URL}, now red; failed: stats (2)"
    ]
    assert "failed: stats (2) has ended" in tick.told()[0]
    assert "stats (1)" not in tick.told()[0]


@pytest.mark.parametrize(
    "done",
    [
        (1, ""),
        (0, "{}"),
        (0, '{"data":{"resource":null}}'),
        (0, '{"data":{"resource":{"commits":null}}}'),
        (0, '{"data":{"resource":{"commits":{"nodes":null}}}}'),
        (0, "invalid json"),
        (
            0,
            '{"data":{"resource":{"state":"OPEN","headRefOid":"first","commits":{"nodes":[{"commit":{"committedDate":"2026-10-07T17:00:00Z","statusCheckRollup":{"contexts":{"nodes":[{"conclusion":"SUCCESS"}],"pageInfo":{"hasNextPage":true}}},"checkSuites":{"nodes":[],"pageInfo":{"hasNextPage":false}}}}]}}}}',
        ),
        (
            0,
            '{"data":{"resource":{"state":"OPEN","headRefOid":"first","commits":{"nodes":[{"commit":{"committedDate":"2026-10-07T17:00:00Z","statusCheckRollup":{"contexts":{"nodes":[{"conclusion":"SKIPPED"}],"pageInfo":{"hasNextPage":false}}}}}]}}}}',
        ),
    ],
)
def test_the_probe_refuses_failed_or_incomplete_reads(done):
    from types import SimpleNamespace

    assert github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=done[0], stdout=done[1])) is None


@pytest.mark.parametrize("head", ["", None])
def test_a_tick_without_a_head_does_not_resolve_or_reset_the_wait(tick, head):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    before = idle.wait(tick.store.redis, "sw", ME)
    tick.pulls[URL] = SimpleNamespace(state="OPEN", head=head, resolved=True, red=False, unpassed_gate="")
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) == before
    assert tick.told() == []


def test_a_legacy_checks_wait_binds_before_resolving(tick, monkeypatch):
    from types import SimpleNamespace

    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "tests", 1, on={"kind": "checks", "target": URL})
    tick.pulls[URL] = SimpleNamespace(state="OPEN", head="first", resolved=True, red=False, unpassed_gate="")
    key = idle.key("sw", "wait", ME)
    ttl = tick.store.redis.pttl(key)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) == {
        "until": 10_000_000,
        "reason": "tests",
        "at": 1,
        "on": {"kind": "checks", "target": URL, "head": "first"},
    }
    assert 0 < tick.store.redis.pttl(key) <= ttl
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]


def test_checks_resolution_uses_the_confirmed_current_head_result(tick):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    replies = iter(
        [
            SimpleNamespace(state="OPEN", head="first", resolved=True, red=False, unpassed_gate=""),
            SimpleNamespace(state="OPEN", head="first", resolved=True, red=True, unpassed_gate="", failed=()),
        ]
    )
    assert waits.end_pass(tick.store, "sw", {}, tick.inbox, lambda url: next(replies), 5_000, None) == [
        f"ended the wait of {ME}: checks on {URL}, now red"
    ]


@pytest.mark.parametrize("head, resolved", [("second", True), ("first", True)])
def test_the_tick_preserves_a_wait_redeclared_during_its_probe(tick, head, resolved):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    calls = []

    def github(url):
        calls.append(url)
        idle.declare_wait(
            tick.store.redis, "sw", ME, 20_000_000, "new wait", 2, on={"kind": "checks", "target": URL, "head": "third"}
        )
        return SimpleNamespace(state="OPEN", head=head, resolved=resolved, red=False, unpassed_gate="")

    assert waits.end_pass(tick.store, "sw", {}, tick.inbox, github, 5_000, None) == []
    assert idle.wait(tick.store.redis, "sw", ME) == {
        "until": 20_000_000,
        "reason": "new wait",
        "at": 2,
        "on": {"kind": "checks", "target": URL, "head": "third"},
    }
    assert tick.told() == []


def test_a_pending_current_head_does_not_rewrite_the_wait(tick, monkeypatch):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    tick.pulls[URL] = SimpleNamespace(state="OPEN", head="first", resolved=False, red=False, unpassed_gate="")

    def unexpected_write(*args, **kwargs):
        pytest.fail("an unresolved wait with the same head needs no Redis write")

    monkeypatch.setattr(tick.store.redis, "transaction", unexpected_write)
    assert tick.end() == []


def test_a_replaced_wait_does_not_stop_resolution_for_the_next_agent(tick):
    from types import SimpleNamespace

    following = "engineer@a1b2c3-0002"
    tick.store.put_agent("sw", AgentRecord(name=following, lane="eng", task="t2", seat="eng-2@sw"))
    tick.hold("checks", URL)
    idle.declare_wait(tick.store.redis, "sw", following, 10_000_000, "", 1, on={"kind": "task", "target": "t3"})

    def github(url):
        idle.declare_wait(
            tick.store.redis, "sw", ME, 20_000_000, "", 2, on={"kind": "checks", "target": URL, "head": "third"}
        )
        return SimpleNamespace(state="OPEN", head="second", resolved=True, red=False, unpassed_gate="")

    assert waits.end_pass(tick.store, "sw", {"t3": {"state": "done"}}, tick.inbox, github, 5_000, None) == [
        f"ended the wait of {following}: task t3, now done"
    ]
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["head"] == "third"
    assert idle.wait(tick.store.redis, "sw", following) is None


@pytest.mark.parametrize("rollup", [[], UNIT_PASSED, [{"name": "Gate — Required", "conclusion": "SKIPPED"}]])
def test_a_missing_or_skipped_required_gate_keeps_the_checks_wait_active(tick, rollup):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(rollup, [], workflows(GATE_WORKFLOW))
    assert tick.pulls[URL].failed == ()
    assert tick.end() == []
    held = idle.wait(tick.store.redis, "sw", ME)
    assert held["on"] == {"kind": "checks", "target": URL, "head": "second"}
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) == held
    assert tick.told() == []
    assert idle.verdict("idle", None, held, 9_999_999) == "waiting"
    assert idle.verdict("idle", None, held, 10_000_000) == "idle"


@pytest.mark.parametrize(
    "rollup",
    [
        UNIT_PASSED + [{"name": "Gate — Required", "status": "QUEUED"}],
        UNIT_PASSED + [{"name": "Gate — Required", "status": "IN_PROGRESS"}],
    ],
)
def test_a_pending_required_gate_keeps_the_checks_wait_active(tick, rollup):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(rollup, [], workflows(GATE_WORKFLOW))
    assert tick.end() == []
    held = idle.wait(tick.store.redis, "sw", ME)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) == held
    assert tick.told() == []


def test_a_missing_gate_on_the_confirming_read_keeps_the_wait(tick):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(UNIT_PASSED, [], workflows(GATE_WORKFLOW))
    assert tick.end() == []
    held = idle.wait(tick.store.redis, "sw", ME)
    passed = gated_probe(
        UNIT_PASSED + [{"name": "Gate — Required", "conclusion": "SUCCESS"}], [], workflows(GATE_WORKFLOW)
    )
    replies = iter([passed, tick.pulls[URL]])
    assert waits.end_pass(tick.store, "sw", {}, tick.inbox, lambda url: next(replies), 5_000, None) == []
    assert idle.wait(tick.store.redis, "sw", ME) == held
    assert tick.told() == []


def test_a_pending_first_read_keeps_waiting_before_a_successful_confirmation(tick):
    tick.hold("checks", URL)
    pending = gated_probe(
        UNIT_PASSED + [{"name": "Gate — Required", "status": "IN_PROGRESS"}], [], workflows(GATE_WORKFLOW)
    )
    tick.pulls[URL] = pending
    assert tick.end() == []
    held = idle.wait(tick.store.redis, "sw", ME)
    passed = gated_probe(
        UNIT_PASSED + [{"name": "Gate — Required", "conclusion": "SUCCESS"}], [], workflows(GATE_WORKFLOW)
    )
    replies = iter([pending, passed])
    assert waits.end_pass(tick.store, "sw", {}, tick.inbox, lambda url: next(replies), 5_000, None) == []
    assert idle.wait(tick.store.redis, "sw", ME) == held
    assert tick.told() == []


def test_a_concluded_failure_ends_the_wait_with_a_missing_gate(tick):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe([{"name": "Unit tests", "conclusion": "FAILURE"}], [], workflows(GATE_WORKFLOW))
    assert tick.end() == []
    assert tick.end() == [
        f"ended the wait of {ME}: checks on {URL}, now red; failed: Unit tests; Gate — Required never passed"
    ]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert len(tick.told()) == 1


@pytest.mark.parametrize("rollup", [[], UNIT_PASSED, [{"name": "Gate — Required", "conclusion": "SKIPPED"}]])
@pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS"])
def test_a_dead_gate_keeps_waiting_while_its_workflow_run_is_active(tick, rollup, status):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(
        rollup, [{"status": status, "workflowRun": {"databaseId": 1}}], workflows(GATE_WORKFLOW)
    )
    assert tick.end() == []
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "second"}
    assert tick.told() == []


OTHER = "https://github.com/o/r/pull/8"


def test_a_wait_on_intent_names_its_own_tasks_pull_request(started, capsys):
    store, ledger = started
    ledger.rows["t1"]["pr_url"] = URL
    assert run("sw", "--as", ME, "wait", "--on", "intent", URL) == 0
    assert held(store)["on"] == {"kind": "intent", "target": URL, "task": "t1"}
    assert held(store)["until"] == 1_000 + waits.CHECKED_MINUTES * 60_000
    capsys.readouterr()
    assert run("sw", "--as", ME, "wait", "--on", "intent", OTHER) == 1
    assert f"wait on intent needs the pull request of your task t1, not {OTHER}" in capsys.readouterr().err


def test_a_checked_wait_reads_only_the_task_it_names(started):
    _, ledger = started
    read = []

    def whole(slug):
        raise AssertionError("a checked wait read the whole task collection")

    ledger.tasks = whole
    ledger.task = lambda slug, task_id: read.append((slug, task_id)) or ledger.rows.get(task_id)
    ledger.rows["t1"]["pr_url"] = URL
    assert run("sw", "--as", ME, "wait", "--on", "task", "t2") == 0
    assert run("sw", "--as", ME, "wait", "--on", "intent", URL) == 0
    assert run("sw", "--as", ME, "wait", "--on", "checks", URL) == 0
    assert run("sw", "--as", ME, "wait", "--on", "task", "t9") == 1
    assert read == [("sw", "t2"), ("sw", "t1"), ("sw", "t9")]


def test_target_problem_takes_intent_only_on_the_own_tasks_pull_request():
    rows = {"t1": {"id": "t1", "pr_url": URL}, "t2": {"id": "t2", "pr_url": OTHER}}
    assert waits.target_problem("intent", URL, "t1", rows, None) == ""
    refused = f"wait on intent needs the pull request of your task t1, not {OTHER}"
    assert waits.target_problem("intent", OTHER, "t1", rows, None) == refused
    assert waits.target_problem("intent", OTHER, "t1", {}, None) == refused


def test_only_an_intent_wait_carries_its_task_and_none_is_empty_by_default():
    assert waits.on("intent", URL) == {"kind": "intent", "target": URL, "task": ""}
    assert waits.on("intent", URL, task="t1") == {"kind": "intent", "target": URL, "task": "t1"}
    assert waits.on("checks", URL, task="t1") == {"kind": "checks", "target": URL}


def hold_intent(tick):
    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "", 1, on={"kind": "intent", "target": URL, "task": "t1"})


def intent_record(verdict, reason, **fields):
    Verdicts("sw", "intent").write("t1", verdict, reason, 4_000, **fields)


@pytest.mark.parametrize("reason", [None, intent.RUNNING, intent.DRAFT])
def test_an_intent_wait_holds_while_no_verdict_or_a_pending_one_stands(tick, reason):
    hold_intent(tick)
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, head="first")
    if reason:
        intent_record("pending", reason)
    assert tick.end() == []
    assert held(tick.store)["on"]["kind"] == "intent"
    assert tick.told() == []


@pytest.mark.parametrize("verdict", ["pass", "fail", "unchecked"])
def test_the_tick_ends_an_intent_wait_on_its_verdict_and_tells_the_reason(tick, verdict):
    hold_intent(tick)
    intent_record(verdict, "the phase can use it")
    outcome = f"the intent verdict on {URL}, now {verdict}: the phase can use it"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert held(tick.store) is None
    assert tick.told() == [
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    ]
    assert waits.notice_task(tick.inbox.inbox("eng-1@sw")[0]) == "t1"


def test_an_intent_wait_holds_on_a_verdict_judged_on_another_head(tick):
    hold_intent(tick)
    intent_record("pass", "fine", head="old", url=URL)
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, head="new")
    assert tick.end() == []
    tick.pulls[URL] = None
    assert tick.end() == []
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, head="old")
    assert tick.end() == [f"ended the wait of {ME}: the intent verdict on {URL}, now pass: fine"]


@pytest.mark.parametrize(
    "queued",
    [
        {"name": "unit"},
        {"context": "unit", "state": "PENDING"},
        {"name": "unit", "state": "EXPECTED"},
        {"name": "unit", "conclusion": "CANCELLED"},
    ],
)
def test_a_queued_required_check_holds_the_wait_after_the_gate_passed(tick, queued):
    from scripts.swarm.ledger_events import GATE, pull_request

    tick.hold("checks", URL)
    gate = {"name": GATE, "conclusion": "SUCCESS", "isRequired": True}
    raw = {"state": "OPEN", "headRefOid": "first", "gated": True}
    tick.pulls[URL] = pull_request({**raw, "statusCheckRollup": [gate, {**queued, "isRequired": True}]})
    assert tick.end() == []
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "first"}
    assert tick.told() == []
    done = {**queued, "isRequired": True, "conclusion": "SUCCESS"}
    tick.pulls[URL] = pull_request({**raw, "statusCheckRollup": [gate, done]})
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]


def test_a_result_read_from_an_older_commit_never_ends_the_wait(tick):
    from scripts.swarm.ledger_events import GATE, pull_request

    tick.hold("checks", URL)
    raw = {
        "state": "OPEN",
        "headRefOid": "first",
        "gated": True,
        "commits": [{"oid": "older", "committedDate": "2026-10-17T10:00:00Z"}],
        "statusCheckRollup": [{"name": GATE, "conclusion": "SUCCESS"}],
    }
    tick.pulls[URL] = pull_request(raw)
    assert tick.pulls[URL].head == ""
    assert tick.end() == []
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "first"}
    tick.pulls[URL] = pull_request({**raw, "commits": [{"oid": "first", "committedDate": "2026-10-17T10:00:00Z"}]})
    assert tick.pulls[URL].head == "first"
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]


def test_a_cached_green_result_ends_the_wait_only_once_a_fresh_read_confirms_it(tick):
    tick.hold("checks", URL)
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, True, head="first")
    tick.fresh[URL] = PullRequest("OPEN", None, 1, False, False, head="first")
    assert tick.end() == []
    tick.fresh[URL] = PullRequest("OPEN", None, 1, False, True, head="second")
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "second"}
    tick.pulls[URL] = tick.fresh[URL]
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]


def test_a_neutral_required_check_beside_a_passed_gate_ends_the_wait_green(tick):
    from scripts.swarm.ledger_events import GATE, pull_request

    tick.hold("checks", URL)
    checks = [{"name": GATE, "conclusion": "SUCCESS"}, {"name": "unit", "conclusion": "NEUTRAL", "isRequired": True}]
    tick.pulls[URL] = pull_request({"state": "OPEN", "headRefOid": "first", "gated": True, "statusCheckRollup": checks})
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]


def test_a_green_wait_holds_until_the_branch_rules_confirm_every_required_check(tick, monkeypatch):
    seen, answer = [], {"green": False}

    def required(target, head):
        seen.append((target, head))
        return answer["green"]

    monkeypatch.setattr(waits, "required_green", required)
    tick.hold("checks", URL)
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, True, head="first")
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "first"}
    answer["green"] = True
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]
    assert seen == [(URL, "first"), (URL, "first")]


def test_a_red_wait_ends_without_asking_the_branch_rules(tick, monkeypatch):
    def required(target, head):
        pytest.fail("a red result needs no required check confirmation")

    monkeypatch.setattr(waits, "required_green", required)
    tick.hold("checks", URL)
    tick.pulls[URL] = PullRequest("OPEN", None, 1, True, True, head="first")
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now red"]


@pytest.mark.parametrize(
    "pull, green",
    [
        (None, False),
        (PullRequest("OPEN", None, None, False, True, head="first"), True),
        (PullRequest("OPEN", None, None, False, True, head="second"), False),
        (PullRequest("OPEN", None, None, False, False, head="first"), False),
        (PullRequest("OPEN", None, None, True, True, head="first"), False),
    ],
)
def test_required_green_needs_every_required_check_passed_on_the_head(monkeypatch, pull, green):
    import subprocess

    calls = []

    def view(url, run):
        calls.append((url, run))
        return pull

    monkeypatch.setattr(merge_queue, "required_view", view)
    assert REAL_REQUIRED_GREEN(URL, "first") is green
    assert calls == [(URL, subprocess.run)]


@pytest.mark.parametrize("error", [SwarmError("GitHub API failed"), KeyError("repository"), TypeError("none")])
def test_required_green_holds_when_github_cannot_answer(monkeypatch, error):
    def view(url, run):
        raise error

    monkeypatch.setattr(merge_queue, "required_view", view)
    assert REAL_REQUIRED_GREEN(URL, "first") is False


def test_the_required_reading_of_an_older_commit_carries_no_head():
    from scripts.swarm.ledger_events import GATE, _required_pull_request

    raw = {
        "state": "OPEN",
        "headRefOid": "first",
        "commits": [{"oid": "older"}],
        "statusCheckRollup": [{"name": GATE, "conclusion": "SUCCESS", "isRequired": True}],
        "checkSuites": [],
    }
    assert _required_pull_request(raw, {GATE: None}).head == ""
    assert _required_pull_request({**raw, "commits": [{"oid": "first"}]}, {GATE: None}).head == "first"
    assert _required_pull_request({k: v for k, v in raw.items() if k != "commits"}, {GATE: None}).head == "first"
