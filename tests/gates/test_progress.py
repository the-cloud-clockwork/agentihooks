import pytest

from scripts.gates import progress
from scripts.swarm.keyspace import ROOT as KEY_ROOT

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG, ME = "demo", "engineer@abcdef-0001"


@pytest.fixture
def store():
    import fakeredis

    return progress.Progress(fakeredis.FakeRedis(decode_responses=True), SLUG)


def test_an_agent_with_no_record_reads_empty(store):
    assert store.read(ME) == progress.Mark(outcome_at=0, outcome="", talk=0)


def test_talk_counts_up_from_one(store):
    assert [store.talk(ME), store.talk(ME), store.talk(ME)] == [1, 2, 3]
    assert store.read(ME).talk == 3


def test_an_outcome_stamps_its_kind_and_zeroes_the_talk(store):
    store.talk(ME)
    store.talk(ME)
    store.outcome(ME, "pushed", now_ms=1234)
    assert store.read(ME) == progress.Mark(outcome_at=1234, outcome="pushed", talk=0)
    assert store.talk(ME) == 1


def test_agents_and_swarms_keep_separate_marks(store):
    store.talk(ME)
    assert store.read("engineer@abcdef-0002").talk == 0
    assert progress.Progress(store.redis, "elsewhere").read(ME).talk == 0
    assert store.key(ME) == f"{KEY_ROOT}:swarm:{SLUG}:progress:{ME}"


def test_outcome_once_stamps_a_resolution_only_the_first_time(store):
    assert store.outcome_once(ME, "checks resolved", "pr-1 pass", now_ms=10) is True
    store.talk(ME)
    assert store.outcome_once(ME, "checks resolved", "pr-1 pass", now_ms=20) is False
    assert store.read(ME) == progress.Mark(outcome_at=10, outcome="checks resolved", talk=1)
    assert store.outcome_once(ME, "checks resolved", "pr-1 fail", now_ms=30) is True
    assert store.read(ME) == progress.Mark(outcome_at=30, outcome="checks resolved", talk=0)


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push -u origin engineer-323133-0115",
        "cd /w && git push origin HEAD",
        "gh pr create --base dev --title x --body-file b.md",
        "git -C /w push",
    ],
)
def test_pushes_and_opened_pull_requests_are_outcomes(command):
    assert progress.outcome_of(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git commit -m push",
        "echo git push",
        "gh pr view 3",
        "gh pr checks 3",
        "grep 'gh pr create' notes.md",
        "",
    ],
)
def test_other_commands_are_not_outcomes(command):
    assert progress.outcome_of(command) == ""


URL = "https://github.com/o/r/pull/7"


def pull(resolved=True, red=False, pushed_at=100):
    from scripts.swarm.ledger_events import PullRequest

    return PullRequest("OPEN", None, pushed_at, red, resolved)


def pr_task(**extra):
    return {"id": "t1", "state": "pr", "pr_url": URL, "claimed_by": ME, **extra}


def test_resolved_checks_are_an_outcome_for_the_task_owner_once(store):
    found = pull()
    assert progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: found, now_ms=5) == [
        f"checks resolved green on {URL}, an outcome for {ME}"
    ]
    assert store.read(ME) == progress.Mark(outcome_at=5, outcome="checks resolved", talk=0)
    store.talk(ME)
    assert progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: found, now_ms=9) == []
    assert store.read(ME).talk == 1


def test_a_new_push_or_a_red_result_is_a_new_resolution(store):
    progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: pull(), now_ms=5)
    assert progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: pull(red=True), now_ms=6) == [
        f"checks resolved red on {URL}, an outcome for {ME}"
    ]
    assert progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: pull(red=True, pushed_at=200), now_ms=7)


@pytest.mark.parametrize(
    "task",
    [pr_task(state="claimed"), pr_task(pr_url=""), pr_task(claimed_by=""), {"id": "t1"}],
)
def test_only_held_pull_requests_with_an_owner_are_read(store, task):
    seen = []
    assert progress.checks_pass(store.redis, SLUG, [task], lambda url: seen.append(url) or pull()) == []
    assert seen == []


@pytest.mark.parametrize("found", [None, "pending"])
def test_unread_or_pending_checks_are_no_outcome(store, found):
    result = pull(resolved=False) if found else None
    assert progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: result) == []
    assert store.read(ME).outcome_at == 0


@pytest.mark.parametrize(
    ("rollup", "resolved", "red"),
    [
        ([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"state": "SUCCESS"}], True, False),
        ([{"status": "COMPLETED", "conclusion": "FAILURE"}], True, True),
        ([{"status": "IN_PROGRESS", "conclusion": ""}], False, False),
        ([{"status": "QUEUED", "conclusion": None}], False, False),
        ([{"state": "PENDING"}], False, False),
        ([{"state": "EXPECTED"}], False, False),
        ([{"conclusion": "SUCCESS"}, {"state": "PENDING"}], False, False),
        ([], False, False),
    ],
)
def test_a_pull_request_reads_resolved_when_every_check_finished(rollup, resolved, red):
    from scripts.swarm.ledger_events import pull_request

    found = pull_request({"state": "OPEN", "statusCheckRollup": rollup})
    assert (found.resolved, found.red) == (resolved, red)


def test_no_command_is_no_outcome():
    assert progress.outcome_of(None) == ""


def test_an_outcome_without_a_time_is_stamped_now(store):
    import time

    before = int(time.time() * 1000)
    store.outcome(ME, "pushed")
    assert before <= store.read(ME).outcome_at <= int(time.time() * 1000)


def test_every_held_pull_request_is_read_past_a_skipped_one(store):
    other = "engineer@abcdef-0002"
    tasks = [
        pr_task(id="t0", state="claimed"),
        pr_task(id="t1", pr_url=URL + "1", claimed_by=other),
        pr_task(id="t2", pr_url=URL + "2"),
        pr_task(id="t3", pr_url=URL + "3", claimed_by="engineer@abcdef-0003"),
    ]
    found = {URL + "1": None, URL + "2": pull(), URL + "3": pull()}
    assert progress.checks_pass(store.redis, SLUG, tasks, found.get, now_ms=5) == [
        f"checks resolved green on {URL}2, an outcome for {ME}",
        f"checks resolved green on {URL}3, an outcome for engineer@abcdef-0003",
    ]
    assert store.read(other).outcome_at == 0
