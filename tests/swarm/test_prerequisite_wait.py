import json

import pytest

from scripts.swarm import cli, idle, merge_queue, prerequisite_wait
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord
from tests.swarm.test_cli import env, run  # noqa: F401

ME = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/7"


@pytest.fixture
def waiting(env, monkeypatch):  # noqa: F811
    store, ledger, runtime = env
    assert run("sw", "create", "--repo", "/repo") == 0
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
    ledger.rows["t1"].update(state="claimed", claimed_by=ME)
    read_task = ledger.task

    def task(slug, task_id):
        assert task_id
        assert slug == "sw"
        return read_task(slug, task_id)

    monkeypatch.setattr(ledger, "task", task)

    def queued(url):
        assert url in {URL, "https://github.com/o/r/pull/8"}
        return PullRequest("OPEN", None, 1, False, queued=True)

    monkeypatch.setattr(cli.ledger_events, "view", queued)
    state = {"state": "OPEN", "queued": True, "entry": {"position": 15}}

    def read(action, url):
        assert (action, url) == ("state", URL)
        return state

    monkeypatch.setattr(merge_queue, "operate", read)
    return store, ledger, state


def test_a_long_prerequisite_wait_directs_stacking_or_blocking_without_idling(waiting, capsys):
    store, _, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    error = capsys.readouterr().err
    assert "stack" in error and "publish" in error and "block" in error
    assert URL in error and "15" in error
    assert error.strip() == (
        f"swarm: prerequisite {URL} is at queue position 15; stack the next change on the prerequisite branch "
        "and publish it, or block with this prerequisite URL, the stack and next step in the notes; "
        "the swarm will reopen after it merges. For the task's final deliverable, "
        f"confirm it with wait --on merge {URL} --final"
    )
    assert idle.wait(store.redis, "sw", ME) is None


@pytest.mark.parametrize("position", [1, 5, None])
def test_a_short_or_unknown_prerequisite_queue_preserves_merge_waits(waiting, position):
    store, _, state = waiting
    state["entry"]["position"] = position
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "merge", "target": URL}


def test_the_tasks_final_deliverable_can_wait_at_any_queue_position(waiting):
    store, ledger, _ = waiting
    ledger.rows["t1"]["pr_url"] = URL
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL, "--final") == 0
    assert idle.wait(store.redis, "sw", ME)["on"]["target"] == URL


def test_block_retains_the_stack_and_tick_reopens_only_after_prerequisite_merge(waiting, env, monkeypatch):  # noqa: F811
    store, ledger, _ = waiting
    note = f"Stack published on continuation branch; next step restack after prerequisite {URL} merges"
    ledger.rows["t1"].update(branch="continuation", pr_url="https://github.com/o/r/pull/8")
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", note) == 0
    assert ledger.rows["t1"]["state"] == "blocked"
    assert ledger.rows["t1"]["branch"] == "continuation"
    assert any(row[:2] == ("t1", note) for row in ledger.comments)
    assert store.agents("sw")[0].state == "finished"
    cli.tick("sw", store, ledger, env[2], 2_000)
    assert ledger.rows["t1"]["state"] == "blocked"
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: PullRequest("MERGED", 2, 1, False))
    cli.tick("sw", store, ledger, env[2], 3_000)
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert ledger.rows["t1"]["branch"] == "continuation"


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", None])
def test_blocked_continuation_does_not_reopen_without_a_merged_prerequisite(waiting, monkeypatch, state):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: PullRequest(state, None, 1, False) if state else None)
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows["t1"]["state"] == "blocked"


@pytest.mark.parametrize("change", [{"state": "done"}, {"claimed_by": "someone-else"}, {"claim_generation": 1}])
def test_prerequisite_merge_never_reopens_a_changed_task(waiting, monkeypatch, change):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    ledger.rows["t1"].update(change)
    expected = dict(ledger.rows["t1"])
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("changed task must not read GitHub"))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows["t1"] == expected


def test_block_without_a_refused_prerequisite_keeps_its_existing_behavior(waiting, monkeypatch):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "block", "Waiting for operator credentials") == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("ordinary block must not read GitHub"))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows["t1"]["state"] == "blocked"


def test_prerequisite_block_releases_a_claim_with_an_unfinished_task_dependency(waiting):
    store, ledger, _ = waiting
    ledger.rows["t1"]["depends_on"] = ["t2"]
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    assert ledger.rows["t1"]["state"] == "blocked"
    assert store.agents("sw")[0].state == "finished"


@pytest.mark.parametrize("note", ["Waiting for operator credentials", "Stack after https://github.com/o/r/pull/78"])
def test_an_unrelated_block_after_a_refusal_is_not_reopened(waiting, monkeypatch, note):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", note) == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("unrelated block must not read GitHub"))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows["t1"]["state"] == "blocked"


def test_an_admitted_wait_clears_the_previous_prerequisite_offer(waiting, monkeypatch):
    store, ledger, state = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    state["entry"]["position"] = 1
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert run("sw", "--as", ME, "block", f"New blocker mentions old stack {URL}; next step credentials") == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("cleared offer must not read GitHub"))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows["t1"]["state"] == "blocked"


@pytest.mark.parametrize("position", [6, 39])
def test_every_position_beyond_one_group_requires_continuation(waiting, position):
    _, _, state = waiting
    state["entry"]["position"] = position
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1


@pytest.mark.parametrize("pull", [None, PullRequest("OPEN", None, 1, False), PullRequest("MERGED", 2, 1, False)])
def test_unqueued_or_unreadable_prerequisites_preserve_wait_admission(waiting, monkeypatch, pull):
    store, _, _ = waiting
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pull)
    monkeypatch.setattr(merge_queue, "operate", lambda *args: pytest.fail("unqueued pull needs no queue read"))
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME) is not None


@pytest.mark.parametrize("change", [{"state": "MERGED"}, {"queued": False}, {"entry": None}])
def test_a_fresh_queue_read_controls_admission(waiting, change):
    store, _, state = waiting
    state.update(change)
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME) is not None


def test_a_failed_queue_read_preserves_wait_admission(waiting, monkeypatch):
    store, _, _ = waiting

    def failed(*args):
        raise cli.SwarmError("GitHub unavailable")

    monkeypatch.setattr(merge_queue, "operate", failed)
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME) is not None


@pytest.mark.parametrize("change", ["agent", "generation"])
def test_another_claimant_cannot_consume_an_old_prerequisite_offer(waiting, monkeypatch, change):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    if change == "agent":
        store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t2", seat="eng-1@sw"))
        ledger.rows["t2"].update(state="claimed", claimed_by=ME)
        task_id = "t2"
    else:
        ledger.rows["t1"]["claim_generation"] = 1
        task_id = "t1"
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("stale offer must not read GitHub"))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert ledger.rows[task_id]["state"] == "blocked"


def test_an_unrelated_refusal_does_not_bypass_unfinished_dependencies(waiting):
    store, ledger, _ = waiting
    ledger.rows["t1"]["depends_on"] = ["t2"]
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", "Waiting for credentials") == 0
    assert ledger.rows["t1"]["state"] == "claimed"
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "task", "target": "t2"}


def test_a_queue_that_grows_long_clears_the_previous_idle_wait(waiting):
    store, _, state = waiting
    state["entry"]["position"] = 1
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME) is not None
    state["entry"]["position"] = 15
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert idle.wait(store.redis, "sw", ME) is None


def test_a_recorded_prerequisite_still_requires_continuation(waiting):
    store, ledger, _ = waiting
    ledger.rows["t1"]["pr_url"] = URL
    ledger.rows["t1"]["description"] = "This pull request is the compatibility prerequisite; implementation follows."
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert idle.wait(store.redis, "sw", ME) is None


@pytest.mark.parametrize("options", [[], ["--on", "checks", URL], ["--inbox"], ["--on", "reply", "item"]])
def test_final_confirmation_requires_a_merge_wait(waiting, capsys, options):
    assert run("sw", "--as", ME, "wait", *options, "--final") == 1
    assert capsys.readouterr().err.strip() == "swarm: --final requires --on merge for the task final deliverable"


def test_final_confirmation_cannot_name_another_pull_request(waiting, capsys):
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL, "--final") == 1
    assert capsys.readouterr().err.strip() == "swarm: --final requires the current task pull request"


def test_merge_wait_help_names_final_confirmation_and_existing_target_labels(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.build_parser().parse_args(["sw", "wait", "--help"])
    assert stopped.value.code == 0
    text = capsys.readouterr().out
    assert "--on KIND TARGET" in text
    assert "--final" in text


def test_reopening_records_the_resume_instruction_and_clears_the_retained_prerequisite(waiting, monkeypatch):
    store, ledger, _ = waiting
    ledger.rows["t1"]["claim_generation"] = 7
    store.redis.set(f"{store.key('sw', 'claim', 't1')}:generation", 7)
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    notes = []
    monkeypatch.setattr(ledger, "comment", lambda *args, **kwargs: notes.append((args, kwargs)))

    def merged(url):
        assert url == URL
        return PullRequest("MERGED", 2, 1, False)

    monkeypatch.setattr(cli.ledger_events, "view", merged)
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == [
        f"task t1 reopened after prerequisite {URL} merged"
    ]
    assert notes == [
        (
            (
                "sw",
                "t1",
                "The queued prerequisite merged. Resume the published stack and next step from the block notes.",
            ),
            {"by": "swarm"},
        )
    ]
    assert store.redis.hget(prerequisite_wait.key("sw"), "t1") is None
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == []
    assert len(notes) == 1


@pytest.mark.parametrize("first", ["offered", "changed", "missing", "unmerged"])
def test_one_unready_prerequisite_never_stops_another_ready_continuation(waiting, monkeypatch, first):
    store, ledger, state = waiting
    second_url = "https://github.com/o/r/pull/8"
    second_agent = "engineer@a1b2c3-0002"
    monkeypatch.setattr(merge_queue, "operate", lambda *args: state)
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    if first != "offered":
        assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    if first == "changed":
        ledger.rows["t1"]["state"] = "done"
    if first == "missing":
        del ledger.rows["t1"]
    ledger.rows["t2"].update(state="claimed", claimed_by=second_agent)
    store.put_agent("sw", AgentRecord(name=second_agent, lane="eng", task="t2", seat="eng-2@sw"))
    assert run("sw", "--as", second_agent, "wait", "--on", "merge", second_url) == 1
    assert run("sw", "--as", second_agent, "block", f"Stack published; next step restack after {second_url}") == 0
    monkeypatch.setattr(
        cli.ledger_events, "view", lambda url: PullRequest("MERGED" if url == second_url else "OPEN", 2, 1, False)
    )
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == [
        f"task t2 reopened after prerequisite {second_url} merged"
    ]
    assert ledger.rows["t2"]["state"] == "open"
    if first in {"changed", "missing"}:
        assert store.redis.hget(prerequisite_wait.key("sw"), "t1") is None


@pytest.mark.parametrize("change", [{"state": "claimed"}, {"claimed_by": "another-owner"}, {"claim_generation": 1}])
def test_reopening_checks_the_claim_and_block_state_atomically(waiting, monkeypatch, change):
    store, ledger, _ = waiting
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    snapshot = {key: dict(value) for key, value in ledger.rows.items()}

    def raced(url):
        assert url == URL
        ledger.rows["t1"].update(change)
        return PullRequest("MERGED", 2, 1, False)

    monkeypatch.setattr(cli.ledger_events, "view", raced)
    assert prerequisite_wait.reopen_pass("sw", store, ledger, snapshot) == []
    assert all(ledger.rows["t1"][field] == value for field, value in change.items())


@pytest.mark.parametrize("task_id", ["", "missing"])
def test_unbound_merge_waits_preserve_admission_without_final_confirmation(waiting, monkeypatch, task_id):
    store, _, _ = waiting
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task=task_id, seat="eng-1@sw"))
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pytest.fail("unbound wait needs no queue read"))
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME)["on"]["target"] == URL
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL, "--final") == 1


@pytest.mark.parametrize("claimant", ["engineer@a1b2c3-0002", ""])
def test_a_changed_claim_is_rejected_before_recording_a_prerequisite(waiting, capsys, claimant):
    store, ledger, _ = waiting
    ledger.rows["t1"]["claimed_by"] = claimant
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert capsys.readouterr().err.strip() == "swarm: task claim changed; refresh the task before waiting"
    assert idle.wait(store.redis, "sw", ME) is None
    assert store.redis.hget(prerequisite_wait.key("sw"), "t1") is None


@pytest.mark.parametrize("ledger_generation,store_generation", [(7, 3), (2, 9), (0, 1)])
def test_a_merge_wait_survives_drift_between_the_ledger_and_lease_generations(
    waiting, ledger_generation, store_generation
):
    store, ledger, state = waiting
    state["entry"]["position"] = 1
    ledger.rows["t1"]["claim_generation"] = ledger_generation
    store.redis.set(f"{store.key('sw', 'claim', 't1')}:generation", store_generation)
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "merge", "target": URL}


def test_a_prerequisite_offer_records_the_ledger_generation_despite_lease_drift(waiting, monkeypatch):
    store, ledger, _ = waiting
    ledger.rows["t1"]["claim_generation"] = 7
    store.redis.set(f"{store.key('sw', 'claim', 't1')}:generation", 3)
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 1
    assert json.loads(store.redis.hget(prerequisite_wait.key("sw"), "t1")) == {
        "agent": ME,
        "generation": 7,
        "target": URL,
    }
    assert run("sw", "--as", ME, "block", f"Stack published; next step restack after {URL}") == 0
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: PullRequest("MERGED", 2, 1, False))
    assert prerequisite_wait.reopen_pass("sw", store, ledger, ledger.rows) == [
        f"task t1 reopened after prerequisite {URL} merged"
    ]


MEMBER_READS = {
    "GET",
    "HGET",
    "HGETALL",
    "HEXISTS",
    "EXISTS",
    "SMEMBERS",
    "SISMEMBER",
    "MGET",
    "HKEYS",
    "HMGET",
    "TTL",
    "PTTL",
    "ZRANGE",
    "ZRANGEBYSCORE",
    "ZSCORE",
    "ZCARD",
    "SCARD",
    "HLEN",
    "LRANGE",
    "TYPE",
    "SCAN",
}


@pytest.mark.parametrize("kind", ["checks", "merge", "reply"])
def test_a_hive_member_credential_declares_checked_waits_through_the_cli(waiting, monkeypatch, kind):
    import fakeredis

    from scripts.hive import auth
    from scripts.inbox.store import InboxStore
    from scripts.swarm.store import RedisStore

    _, _, state = waiting
    state["entry"]["position"] = 1
    server = fakeredis.FakeServer()
    admin = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr(cli, "connect", lambda: RedisStore(admin))
    assert run("sw", "create", "--repo", "/repo") == 0
    RedisStore(admin).put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
    admin.acl_setuser(
        "hive-m1",
        enabled=True,
        nopass=True,
        categories=auth.ACL_CATEGORIES,
        keys=auth._acl_keys("m1"),
        channels=auth.ACL_PATTERNS,
    )

    # fakeredis drops %R~ read selectors, so reads the member's %R~ grant allows go through the admin client.
    class Member(fakeredis.FakeRedis):
        def execute_command(self, *args, **options):
            if str(args[0]).upper() in MEMBER_READS:
                return admin.execute_command(*args, **options)
            return super().execute_command(*args, **options)

    member = Member(server=server, decode_responses=True, username="hive-m1")
    member.auth("", username="hive-m1")
    assert member.acl_whoami() == "hive-m1"
    target = {
        "checks": "https://github.com/o/r/actions/runs/40200544185",
        "merge": URL,
        "reply": InboxStore(admin).send("operator", ME, "which branch?").id,
    }[kind]
    admin.hset(prerequisite_wait.key("sw"), "t1", "{}")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "m1")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_REDIS_URL", "redis://hive.example:6380/2")
    monkeypatch.setattr(cli, "connect", lambda: RedisStore(member))
    # The agent lookup runs a read-only script the fake server checks as a write; real Redis grants it by %R~.
    agent = RedisStore(admin).agents("sw")[0]
    monkeypatch.setattr(cli, "_me", lambda store, args: agent)
    assert run("sw", "--as", ME, "wait", "--on", kind, target) == 0
    assert idle.wait(member, "sw", ME)["on"] == {"kind": kind, "target": target}
    assert not admin.hexists(prerequisite_wait.key("sw"), "t1")
