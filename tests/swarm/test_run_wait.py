import json
import subprocess

import pytest

from scripts.swarm import cli, idle, mutation_wait, waits
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_waits import ME, held, started, tick  # noqa: F401

RUN = "https://github.com/org/repo/actions/runs/123"
ENDPOINT = "repos/org/repo/actions/runs/123"
JOBS = f"{ENDPOINT}/jobs?per_page=100"


@pytest.fixture
def actions(monkeypatch):
    state = {
        "run": {"status": "in_progress", "conclusion": None},
        "jobs": {"jobs": []},
        "jobs_code": 0,
        "code": 0,
        "stderr": b"",
        "asked": [],
    }

    def api(args, **kwargs):
        state["asked"].append(args)
        assert kwargs == {"capture_output": True, "timeout": 20}
        if args == ["gh", "api", JOBS]:
            out = json.dumps(state["jobs"]).encode() if state["jobs_code"] == 0 else b""
            return subprocess.CompletedProcess(args, state["jobs_code"], out, b"HTTP 502: Bad Gateway")
        out = json.dumps(state["run"]).encode() if state["code"] == 0 else b""
        return subprocess.CompletedProcess(args, state["code"], out, state["stderr"])

    monkeypatch.setattr(subprocess, "run", api)
    return state


def hold_run(rig):
    idle.declare_wait(rig.store.redis, "sw", ME, 10_000_000, "", 1, on=waits.on("checks", RUN))


def test_a_checks_wait_accepts_an_actions_run_without_reading_a_pull_request_head(
    started,  # noqa: F811
    monkeypatch,
    capsys,
):
    store, _ = started
    monkeypatch.setattr(cli.ledger_events, "head", lambda url: pytest.fail("a pull request head was read"))
    assert run("sw", "--as", ME, "wait", "--on", "checks", RUN, "--reason", "proof run") == 0
    assert held(store)["on"] == {"kind": "checks", "target": RUN}
    assert json.loads(capsys.readouterr().out)["on"] == {"kind": "checks", "target": RUN}


def test_checks_take_a_pull_request_or_a_run_and_merge_takes_only_a_pull_request():
    assert waits.target_problem("checks", RUN, "t1", {}, None) == ""
    assert waits.target_problem("checks", RUN + "/jobs/7", "t1", {}, None) == (
        f"wait on checks needs a pull request url or an Actions run url, not {RUN}/jobs/7"
    )
    assert waits.target_problem("merge", RUN, "t1", {}, None) == f"wait on merge needs a pull request url, not {RUN}"


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_a_run_still_going_keeps_the_wait(tick, actions, status):  # noqa: F811
    actions["run"] = {"status": status, "conclusion": None}
    hold_run(tick)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["target"] == RUN
    assert actions["asked"] == [["gh", "api", ENDPOINT]]


@pytest.mark.parametrize(
    "conclusion, outcome",
    [
        ("success", f"checks on {RUN}, now green"),
        ("failure", f"checks on {RUN}, now red; the run concluded failure"),
        ("timed_out", f"checks on {RUN}, now red; the run concluded timed_out"),
        ("cancelled", f"checks on {RUN}, cancelled; dispatch the run again and wait on the new one"),
    ],
)
def test_the_tick_ends_a_run_wait_with_its_conclusion(tick, actions, conclusion, outcome):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": conclusion}
    hold_run(tick)
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    ]


def test_a_missing_run_ends_the_wait_with_that_reason(tick, actions):  # noqa: F811
    actions["code"], actions["stderr"] = 1, b"gh: Not Found (HTTP 404)"
    hold_run(tick)
    assert mutation_wait.run_resolution(RUN) == f"checks on {RUN}, missing; GitHub has no such run"
    assert tick.end() == [f"ended the wait of {ME}: checks on {RUN}, missing; GitHub has no such run"]


def test_an_unreachable_github_keeps_the_run_wait(tick, actions):  # noqa: F811
    actions["code"], actions["stderr"] = 1, b"HTTP 502: Bad Gateway"
    hold_run(tick)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["target"] == RUN


def job(name, conclusion):
    return {"name": name, "status": "completed", "conclusion": conclusion}


def test_a_red_run_names_its_failed_jobs_and_ends_the_wait(tick, actions):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "failure"}
    actions["jobs"] = {
        "jobs": [
            job("lint", "success"),
            job("unit (3.11)", "failure"),
            job("unit (3.12)", "timed_out"),
            job("mutation", "cancelled"),
            job("docs", "skipped"),
        ]
    }
    hold_run(tick)
    outcome = (
        f"checks on {RUN}, now red; the run concluded failure; "
        "failed jobs: unit (3.11) failure, unit (3.12) timed_out, mutation cancelled"
    )
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    ]
    assert actions["asked"] == [["gh", "api", ENDPOINT], ["gh", "api", JOBS]]


def test_a_green_run_reads_no_jobs(tick, actions):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "success"}
    hold_run(tick)
    assert tick.end() == [f"ended the wait of {ME}: checks on {RUN}, now green"]
    assert actions["asked"] == [["gh", "api", ENDPOINT]]


def test_a_red_run_with_no_failed_job_names_only_its_conclusion(tick, actions):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "failure"}
    actions["jobs"] = {"jobs": [job("lint", "success"), job("docs", "skipped")]}
    hold_run(tick)
    assert tick.end() == [f"ended the wait of {ME}: checks on {RUN}, now red; the run concluded failure"]
    assert actions["asked"] == [["gh", "api", ENDPOINT], ["gh", "api", JOBS]]


def test_unreadable_jobs_still_end_a_red_run_wait_with_its_conclusion(tick, actions):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "failure"}
    actions["jobs_code"] = 1
    hold_run(tick)
    outcome = f"checks on {RUN}, now red; the run concluded failure; its jobs could not be read"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None


@pytest.mark.parametrize(
    "answer",
    [{"message": "no jobs"}, [], {"jobs": ["unit"]}, {"jobs": [{"conclusion": "failure"}]}],
    ids=["no jobs key", "not an object", "job not an object", "job without a name"],
)
def test_a_malformed_jobs_answer_still_ends_a_red_run_wait(tick, actions, answer):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "timed_out"}
    actions["jobs"] = answer
    hold_run(tick)
    outcome = f"checks on {RUN}, now red; the run concluded timed_out; its jobs could not be read"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None


def test_the_wait_timer_ends_a_run_wait_while_the_tick_snapshot_is_blocked(env, actions, monkeypatch):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from scripts.inbox.store import InboxStore
    from scripts.swarm import snapshot
    from scripts.swarm.store import AgentRecord
    from tests.swarm.test_delivery import FakeHerdr

    store, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    entered, release = Event(), Event()

    def slow_snapshot(*args):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(snapshot, "auto", slow_snapshot)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cli.run_tick, store, "sw", ledger, runtime, FakeHerdr({}))
        try:
            assert entered.wait(5)
            store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
            idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on={"kind": "checks", "target": RUN})
            actions["run"] = {"status": "completed", "conclusion": "success"}
            assert cli.main(["waits"]) == 0
            assert not future.done()
            assert idle.wait(store.redis, "sw", ME) is None
            inbox = InboxStore(store.redis)
            notices = [item.text for item in inbox.inbox("eng-1@sw")]
            assert notices == [
                f"Your wait on checks on {RUN}, now green has ended. Pick task t1 back up: "
                "agentihooks swarm sw done, block, or wait on the next thing."
            ]
            assert cli.main(["waits"]) == 0
            assert [item.text for item in inbox.inbox("eng-1@sw")] == notices
        finally:
            release.set()
        future.result()


def test_the_wait_pass_leaves_a_foreign_controllers_wait_untouched(tick, monkeypatch):  # noqa: F811
    from scripts.swarm import lease

    hold_run(tick)
    before = idle.wait(tick.store.redis, "sw", ME)
    lease.acquire(tick.store, "sw", "another-controller")
    monkeypatch.setattr(cli, "LedgerClient", lambda: pytest.fail("foreign ledger was read"))
    assert cli.run_waits(tick.store, "sw") == []
    assert idle.wait(tick.store.redis, "sw", ME) == before
    assert tick.told() == []


@pytest.mark.parametrize("authority", [None, "trusted-tick-fixture"])
def test_the_wait_pass_reads_a_remote_ledger_only_with_tick_authority(tick, monkeypatch, authority):  # noqa: F811
    from scripts.swarm import lease
    from scripts.swarm_ledger import authority_context

    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.delenv("LEDGER_URL", raising=False)
    tick.store.redis.hset(tick.store.key("sw", "config"), "api_url", "https://hosted.example")
    hold_run(tick)
    before = idle.wait(tick.store.redis, "sw", ME)
    acquired, read = [], []
    monkeypatch.setattr(
        lease, "acquire", lambda _store, slug, owner: acquired.append(slug) or lease.Lease(owner, 1, 10**15)
    )

    def client():
        read.append(True)
        raise RuntimeError("remote ledger read")

    monkeypatch.setattr(cli, "LedgerClient", client)
    with authority_context.bound(authority):
        if authority:
            with pytest.raises(RuntimeError, match="remote ledger read"):
                cli.run_waits(tick.store, "sw")
        else:
            assert cli.run_waits(tick.store, "sw") == []
    assert acquired == (["sw"] if authority else [])
    assert read == ([True] if authority else [])
    assert idle.wait(tick.store.redis, "sw", ME) == before
    assert tick.told() == []


def test_another_sessions_hosted_swarm_leaves_this_wait_pass_local(tick, monkeypatch):  # noqa: F811
    from scripts.swarm import lease
    from scripts.swarm_ledger import authority_context

    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.delenv("LEDGER_URL", raising=False)
    monkeypatch.setenv("SWARM_URL", "https://hosted.example")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "other")
    acquired = []
    monkeypatch.setattr(lease, "acquire", lambda _store, slug, owner: acquired.append(slug))
    with authority_context.bound(None):
        assert cli.run_waits(tick.store, "sw") == []
    assert acquired == ["sw"]


@pytest.mark.parametrize("held", ["none", "bare", "finished"])
def test_the_wait_pass_skips_ledger_reads_without_active_checked_waits(tick, monkeypatch, held):  # noqa: F811
    if held != "none":
        hold_run(tick)
    if held == "bare":
        idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "a short break", 1)
    if held == "finished":
        from dataclasses import replace

        tick.store.put_agent("sw", replace(tick.store.agents("sw")[0], state="finished"))
    monkeypatch.setattr(cli, "LedgerClient", lambda: pytest.fail("idle ledger was read"))
    assert cli.run_waits(tick.store, "sw") == []
    assert tick.told() == []


def test_the_wait_command_checks_other_swarms_despite_a_failure_or_slow_pass(monkeypatch, capsys):
    from argparse import Namespace
    from threading import Event
    from types import SimpleNamespace

    slow, checked = Event(), Event()

    def check(store, slug):
        if slug == "broken":
            raise RuntimeError("unavailable")
        if slug == "slow":
            slow.set()
            assert checked.wait(5)
        else:
            assert slow.wait(5)
            checked.set()
        return ["wait checked"]

    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(cli, "run_waits", check)
    cli.cmd_waits(SimpleNamespace(slugs=lambda: ["broken", "slow", "fast"]), Namespace())
    output = capsys.readouterr()
    assert set(output.out.splitlines()) == {"slow: wait checked", "fast: wait checked"}
    assert output.err == "broken: RuntimeError: unavailable\n"


def test_the_wait_command_accepts_an_empty_swarm_list(monkeypatch, capsys):
    from argparse import Namespace
    from types import SimpleNamespace

    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    cli.cmd_waits(SimpleNamespace(slugs=lambda: []), Namespace())
    assert capsys.readouterr() == ("", "")


def test_every_wait_pass_guards_the_scheduled_tick(monkeypatch, capsys):
    from argparse import Namespace
    from types import SimpleNamespace

    guarded = []
    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(cli.timer, "entry_point", lambda: "/installed/bin/agentihooks")
    monkeypatch.setattr(cli.timer, "guard", lambda binary: guarded.append(binary) or True)
    cli.cmd_waits(SimpleNamespace(slugs=lambda: []), Namespace())
    cli.cmd_waits(SimpleNamespace(slugs=lambda: []), Namespace())
    assert guarded == ["/installed/bin/agentihooks"] * 2
    assert capsys.readouterr() == ("", "")


def test_a_failed_tick_guard_is_logged_and_the_wait_pass_goes_on(monkeypatch, capsys):
    from argparse import Namespace
    from types import SimpleNamespace

    def broken(binary):
        raise OSError("no systemctl")

    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(cli.timer, "guard", broken)
    monkeypatch.setattr(cli, "run_waits", lambda store, slug: ["wait checked"])
    cli.cmd_waits(SimpleNamespace(slugs=lambda: ["sw"]), Namespace())
    output = capsys.readouterr()
    assert output.out == "sw: wait checked\n"
    assert output.err == "the scheduled swarm tick guard failed: OSError: no systemctl\n"


def test_a_guard_that_cannot_enable_the_tick_timer_warns(monkeypatch, capsys):
    from argparse import Namespace
    from types import SimpleNamespace

    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(cli.timer, "guard", lambda binary: False)
    cli.cmd_waits(SimpleNamespace(slugs=lambda: []), Namespace())
    assert capsys.readouterr() == (
        "",
        "warning: the scheduled swarm tick guard could not enable the swarm tick timer\n",
    )


def test_the_wait_command_refuses_a_run_outside_the_install(monkeypatch):
    from argparse import Namespace

    from scripts.swarm.store import SwarmError

    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "outside the installed checkout")
    with pytest.raises(SwarmError, match="the wait pass refused to run: outside the installed checkout"):
        cli.cmd_waits(None, Namespace())


def test_the_wait_pass_rechecks_ownership_after_reading_github(tick, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    from scripts.swarm import lease
    from scripts.swarm.store import SwarmError

    hold_run(tick)
    before = idle.wait(tick.store.redis, "sw", ME)

    def changed_owner(*args):
        tick.store.redis.delete(tick.store.key("sw", "control-owner"))
        lease.acquire(tick.store, "sw", "another-controller")
        return lambda url: None

    monkeypatch.setattr(cli.ledger_events, "tick_view", changed_owner)
    ledger = SimpleNamespace(state=lambda slug: {"tasks": []})
    with pytest.raises(SwarmError):
        cli.run_waits(tick.store, "sw", ledger)
    assert idle.wait(tick.store.redis, "sw", ME) == before
    assert tick.told() == []


def test_the_wait_pass_confirms_a_merge_wait_with_an_uncached_github_read(tick, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    from tests.swarm.test_waits import LISTED, UNLISTED, URL

    tick.hold("merge", URL)
    monkeypatch.setattr(cli.ledger_events, "tick_view", lambda *args: {URL: UNLISTED}.get)
    monkeypatch.setattr(cli.ledger_events, "view", {URL: LISTED}.get)
    ledger = SimpleNamespace(state=lambda slug: {"tasks": []})
    assert cli.run_waits(tick.store, "sw", ledger) == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "merge", "target": URL, "queued": True}
    assert tick.told() == []


def test_the_wait_pass_resolves_against_current_ledger_tasks(tick):  # noqa: F811
    from types import SimpleNamespace

    tick.hold("task", "t2")
    documents = {"sw": {"tasks": [{"id": "t2", "state": "done"}]}}
    ledger = SimpleNamespace(state=documents.__getitem__)
    assert cli.run_waits(tick.store, "sw", ledger) == [f"ended the wait of {ME}: task t2, now done"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert len(tick.told()) == 1


def test_the_wait_pass_resolves_waits_beside_an_open_red_check_notice(tick, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    from tests.swarm.test_waits import URL

    tick.hold("task", "t2")
    notice = tick.inbox.send("swarm", "eng-1@sw", "A previous pull request has failed checks")
    tick.store.redis.hset(tick.store.key("sw", "red-notices"), notice.id, URL)
    monkeypatch.setattr(cli.ledger_events, "views", lambda urls, cache: {})
    documents = {"sw": {"tasks": [{"id": "t2", "state": "done"}]}}
    assert cli.run_waits(tick.store, "sw", SimpleNamespace(state=documents.__getitem__)) == [
        f"ended the wait of {ME}: task t2, now done"
    ]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.inbox.get(notice.id).state == "pending"
    assert tick.told() == [
        "A previous pull request has failed checks",
        "Your wait on task t2, now done has ended. Pick task t1 back up: "
        "agentihooks swarm sw done, block, or wait on the next thing.",
    ]
