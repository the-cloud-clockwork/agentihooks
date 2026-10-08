import json
import subprocess

import pytest

from scripts.gates import log as gate_log
from scripts.inbox.store import InboxStore
from scripts.swarm import cli, runtime, timer
from scripts.swarm.health import checks
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.resume import Outcome
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


from tests.swarm.profile_fixture import validated


@pytest.fixture
def env(monkeypatch, tmp_path):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "ci"}])
    ledger.said = []
    ledger.comments = []
    ledger.say = lambda slug, text, by=None: ledger.said.append((text, by))
    ledger.relay = lambda slug, text, by: ledger.said.append((text, by))
    ledger.comment = lambda slug, task, text, by: ledger.comments.append((task, text, by))
    rt = FakeRuntime()
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: rt)
    monkeypatch.setattr(cli.timer, "ensure", lambda binary: True)
    ledger.chat = lambda slug: [{"id": "old", "by": "operator", "at": 50, "text": "old talk"}]
    monkeypatch.setattr(cli.delivery, "HerdrMessenger", lambda: FakeHerdr({}))
    ledger.pulls = {}
    monkeypatch.setattr(cli, "pull_branch", lambda url: "")
    monkeypatch.setattr(
        cli.ledger_events, "view", lambda url: ledger.pulls.get(url, PullRequest("MERGED", 1, 1, False))
    )
    return store, ledger, rt


def run(*argv):
    return cli.main(list(argv))


def test_create_is_paused_then_start_spawns_and_status_lists(env, capsys):
    store, ledger, rt = env
    assert run("sw", "create", "--repo", "/repo", "--max-eng-agents", "1", "--max-ci-agents", "1") == 0
    assert store.config("sw").state == "paused" and rt.spawned == []
    assert run("sw", "start") == 0
    assert [s[1] for s in rt.spawned] == ["engineer@a1b2c3-0001", "ci@a1b2c3-0001"]
    run("sw", "status")
    assert "engineer@a1b2c3-0001" in capsys.readouterr().out


def test_start_reports_plan_shape_and_warns_before_spawning(env, capsys, monkeypatch):
    store, ledger, _ = env
    ledger.rows = {"a": {"id": "a", "lane": "eng"}, "b": {"id": "b", "lane": "eng", "depends_on": ["a"]}}
    run("demo", "create", "--repo", "/repo", "--max-eng-agents", "4")
    capsys.readouterr()
    observed = []
    monkeypatch.setattr(cli, "run_tick", lambda *args: observed.append(capsys.readouterr()) or [])
    assert run("demo", "start") == 0
    assert "Critical path: 2 tasks (a -> b)" in observed[0].out
    assert "Parallel width: 1 tasks; engineer width: 1" in observed[0].out
    assert "engineer width 1 is below engineer cap 4" in observed[0].err


def test_status_json_includes_plan_shape_without_cap_warning_when_wide_enough(env, capsys):
    run("demo", "create", "--repo", "/repo", "--max-eng-agents", "1")
    capsys.readouterr()
    run("demo", "status", "--json")
    shape = json.loads(capsys.readouterr().out)["plan_shape"]
    assert shape["chain_length"] == 1
    assert shape["parallel_width"] == 2
    assert shape["engineer_width"] == 1
    assert shape["warning"] == ""


def test_create_marks_the_ledger_as_a_swarm_ledger(env):
    _, ledger, _ = env
    assert run("sw", "create", "--repo", "/repo") == 0
    assert ledger.swarm_sized == ["sw"]


def test_bare_pairs_set_the_caps(env):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "max-eng-agents=4", "max-ci-agents=0") == 0
    assert (store.config("sw").max_eng, store.config("sw").max_ci) == (4, 0)
    assert run("sw", "set", "max-eng-agents=x") == 1


def test_done_closes_the_task_and_marks_the_agent_finished(env, monkeypatch):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    assert run("sw", "done", "--pr", "https://github.com/o/r/pull/9") == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["pr_url"]) == ("done", "https://github.com/o/r/pull/9")
    assert [a.state for a in store.agents("sw") if a.name == "engineer@a1b2c3-0001"] == ["finished"]
    assert store.claimant("sw", "t1") is None


def test_done_on_a_group_lead_closes_its_members_with_the_lead_pull_request(env, monkeypatch):
    store, ledger, _ = env
    ledger.rows["t1"]["group_members"] = ["t3", "t4"]
    member = {"lane": "eng", "title": "member", "description": "spec", "claimed_by": "", "merged_into": "t1"}
    ledger.rows["t3"] = {"id": "t3", "state": "open", **member}
    ledger.rows["t4"] = {"id": "t4", "state": "done", **member}
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    url = "https://github.com/o/r/pull/9"
    ledger.rows["t1"]["pr_url"] = url
    ledger.updates, slugs = [], []
    original, listed = ledger.update_task, ledger.tasks
    ledger.update_task = lambda slug, task_id, fields, **kw: (
        ledger.updates.append((slug, task_id, kw["by"])) or original(slug, task_id, fields, **kw)
    )
    ledger.tasks = lambda slug: slugs.append(slug) or listed(slug)
    assert run("sw", "done", "--finding", "one pull request") == 0
    assert (ledger.rows["t3"]["state"], ledger.rows["t3"]["pr_url"]) == ("done", url)
    assert ledger.rows["t3"]["proof"] == {"finding": "one pull request"}
    agent = "engineer@a1b2c3-0001"
    assert ledger.updates == [("sw", "t1", agent), ("sw", "t3", agent)]
    assert set(slugs) == {"sw"}


def test_done_on_a_code_task_waits_for_its_pull_request_to_merge(env, monkeypatch, capsys):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    url = "https://github.com/o/r/pull/9"
    ledger.pulls[url] = PullRequest("OPEN", None, 1, False)
    capsys.readouterr()
    assert run("sw", "done", "--pr", url) == 1
    assert capsys.readouterr().err.strip() == (
        f"swarm: pull request {url} is open, not merged; merge it, then run swarm done again"
    )
    assert ledger.rows["t1"]["state"] != "done"
    assert [a.state for a in store.agents("sw") if a.name == "engineer@a1b2c3-0001"] != ["finished"]
    assert run("sw", "done") == 1
    assert "give --pr <url>" in capsys.readouterr().err
    ledger.rows["t1"]["pr_url"] = url
    ledger.pulls[url] = PullRequest("MERGED", 1, 1, False)
    assert run("sw", "done") == 0
    assert ledger.rows["t1"]["state"] == "done"


@pytest.mark.parametrize("kind", ["code", "ci"])
def test_done_waits_on_a_queued_pull_request_and_accepts_only_after_it_lands(env, monkeypatch, capsys, kind):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    name = "engineer@a1b2c3-0001"
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    url = "https://github.com/o/r/pull/9"
    ledger.rows["t1"].update(kind=kind, pr_url=url)
    ledger.pulls[url] = PullRequest("OPEN", None, 1, False, resolved=True, head="first", queued=True)
    monkeypatch.setattr(cli, "now_ms", lambda: 1000)
    assert run("sw", "done") == 1
    assert "merge queue" in capsys.readouterr().err
    assert ledger.rows["t1"]["state"] != "done"
    assert store.agents("sw")[0].state != "finished"
    assert cli.idle.wait(store.redis, "sw", name) == {
        "until": 43_201_000,
        "reason": "merge queue",
        "at": 1000,
        "on": {"kind": "merge", "target": url},
    }
    assert cli.waits.end_pass(store, "sw", ledger.rows, InboxStore(store.redis), ledger.pulls.get, 1000) == []
    ledger.pulls[url] = PullRequest("MERGED", 2, 1, False)
    ended = cli.waits.end_pass(store, "sw", ledger.rows, InboxStore(store.redis), ledger.pulls.get, 2000)
    assert ended == [f"ended the wait of {name}: pull request {url}, now merged"]
    assert cli.idle.wait(store.redis, "sw", name) is None
    assert run("sw", "done") == 0
    assert ledger.rows["t1"]["state"] == "done"
    assert next(a for a in store.agents("sw") if a.name == name).state == "finished"


def test_an_ops_task_completes_with_its_proof_even_when_a_linked_pull_request_is_queued(env, monkeypatch):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    name = "engineer@a1b2c3-0001"
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    ledger.rows["t1"]["kind"] = "ops"
    url = "https://github.com/o/r/pull/9"
    ledger.pulls[url] = PullRequest("OPEN", None, 1, False, queued=True)
    assert run("sw", "done", "--pr", url, "--command", "probe", "--output", "passed") == 0
    assert ledger.rows["t1"]["state"] == "done"
    assert cli.idle.wait(store.redis, "sw", name) is None


def test_the_tick_reopens_a_done_task_whose_pull_request_closed_unmerged(env, monkeypatch):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    url = "https://github.com/o/r/pull/4"
    monkeypatch.setattr(cli, "now_ms", lambda: 9_000_000)
    ledger.rows["t2"].update(state="done", kind="ci", pr_url=url)
    ledger.log = [{"kind": "task done", "target": "tasks/t2", "by": "ci@a1b2c3-0001", "at": 8_000_000}]
    ledger.pulls[url] = PullRequest("CLOSED", None, 1, False)
    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert "task t2 reopened, its pull request is closed" in actions
    assert (ledger.rows["t2"]["state"], ledger.rows["t2"]["claimed_by"]) == ("open", "")
    assert ledger.comments[-1][0::2] == ("t2", "swarm")


@pytest.mark.parametrize("dependencies", [None, [], ["done"]])
def test_block_comments_parks_and_finishes(env, dependencies, capsys):
    from scripts.swarm import idle

    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    if dependencies is not None:
        ledger.rows["t2"]["depends_on"] = dependencies
    ledger.rows["done"] = {"id": "done", "state": "done"}
    capsys.readouterr()
    assert run("sw", "--as", "ci@a1b2c3-0001", "block", "waiting on a token only the operator can create") == 0
    assert ledger.rows["t2"]["state"] == "blocked"
    assert ledger.comments == [("t2", "waiting on a token only the operator can create", "ci@a1b2c3-0001")]
    assert store.agents("sw")[0].state == "finished"
    assert store.claimant("sw", "t2") is None
    assert idle.wait(store.redis, "sw", "ci@a1b2c3-0001") is None
    assert json.loads(capsys.readouterr().out) == {
        "task": "t2",
        "state": "blocked",
        "next": "stop now; the swarm closes this session",
    }


@pytest.mark.parametrize("dependency_state", ["open", "claimed", "pr", "blocked"])
def test_block_waits_on_the_first_unfinished_dependency(env, capsys, monkeypatch, dependency_state):
    from scripts.swarm import idle

    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    agent = store.agents("sw")[0]
    ledger.rows[agent.task]["depends_on"] = ["done", "first", "second"]
    ledger.rows.update(
        done={"id": "done", "state": "done"},
        first={"id": "first", "state": dependency_state},
        second={"id": "second", "state": "pr"},
    )
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", agent.name, "continue after the prerequisite")
    notice = inbox.send("swarm", agent.name, f"Your wait ended. Pick task {agent.task} back up: continue.")
    before = dict(ledger.rows[agent.task])
    monkeypatch.setattr(cli, "now_ms", lambda: 1_000)
    capsys.readouterr()

    assert run("sw", "--as", agent.name, "block", "waiting for the prerequisite") == 0

    assert ledger.rows[agent.task] == before
    assert before["state"] == "claimed"
    assert store.claimant("sw", agent.task) == agent.name
    assert store.agents("sw")[0].state == "working"
    assert ledger.comments == []
    assert ledger.notes == []
    assert inbox.get(item.id).state == "pending"
    closed = inbox.get(notice.id)
    assert (closed.state, closed.reason) == (
        "done",
        f"done: {agent.name} recorded a new wait on task {agent.task}",
    )
    assert idle.wait(store.redis, "sw", agent.name) == {
        "until": 43_201_000,
        "reason": "waiting for the prerequisite",
        "at": 1_000,
        "on": {"kind": "task", "target": "first"},
    }
    assert json.loads(capsys.readouterr().out) == {
        "task": agent.task,
        "state": "claimed",
        "waits_on": {"kind": "task", "target": "first"},
    }


def test_say_addresses_and_strangers_are_refused(env, capsys):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.said.clear()
    assert run("sw", "--as", "engineer@a1b2c3-0001", "say", "the docs task is merged", "--to", "ci") == 0
    assert ledger.said == [("@ci the docs task is merged", "engineer@a1b2c3-0001")]
    [item] = InboxStore(store.redis).inbox("ci@a1b2c3-0001")
    assert (item.sender, item.text, item.state) == ("engineer@a1b2c3-0001", "the docs task is merged", "pending")
    assert run("sw", "--as", "engineer@a1b2c3-0001", "say", "status for the page only") == 0
    assert ledger.said[-1] == ("status for the page only", "engineer@a1b2c3-0001")
    assert [i.text for i in InboxStore(store.redis).inbox("ci@a1b2c3-0001")] == ["the docs task is merged"]

    assert run("sw", "--as", "stranger", "say", "hello") == 1
    assert "not an agent" in capsys.readouterr().err


def test_say_with_fyi_marks_each_item_as_needing_no_work(env):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as", "engineer@a1b2c3-0001", "say", "thanks for the review", "--to", "ci", "--fyi") == 0
    assert run("sw", "--as", "engineer@a1b2c3-0001", "say", "please rerun the checks", "--to", "ci") == 0
    assert [(i.text, i.fyi) for i in InboxStore(store.redis).inbox("ci@a1b2c3-0001")] == [
        ("thanks for the review", True),
        ("please rerun the checks", False),
    ]


def test_stop_now_terminates_reopens_claimed_but_not_finished_work(env, monkeypatch):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "ci@a1b2c3-0001")
    run("sw", "done", "--pr", "https://github.com/o/r/pull/4")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    assert run("sw", "stop", "--now") == 0
    assert sorted(rt.killed) == ["ci@a1b2c3-0001", "engineer@a1b2c3-0001", "master@a1b2c3-0001"]
    assert store.config("sw").state == "stopped" and store.agents("sw") == []
    assert (ledger.rows["t1"]["state"], ledger.rows["t2"]["state"]) == ("open", "done")


def test_stop_now_retires_each_agent_with_its_task_scratch_homes(env, scratch):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    homes = {a.name: scratch(a.task) for a in store.agents("sw")}
    assert run("sw", "stop", "--now") == 0
    assert rt.homes == homes


def test_a_binned_ledger_stops_its_swarm_and_the_tick_leaves_it_alone(env):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    panes = sorted(a.pane_id for a in store.agents("sw"))
    ledger.bin = {"sw"}
    assert run("sw", "stop", "--now") == 0
    assert store.agents("sw") == [] and sorted(rt.closed) == panes and rt.live == set()
    assert rt.closed_spaces == ["sw"]
    store.update("sw", state="running")
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == ["the ledger is in the bin, stopped"]
    assert store.agents("sw") == [] and len(rt.spawned) == 2 and len(rt.masters) == 1


@pytest.mark.parametrize("state", ["running", "stopping"])
def test_one_tick_after_binning_leaves_no_agent_and_no_space(env, state):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert {a.lane for a in store.agents("sw")} == {"master", "eng", "ci"}
    store.update("sw", state=state)
    ledger.bin = {"sw"}
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == ["the ledger is in the bin, stopped"]
    assert store.agents("sw") == [] and rt.live == set() and rt.closed_spaces == ["sw"]
    assert store.config("sw").state == "stopped"
    assert (ledger.rows["t1"]["state"], ledger.rows["t2"]["state"]) == ("open", "open")


def test_a_refused_reopen_on_the_bin_stop_path_skips_that_task_and_still_stops(env, capsys):
    from scripts.swarm.ledger_client import LedgerRefused

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    update = ledger.update_task

    def refuse_t1(slug, task_id, fields, by="swarm"):
        if task_id == "t1":
            raise LedgerRefused("ledger sw: server refused: 400 the ledger is in the bin")
        return update(slug, task_id, fields, by)

    ledger.update_task = refuse_t1
    assert (ledger.rows["t1"]["state"], ledger.rows["t2"]["state"]) == ("claimed", "claimed")
    ledger.bin = {"sw"}
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == ["the ledger is in the bin, stopped"]
    assert store.agents("sw") == [] and rt.live == set() and rt.closed_spaces == ["sw"]
    assert store.config("sw").state == "stopped"
    assert (ledger.rows["t1"]["state"], ledger.rows["t2"]["state"]) == ("claimed", "open")
    err = capsys.readouterr().err
    assert (
        "task t1 not reopened, the ledger refused its write: ledger sw: server refused: 400 the ledger is in the bin"
        in err
    )


def test_a_binned_ledger_keeps_retiring_until_no_agent_is_left(env):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    stuck = [a.name for a in store.agents("sw") if a.lane != "ci"]
    rt.stuck = set(stuck)
    ledger.bin = {"sw"}
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == [
        f"the ledger is in the bin, still retiring {stuck[0]}, {stuck[1]}"
    ]
    assert [a.name for a in store.agents("sw")] == stuck and rt.closed_spaces == []
    assert store.config("sw").state == "stopping"
    rt.stuck = set()
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == ["the ledger is in the bin, stopped"]
    assert store.agents("sw") == [] and rt.closed_spaces == ["sw"]


def test_binning_releases_and_reopens_only_this_swarms_claims(env):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert {store.claimant("sw", t) for t in ("t1", "t2")} == {a.name for a in store.agents("sw") if a.lane != "master"}
    tasks, update = ledger.tasks, ledger.update_task
    ledger.tasks = lambda slug: tasks(slug) if slug == "sw" else []
    ledger.update_task = lambda slug, task_id, fields, by="swarm": slug == "sw" and update(slug, task_id, fields, by)
    states, retire = [], rt.retire
    rt.retire = lambda agent, homes=(): states.append(store.config("sw").state) or retire(agent, homes)
    ledger.bin = {"sw"}
    assert cli.run_tick(store, "sw", ledger, None, FakeHerdr({})) == ["the ledger is in the bin, stopped"]
    assert states == ["stopping", "stopping", "stopping"]
    assert [(ledger.rows[t]["state"], ledger.rows[t]["claimed_by"]) for t in ("t1", "t2")] == [
        ("open", ""),
        ("open", ""),
    ]
    assert (store.claimant("sw", "t1"), store.claimant("sw", "t2")) == (None, None)


def test_stop_now_prints_its_state_and_who_is_still_running(env, capsys):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    stuck = next(a.name for a in store.agents("sw") if a.lane == "master")
    rt.stuck = {stuck}
    capsys.readouterr()
    assert run("sw", "stop", "--now") == 0
    assert capsys.readouterr().out == json.dumps({"swarm": "sw", "state": "stopping", "still_running": [stuck]}) + "\n"


@pytest.mark.parametrize("state", ["running", "paused", "stopped"])
def test_a_binned_ledger_never_gets_a_new_spawn(env, state):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state=state)
    ledger.bin = {"sw"}
    InboxStore(store.redis).send("operator", "master@sw", "wake up")
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert rt.spawned == [] and rt.masters == [] and store.agents("sw") == []
    assert store.config("sw").state == "stopped"


def test_restoring_from_the_bin_leaves_the_swarm_stopped(env):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.bin = {"sw"}
    run("sw", "stop", "--now")
    ledger.bin = set()
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert store.config("sw").state == "stopped"
    assert store.agents("sw") == [] and len(rt.spawned) == 2 and len(rt.masters) == 1


def test_create_refuses_ids_that_break_agent_names(env, capsys):
    assert run("2026-q4", "create", "--repo", "/repo") == 1
    assert "starting with a letter" in capsys.readouterr().err


def test_as_equals_is_not_mistaken_for_set(env, monkeypatch):
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as=engineer@a1b2c3-0001", "issue", "https://github.com/o/r/issues/1") == 0


def test_redis_down_is_a_clear_failure(monkeypatch, capsys):
    def down():
        raise SwarmError("Redis is unreachable; the swarm refuses to run without it")

    monkeypatch.setattr(cli, "connect", down)
    assert run("sw", "status") == 1
    assert "refuses to run" in capsys.readouterr().err


def test_a_concurrent_tick_is_skipped(env):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state="running")
    store.redis.set(store.key("sw", "tick-lock"), "1")
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == ["another tick is running"]


def test_tick_reports_its_cost_even_when_a_lock_skips_work(env, capsys):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.redis.set(store.key("sw", "tick-lock"), "1")
    capsys.readouterr()
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == ["another tick is running"]
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines() if '"swarm_tick_step"' in line]
    assert [row["phase"] for row in rows] == ["started", "finished"]
    assert [row["step"] for row in rows] == ["run_tick", "run_tick"]
    assert all(row["slug"] == "sw" for row in rows)
    assert rows[1]["outcome"] == "success"
    assert all(rows[1][key] >= 0 for key in ("wall_s", "own_cpu_s", "reaped_child_cpu_s"))


def test_tick_reports_spawn_intent_and_store_stages(env, capsys):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state="running")
    capsys.readouterr()
    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert any(action.startswith("spawned engineer") for action in actions)
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines() if '"swarm_tick_step"' in line]
    finished = {row["step"] for row in rows if row["phase"] == "finished"}
    assert {
        "scripts.swarm.tick._spawn",
        "scripts.gates.intent.Check.run",
        "scripts.swarm.store.RedisStore.agents",
    } <= finished
    assert rows[0]["step"] == rows[-1]["step"] == "run_tick"
    assert rows[-1]["outcome"] == "success"
    assert all(row["slug"] == "sw" for row in rows)


def test_tick_records_failed_stage_and_releases_its_lock(env, monkeypatch, capsys):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    capsys.readouterr()
    error = ValueError("a failed stage")

    def failing(*args):
        raise error

    monkeypatch.setattr(cli.phase_planning, "planning_pass", failing)
    with pytest.raises(ValueError) as caught:
        cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert caught.value is error
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines() if '"swarm_tick_step"' in line]
    assert [row["outcome"] for row in rows if row.get("outcome") == "error"] == ["error", "error"]
    assert rows[-1]["step"] == "run_tick"
    assert not store.redis.exists(store.key("sw", "tick-lock"))
    assert cli.timing.SWARM.get() is None


def test_the_tick_hands_the_wake_pass_its_clock_window_and_quiet_window(env, monkeypatch):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    calls = []
    monkeypatch.setattr(cli, "now_ms", lambda: 9_000_000)
    monkeypatch.setenv(cli.wake.WINDOW_ENV, "60")
    monkeypatch.setenv(cli.wake.QUIET_ENV, "7")
    monkeypatch.setattr(cli.wake, "wake_pass", lambda *args: calls.append(args[-3:]) or [])
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert calls == [(9_000_000, 60_000, 7_000)]


def test_timer_units_and_enable(tmp_path):
    calls = []
    ok = timer.ensure(
        "/bin/agentihooks", tmp_path, run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0)
    )
    assert ok and (tmp_path / "agentihooks-swarm.timer").exists()
    service = (tmp_path / "agentihooks-swarm.service").read_text()
    assert 'ExecStart="/bin/agentihooks" swarm tick' in service
    assert "Environment=PATH=%h/.local/bin" in service and "EnvironmentFile=-%h/.agentihooks/.env" in service
    assert "KillMode=process" in service
    assert calls[-1] == ["systemctl", "--user", "enable", "--now", "agentihooks-swarm.timer"]


def test_timer_ensure_also_runs_the_inbox_waker_service(tmp_path):
    calls = []
    timer.ensure(
        "/bin/agentihooks",
        tmp_path,
        run=lambda argv, **kw: calls.append((argv, kw)) or subprocess.CompletedProcess(argv, 0),
    )
    assert (tmp_path / "agentihooks-inbox-waker.service").read_text() == (
        "[Unit]\nDescription=agentihooks inbox waker for Codex panes\n\n"
        "[Service]\nType=simple\nEnvironment=PYTHONUNBUFFERED=1\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        'ExecStart="/bin/agentihooks" swarm waker\n'
        "Restart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    enable = ["systemctl", "--user", "enable", "--now", "agentihooks-inbox-waker.service"]
    assert (enable, {"capture_output": True, "text": True, "timeout": 30}) in calls


@pytest.fixture
def shared_units(tmp_path, monkeypatch):
    from scripts.targets._common import _install_module

    install = _install_module()
    installed = tmp_path / "installed-agentihooks"
    monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", installed)
    monkeypatch.setattr(install, "install_root", lambda: installed)
    shared = tmp_path / "systemd" / "user"
    monkeypatch.setattr(timer, "UNIT_DIR", shared)
    return install, installed, shared


def test_timer_from_a_scratch_copy_leaves_the_shared_units_alone(shared_units, tmp_path, monkeypatch, capsys):
    install, installed, shared = shared_units
    calls = []
    run = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0)  # noqa: E731
    assert timer.ensure("/installed/bin/agentihooks", run=run)
    before = {path.name: path.read_text() for path in shared.iterdir()}
    calls.clear()

    scratch = tmp_path / "scratch" / "agentihooks"
    monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", scratch)
    assert timer.ensure("/scratch/bin/agentihooks", run=run) is False
    assert timer.ensure("/scratch/bin/agentihooks", shared, run=run) is False

    assert {path.name: path.read_text() for path in shared.iterdir()} == before
    assert 'ExecStart="/installed/bin/agentihooks" swarm tick' in before["agentihooks-swarm.service"]
    assert calls == []
    refusal = (
        f"this run comes from {scratch}, not the installed agentihooks at {installed}, "
        f"so it leaves the shared swarm timer units in {shared} alone\n"
    )
    assert capsys.readouterr().err == refusal * 2

    proof_dir = tmp_path / "proof-units"
    assert timer.ensure("/scratch/bin/agentihooks", proof_dir, run=run)
    assert 'ExecStart="/scratch/bin/agentihooks" swarm tick' in (proof_dir / "agentihooks-swarm.service").read_text()


def test_timer_from_the_installed_agentihooks_writes_the_shared_units(shared_units):
    _, _, shared = shared_units
    calls = []
    assert timer.ensure(
        "/installed/bin/agentihooks", run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0)
    )
    assert (
        'ExecStart="/installed/bin/agentihooks" swarm waker' in (shared / "agentihooks-inbox-waker.service").read_text()
    )
    assert calls[-1] == ["systemctl", "--user", "enable", "--now", "agentihooks-swarm.timer"]


def test_main_runs_the_inbox_waker_and_renames_without_a_slug(monkeypatch):
    ran = []
    monkeypatch.setattr(cli, "connect", lambda: "store")
    for name in ("waker", "rename"):
        monkeypatch.setattr(cli, f"cmd_{name}", lambda store, args, name=name: ran.append((name, store, vars(args))))
        assert cli.main([name]) == 0
    assert ran == [("waker", "store", {}), ("rename", "store", {})]


def test_the_waker_command_runs_the_waker_with_the_swarm_clock(monkeypatch):
    from scripts.inbox import waker

    ran = []
    monkeypatch.setattr(waker, "run", lambda store, herdr, clock: ran.append((store, type(herdr), clock)))
    cli.cmd_waker("store", None)
    assert ran == [("store", cli.delivery.HerdrMessenger, cli.now_ms)]


def test_runtime_spawns_through_init_agent_with_a_private_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.swarm.runtime.time.time", lambda: 7.0)
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=validated(argv, "pane_id=w3:p1\nstatus=started\nroute_status=direct\naccount=acct\nagent=codex\n"),
            stderr="",
        )

    rt = runtime.HerdrRuntime(home=tmp_path, run=fake_run, choose=lambda r, e: ("codex", "rotation"))
    config = cli.SwarmConfig("sw", "/repo", 1, 1)
    placed = rt.spawn(config, "ci", "ci@a1b2c3-0001", {"id": "t2", "title": "speed up the tests"})
    prompt_path = tmp_path / "sw" / "prompts" / "ci@a1b2c3-0001.md"
    decision = {
        "profile": "cicd",
        "source": "lane",
        "responsibility": "ci lane",
        "model": "",
        "confidence": None,
        "calibrated": None,
        "anchors": [],
        "overlays": [],
        "bundle_revision": "",
    }
    assert placed == runtime.Placed(
        "w3:p1",
        "codex",
        "acct",
        profile="cicd",
        model_source="lane-default",
        profile_decision={**decision, "validation": placed.profile_decision["validation"]},
        choice="rotation",
        launched_at=7_000,
        launch_timings=placed.launch_timings,
    )
    assert (placed.launch_timings["launched_at"], placed.launch_timings["returned_at"]) == (7_000, 7_000)
    assert placed.profile_decision["validation"]["state"] == "validated"
    assert seen[0][1:4] == ["init-agent", "--host", "herdr"]
    assert seen[0][seen[0].index("--name") + 1 : seen[0].index("--name") + 4] == ["ci@a1b2c3-0001", "--agent", "codex"]
    assert oct(prompt_path.stat().st_mode)[-3:] == "600"
    text = prompt_path.read_text()
    assert "speed up the tests" in text and "CI speed" in text and "agentihooks swarm sw done --pr" in text


def test_runtime_spawn_failure_names_the_reason(tmp_path):
    def fail(argv, **kw):
        return subprocess.CompletedProcess(argv, 3, stdout="status=failed\n", stderr="herdr: no server\n")

    rt = runtime.HerdrRuntime(home=tmp_path, run=fail, choose=lambda r, e: ("claude", "priority"))
    with pytest.raises(runtime.SpawnError, match="no server"):
        rt.spawn(cli.SwarmConfig("sw", "/repo", 1, 1), "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})


def test_agent_record_round_trips_through_status_json(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0009", "eng", "t1"))
    run("sw", "status", "--json")
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["agents"][0]["name"] == "engineer@a1b2c3-0009"


def test_agentihooks_dispatches_swarm(monkeypatch):
    import scripts.install as install

    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr("sys.argv", ["agentihooks", "swarm", "list"])
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert (exc.value.code, seen) == (0, [["list"]])


def test_runtime_refuses_to_spawn_when_every_agent_is_full(tmp_path):
    from scripts import agent_choice

    calls = []
    rt = runtime.HerdrRuntime(
        home=tmp_path, run=lambda argv, **kw: calls.append(argv), choose=lambda r, e: ("claude", agent_choice.ALL_FULL)
    )
    with pytest.raises(runtime.SpawnError, match="session cap"):
        rt.spawn(cli.SwarmConfig("sw", "/repo", 1, 1), "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert calls == []


def test_runtime_treats_a_failed_route_as_a_failed_spawn_and_cleans_up(tmp_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv[1])
        out = "pane_id=w3:p1\nstatus=started\nroute_status=failed\n" if argv[1] == "init-agent" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    rt = runtime.HerdrRuntime(home=tmp_path, run=fake_run, choose=lambda r, e: ("claude", "priority"))
    with pytest.raises(runtime.SpawnError):
        rt.spawn(cli.SwarmConfig("sw", "/repo", 1, 1), "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert seen == ["init-agent", "terminate-agent"]


def test_runtime_retire_reports_a_refused_end_and_closes_no_pane(tmp_path):
    from scripts.swarm.reaper import Outcome

    closed = []
    rt = runtime.HerdrRuntime(home=tmp_path, herdr=lambda args: closed.append(args) or {})
    rt.end = lambda name, pid, homes: Outcome((), 4242, "survived SIGKILL: 4242")
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id="w3:p1")
    assert rt.retire(agent) is False and closed == []
    assert rt.refusal(agent) == {"process": 4242, "refusal": "survived SIGKILL: 4242"}
    rt.end = lambda name, pid, homes: Outcome()
    assert rt.retire(agent) is True and closed == [["pane", "close", "w3:p1"]]
    assert rt.refusal(agent) == {"process": 0, "refusal": "unknown"}


def test_runtime_retire_ends_the_recorded_launch_process_never_the_name(tmp_path):
    from scripts.swarm.reaper import Outcome

    ended = []
    rt = runtime.HerdrRuntime(
        home=tmp_path, run=lambda argv, **kw: pytest.fail("retire never runs terminate-agent"), herdr=lambda a: {}
    )
    rt.end = lambda name, pid, homes: ended.append((name, pid, homes)) or Outcome((pid,))
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", profile_decision={"validation": {"pid": 321}})
    assert rt.retire(agent, homes=[tmp_path])
    assert ended == [("engineer@a1b2c3-0001", 321, [tmp_path])]


def test_runtime_reports_a_pane_that_will_not_close(tmp_path):
    from scripts.swarm.reaper import Outcome

    def herdr(args):
        raise RuntimeError("herdr socket gone")

    rt = runtime.HerdrRuntime(home=tmp_path, herdr=herdr)
    rt.end = lambda name, pid, homes: Outcome()
    agent = AgentRecord(
        "engineer@a1b2c3-0001", "eng", "t1", pane_id="w3:p1", profile_decision={"validation": {"pid": 9}}
    )
    assert rt.retire(agent) is False
    assert rt.refusal(agent) == {"process": 9, "refusal": "pane w3:p1: herdr socket gone"}
    unbound = AgentRecord("engineer@a1b2c3-0002", "eng", "t2", pane_id="w3:p2")
    assert rt.retire(unbound) is False
    assert rt.refusal(unbound) == {"process": 0, "refusal": "pane w3:p2: herdr socket gone"}


def test_runtime_reaps_every_session_holding_a_stray_name(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts import terminate_agent
    from scripts.swarm.reaper import Outcome

    held = [SimpleNamespace(name=n, process=SimpleNamespace(pid=p)) for n, p in (("x@a", 5), ("y@b", 6), ("x@a", 7))]
    monkeypatch.setattr(terminate_agent, "sessions", lambda: held)
    reaped = []
    rt = runtime.HerdrRuntime(home=tmp_path)
    rt.reap = lambda pids: reaped.append(pids) or Outcome(tuple(pids))
    assert rt.reap_name("x@a") is True and reaped == [[5, 7]]
    rt.reap = lambda pids: Outcome((), 5, "survived SIGKILL: 5")
    assert rt.reap_name("x@a") is False


HANDOFF = """# Handoff v2
## Intent
{intent}
## Done
- Seam one green, `pytest -k seam_one` 3 passed
## Stopped at
Seam two test red for the expected reason.
## Decisions and promises
None
## Next
1. Make seam two green; done when its test passes.
## Read first
- ledger:sw/tasks/t1 the task and its contract
<!-- handoff complete -->
"""


def _handoff_doc(tmp_path, intent="Finish the parser task for the swarm."):
    doc = tmp_path / "handoff.md"
    doc.write_text(HANDOFF.format(intent=intent))
    return doc


def test_handoff_finishes_the_agent_keeps_the_claim_and_stores_the_doc(env, tmp_path, capsys):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc = _handoff_doc(tmp_path)
    capsys.readouterr()
    assert run("sw", "--as", "engineer@a1b2c3-0001", "handoff", str(doc)) == 0
    assert [a.state for a in store.agents("sw") if a.name == "engineer@a1b2c3-0001"] == ["finished"]
    assert store.claimant("sw", "t1") == "engineer@a1b2c3-0001"
    assert store.handoff("sw", "t1") == doc.read_text()
    assert store.handoff_seat("sw", "t1") == "eng-1@sw"
    assert ledger.rows["t1"]["state"] == "claimed"
    out = json.loads(capsys.readouterr().out)
    assert "stop now" in out["next"]
    assert out["envelope"] == store.handoff_envelope("sw", "t1")
    assert out["envelope"]["seat"] == "eng-1@sw" and out["envelope"]["reason"] == "recycle"
    assert out["envelope"]["claims"] == ["t1"]


def test_handoff_records_the_reason_given(env, tmp_path, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc = _handoff_doc(tmp_path)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "handoff", str(doc), "--reason", "quota") == 0
    assert store.handoff_envelope("sw", "t1")["reason"] == "quota"


def test_handoff_refuses_a_malformed_document_and_stores_nothing(env, tmp_path, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc = _handoff_doc(tmp_path, intent="Fix the bug in hooks/context/context_recycle.py at line 140.")
    doc.write_text(doc.read_text().replace("- ledger:sw/tasks/t1", "- ledger:sw/tasks/t9"))
    assert run("sw", "--as", "engineer@a1b2c3-0001", "handoff", str(doc)) == 1
    err = capsys.readouterr().err
    assert "handoff refused" in err and "file path" in err and "line number" in err and "does not resolve" in err
    assert store.handoff("sw", "t1") == "" and store.handoff_envelope("sw", "t1") is None
    assert [a.state for a in store.agents("sw") if a.name == "engineer@a1b2c3-0001"] == ["working"]


def test_wait_declares_an_end_time_for_the_calling_agent(env, capsys, monkeypatch):
    from scripts.swarm import idle

    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setattr(cli, "now_ms", lambda: 1_000)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "wait", "15", "--reason", "deploy run") == 0
    assert idle.wait(store.redis, "sw", "engineer@a1b2c3-0001") == {
        "until": 1_000 + 15 * 60_000,
        "reason": "deploy run",
        "at": 1_000,
    }
    assert '"until"' in capsys.readouterr().out
    assert run("sw", "--as", "engineer@a1b2c3-0001", "wait", "0") == 1


def test_handoff_refuses_a_missing_document(env, capsys):
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as", "engineer@a1b2c3-0001", "handoff", "/no/such/doc.md") == 1
    assert "handoff" in capsys.readouterr().err


def test_done_closes_items_left_for_the_agent_and_tells_their_sender(env, monkeypatch):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    inbox = InboxStore(store.redis)
    item = inbox.send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "contract confirmed")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    assert run("sw", "done", "--pr", "https://github.com/o/r/pull/9") == 0
    closed = inbox.get(item.id)
    assert closed.state == "cancelled" and "engineer@a1b2c3-0001 finished its task and exited" in closed.reason
    assert [entry["state"] for entry in inbox.history(item.id)] == ["pending", "cancelled"]
    assert inbox.pending_items("engineer@a1b2c3-0001") == []
    [told] = inbox.pending_items("ci@a1b2c3-0001")
    assert told.sender == "swarm" and item.id in told.text


def test_handoff_moves_items_left_for_the_agent_to_its_seat(env, tmp_path):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    inbox = InboxStore(store.redis)
    item = inbox.send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "contract confirmed")
    doc = _handoff_doc(tmp_path)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "handoff", str(doc)) == 0
    moved = inbox.get(item.id)
    assert (moved.address, moved.state) == ("eng-1@sw", "pending")
    assert inbox.pending_items("engineer@a1b2c3-0001") == []
    assert [i.id for i in inbox.pending_items("eng-1@sw")] == [item.id]
    assert "moved to eng-1@sw" in inbox.history(item.id)[-1]["reason"]


def test_done_also_closes_an_item_delivered_but_never_closed(env, monkeypatch):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    inbox = InboxStore(store.redis)
    seen = inbox.send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "contract confirmed")
    unseen = inbox.send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "schema confirmed")
    inbox.deliver(seen.id, "engineer@a1b2c3-0001")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    assert run("sw", "done", "--pr", "https://github.com/o/r/pull/9") == 0
    assert [inbox.get(i.id).state for i in (seen, unseen)] == ["cancelled", "cancelled"]
    assert len(inbox.pending_items("ci@a1b2c3-0001")) == 2


def test_handoff_puts_an_item_delivered_but_never_closed_back_on_the_seat_pending(env, tmp_path):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    inbox = InboxStore(store.redis)
    seen = inbox.send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "contract confirmed")
    inbox.read(seen.id, "engineer@a1b2c3-0001")
    doc = _handoff_doc(tmp_path)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "handoff", str(doc)) == 0
    moved = inbox.get(seen.id)
    assert (moved.address, moved.state) == ("eng-1@sw", "pending")
    assert [i.id for i in inbox.pending_items("eng-1@sw")] == [seen.id]


def test_exit_notice_for_an_exited_sender_goes_to_the_master_seat(env):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    inbox = InboxStore(store.redis)
    for notice in inbox.pending_items("master@sw"):
        inbox.close(notice.id, "master@a1b2c3-0001", "done", "handled the notice")
    item = inbox.send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "contract confirmed")
    assert run("sw", "--as", "ci@a1b2c3-0001", "done", "--pr", "https://github.com/o/r/pull/8") == 0
    assert run("sw", "--as", "engineer@a1b2c3-0001", "done", "--pr", "https://github.com/o/r/pull/9") == 0
    assert inbox.pending_items("ci@a1b2c3-0001") == []

    [notice] = inbox.pending_items("master@sw")
    assert item.id in notice.text


def test_next_tick_settles_messages_sent_after_done(env):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    inbox = InboxStore(store.redis)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "done", "--pr", "https://github.com/o/r/pull/9") == 0
    cli.run_tick(store, "sw")
    item = inbox.send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "late contract")
    cli.run_tick(store, "sw")
    closed = inbox.get(item.id)
    assert closed.state == "cancelled"
    assert "finished its task and exited" in closed.reason
    [notice] = inbox.pending_items("ci@a1b2c3-0001")
    assert item.id in notice.text


@pytest.mark.parametrize("state", ["delivered", "read"])
def test_block_closes_open_items_once_and_tells_the_sender(env, state):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    inbox = InboxStore(store.redis)
    item = inbox.send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "contract confirmed")
    getattr(inbox, "deliver" if state == "delivered" else "read")(item.id, "engineer@a1b2c3-0001")
    assert run("sw", "--as", "engineer@a1b2c3-0001", "block", "missing dependency") == 0
    closed = inbox.get(item.id)
    assert closed.state == "cancelled"
    assert "blocked its task and exited" in closed.reason
    cli.run_tick(store, "sw")
    assert len(inbox.pending_items("ci@a1b2c3-0001")) == 1


def test_tick_redirects_a_late_handoff_item_to_the_successor(env, tmp_path):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc = _handoff_doc(tmp_path)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "handoff", str(doc)) == 0
    cli.run_tick(store, "sw")
    inbox = InboxStore(store.redis)
    item = inbox.send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "late contract")
    cli.run_tick(store, "sw")
    successor = store.seats.occupant("eng-1@sw").occupant
    assert successor == "engineer@a1b2c3-0002"
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("eng-1@sw", "pending")
    assert inbox.deliver(item.id, successor).state == "delivered"


def test_a_tick_with_a_refused_page_post_still_runs_every_other_pass(env, monkeypatch):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")

    def refuse(slug, text, by=None):
        raise SwarmError("ledger sw refused: chat refused: clock time '18:45'")

    ledger.relay = refuse
    ran = []
    for module, name in ((cli.ledger_events, "event_pass"), (cli.phases, "phase_pass"), (cli.wake, "wake_pass")):
        real = getattr(module, name)
        monkeypatch.setattr(module, name, lambda *a, _n=name, _r=real: ran.append(_n) or _r(*a))
    item = InboxStore(store.redis).send("engineer@a1b2c3-0001", "operator", "the job finished at 18:45")
    cli.run_tick(store, "sw")
    assert ran == ["phase_pass", "event_pass", "wake_pass"]
    assert InboxStore(store.redis).get(item.id).state == "cancelled"


def test_agent_prompt_starts_by_reading_the_ledger_json(monkeypatch):
    from scripts.swarm import prompt

    monkeypatch.delenv("LEDGER_DIR")

    text = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert text.index("~/development-ledger/sw.json") < text.index("Work it end to end")


def test_status_json_gives_every_agent_a_status_and_its_model(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    records = {
        "engineer@a1b2c3-0001": AgentRecord(
            "engineer@a1b2c3-0001", "eng", "t1", state="starting", model="opus", effort="high"
        ),
        "engineer@a1b2c3-0002": AgentRecord("engineer@a1b2c3-0002", "eng", "t1", idle_ticks=1),
        "engineer@a1b2c3-0003": AgentRecord("engineer@a1b2c3-0003", "eng", "t1", idle_ticks=3),
        "engineer@a1b2c3-0004": AgentRecord("engineer@a1b2c3-0004", "eng", "t1", state="finished", idle_ticks=5),
        "engineer@a1b2c3-0005": AgentRecord("engineer@a1b2c3-0005", "eng", "t1"),
    }
    for record in records.values():
        store.put_agent("sw", record)
    run("sw", "status", "--json")
    agents = {a["name"]: a for a in json.loads(capsys.readouterr().out.splitlines()[-1])["agents"]}
    assert {n: a["status"] for n, a in agents.items()} == {
        "engineer@a1b2c3-0001": "working",
        "engineer@a1b2c3-0002": "idle",
        "engineer@a1b2c3-0003": "stalled",
        "engineer@a1b2c3-0004": "finished",
        "engineer@a1b2c3-0005": "working",
    }
    assert (agents["engineer@a1b2c3-0001"]["model"], agents["engineer@a1b2c3-0001"]["effort"]) == ("opus", "high")


def test_status_text_shows_each_agent_model_and_effort_or_unknown(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent(
        "sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1", harness="codex", model="gpt-6.1-sol", effort="high")
    )
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0002", "eng", "t2", harness="claude"))
    run("sw", "status")
    lines = {line.split("\t")[0]: line.split("\t") for line in capsys.readouterr().out.splitlines() if "\t" in line}
    assert "gpt-6.1-sol high" in lines["engineer@a1b2c3-0001"]
    assert "unknown" in lines["engineer@a1b2c3-0002"]


def test_status_text_names_the_promoted_engineer(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    state = {"since": 1, "failure": "master spawn failed: boom", "promoted": "engineer@a1b2c3-0001"}
    store.redis.set(store.key("sw", "master-outage"), json.dumps(state))
    run("sw", "status")
    out = capsys.readouterr().out.splitlines()
    assert "promoted  engineer@a1b2c3-0001  restoring the master: master spawn failed: boom" in out


def test_status_text_logs_a_base_miss_as_enforced_with_a_finding(env, capsys):
    from scripts.swarm import launch_check

    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1")
    launch_check.record(
        store, "sw", agent, {"base": {"expected": "package:engineer", "actual": "engineer"}}, 10, 60_000
    )
    run("sw", "status")
    out = capsys.readouterr().out.splitlines()
    assert "launch  engineer@a1b2c3-0001  failed  60000ms" in out
    assert "  base  enforced  expected package:engineer; observed engineer" in out
    assert (
        "finding  launch check  engineer@a1b2c3-0001/base: engineer@a1b2c3-0001 failed its launch check on base" in out
    )


def test_status_text_logs_enforced_and_passed_launch_checks(env, capsys):
    from scripts.swarm import launch_check

    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1")
    launch_check.record(store, "sw", agent, {"name": {"expected": "a", "actual": "b"}}, 10, 60_000)
    launch_check.record(store, "sw", AgentRecord("engineer@a1b2c3-0002", "eng", "t2"), {}, 10, 2000)
    run("sw", "status")
    out = capsys.readouterr().out.splitlines()
    assert "launch  engineer@a1b2c3-0001  failed  60000ms" in out
    assert "  name  enforced  expected a; observed b" in out
    assert "launch  engineer@a1b2c3-0002  passed  2000ms" in out
    assert (
        "finding  launch check  engineer@a1b2c3-0001/name: engineer@a1b2c3-0001 failed its launch check on name" in out
    )


def test_status_text_lists_each_phase_lifecycle_and_the_tasks_it_holds(env, capsys):
    _, ledger, _ = env
    ledger.rows["t1"]["phase"], ledger.rows["t2"]["phase"] = "p1", "p2"
    ledger.rows["t3"] = {**ledger.rows["t2"], "id": "t3"}
    ledger.phases = [{"id": "p1"}, {"id": "p2", "depends_on": ["p1"]}]
    run("sw", "create", "--repo", "/repo")
    capsys.readouterr()
    run("sw", "status")
    lines = capsys.readouterr().out.splitlines()
    assert "phase p1  building" in lines and "phase p2  waiting  holds t2, t3" in lines


def test_agent_prompt_joins_the_ledger_reads_its_inbox_and_leaves_before_done():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "phase": "p1"})
    led = "agentihooks ledger --slug sw --as engineer@a1b2c3-0001"
    assert f"{led} join" in text
    assert "ledger watch" not in text and "keep a Monitor" not in text
    assert "inbox" in text
    assert f"{led} ack" in text
    assert f"{led} comment phases/p1" in text
    assert f"{led} followup add" in text
    assert text.index(f"{led} join") < text.index("Work it end to end")
    assert text.index(f"{led} leave") < text.index("agentihooks swarm sw done --pr")


def test_agent_prompt_waits_on_checks_through_the_swarm():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "phase": "p1"})
    merge = next(line for line in text.splitlines() if line.startswith("6. "))
    assert merge == (
        "6. Wait on the checks with agentihooks swarm sw wait --on checks <pr url>: the tick ends the wait and tells "
        "you when they resolve, so no Monitor is needed. Queue on green checks with agentihooks swarm sw merge "
        "queue <pr url>, then agentihooks swarm sw wait --on merge <pr url>. Report queue state with "
        "agentihooks swarm sw merge state <pr url>. Before fixing a queued pull request, dequeue first with "
        "agentihooks swarm sw merge dequeue <pr url>, then push, then queue again with agentihooks swarm sw "
        "merge queue <pr url> once checks pass. Keep the worktree until the tick confirms merged; "
        "a red merge wait means fix the pull request and queue it again. After merged, run wt.sh done."
    )


def test_master_prompt_needs_no_ledger_watch():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "master", "master@a1b2c3-0002", {"id": "master"})
    assert "ledger watch" not in text and "keep a Monitor" not in text


def test_assist_asks_for_merge_approval_on_the_task_and_waits_through_the_swarm():
    from scripts.swarm import prompt

    task = {"id": "t1", "title": "x", "phase": "p1"}
    text = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0001", task, autonomy="assist")
    push = [line for line in text.splitlines() if line.startswith("5. ")]
    assert push == [
        "5. Push, open the pull request into dev (with Closes #<n> when there is an issue), record it: "
        "agentihooks swarm sw pr <pr url>"
    ]
    ask = next(line for line in text.splitlines() if line.startswith("6. "))
    assert ask == (
        "6. This swarm runs at assist autonomy. Once checks are green, ask the operator to approve the merge: "
        'agentihooks ledger --slug sw --as engineer@a1b2c3-0001 comment tasks/t1 "<plain words: what the pull '
        'request does, checks green, waiting for your approval to merge>", then agentihooks swarm sw wait 60 '
        '--reason "operator merge approval"; his answer reaches you as an inbox message. Queue with agentihooks '
        "swarm sw merge queue <pr url> only after an OPERATOR line on the ledger approves it, then agentihooks "
        "swarm sw wait --on merge <pr url>. Report queue state with agentihooks swarm sw merge state <pr url>. "
        "Before fixing a queued pull request, dequeue first with agentihooks swarm sw merge dequeue <pr url>, "
        "then push, then queue again with agentihooks swarm sw merge queue <pr url> once checks pass and "
        "approval still holds. Keep the worktree until the tick confirms merged; a red merge wait means fix the pull request and "
        "queue it again. After merged, run wt.sh done. An OPERATOR line asking for changes: make them "
        "and ask again."
    )
    assert "ledger watch" not in text and "keep a Monitor" not in text


def test_agent_prompt_runs_gates_and_review_before_the_merge_and_ends_with_leave_then_done():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "phase": "p1"})
    steps = [line for line in text.splitlines() if line[:1].isdigit() and line[1:3] == ". "]
    review = next(i for i, s in enumerate(steps) if "Gates green" in s and "review per the dev-cycle skill" in s)
    merge = next(i for i, s in enumerate(steps) if "Queue on green checks" in s)
    assert review < merge
    assert "Standards and Spec" in steps[review] and "three rounds" in steps[review]
    last = steps[-1]
    assert last.index("agentihooks ledger --slug sw --as engineer@a1b2c3-0001 leave") < last.index(
        "agentihooks swarm sw done --pr"
    )


def test_set_compact_limit_stores_it_on_the_swarm(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "set", "compact-limit=40") == 0
    assert store.config("sw").compact_limit == 40
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["compact_limit"] == 40


def test_the_master_takes_no_task_commands(env, capsys):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("master@a1b2c3-0001", "master", "master"))
    for argv in (("issue", "https://x/issues/1"), ("pr", "https://x/pull/1"), ("branch",), ("done",), ("block", "why")):
        assert run("sw", "--as", "master@a1b2c3-0001", *argv) == 1
        assert "master works no task" in capsys.readouterr().err
    assert ledger.comments == [] and [a.state for a in store.agents("sw")] == ["working"]


def git_answers(answers):
    calls = []

    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        code, out = answers[argv[1]]
        return subprocess.CompletedProcess(argv, code, out, "")

    return calls, fake


GIT_OPTS = {"capture_output": True, "text": True, "timeout": 20}
URL3 = "https://github.com/o/r/pull/3"


def test_the_worktree_branch_is_read_and_checked_on_origin():
    calls, fake = git_answers({"branch": (0, "engineer-a1b2c3-0001\n"), "ls-remote": (0, "abc\trefs/heads/x\n")})
    assert cli.worktree_branch(run=fake) == "engineer-a1b2c3-0001"
    assert calls == [
        (["git", "branch", "--show-current"], GIT_OPTS),
        (["git", "ls-remote", "--exit-code", "--heads", "origin", "engineer-a1b2c3-0001"], GIT_OPTS),
    ]


def test_a_branch_missing_on_origin_is_refused():
    _, fake = git_answers({"branch": (0, "engineer-a1b2c3-0001\n"), "ls-remote": (2, "")})
    with pytest.raises(SwarmError) as refused:
        cli.worktree_branch(run=fake)
    assert str(refused.value) == (
        "branch engineer-a1b2c3-0001 is not on origin; push it first with git push -u origin engineer-a1b2c3-0001"
    )


@pytest.mark.parametrize("answer", [(0, "\n"), (128, "engineer-a1b2c3-0001\n")])
def test_a_checkout_on_no_branch_is_refused(answer):
    calls, fake = git_answers({"branch": answer})
    with pytest.raises(SwarmError) as refused:
        cli.worktree_branch(run=fake)
    assert str(refused.value) == "swarm branch runs in a worktree on a branch; this checkout is on none"
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["git"], 20), FileNotFoundError("git")])
@pytest.mark.parametrize("step", ["branch", "ls-remote"])
def test_a_git_call_that_cannot_run_is_a_clean_refusal(failure, step):
    def fake(argv, **kwargs):
        if argv[1] == step:
            raise failure
        return subprocess.CompletedProcess(argv, 0, "engineer-a1b2c3-0001\n", "")

    with pytest.raises(SwarmError) as refused:
        cli.worktree_branch(run=fake)
    assert str(refused.value) == f"git {step} could not run: {failure}"


def test_the_pull_request_head_branch_is_read_from_github():
    calls = []

    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{"headRefName": "engineer-a1b2c3-0001"}', "")

    assert cli.pull_branch("https://github.com/o/r/pull/3", run=fake) == "engineer-a1b2c3-0001"
    assert calls == [(["gh", "pr", "view", "https://github.com/o/r/pull/3", "--json", "headRefName"], GIT_OPTS)]


@pytest.mark.parametrize("answer", [(1, '{"headRefName": "x"}'), (0, "not json"), (0, "{}")])
def test_an_unreadable_pull_request_gives_no_branch(answer):
    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, answer[0], answer[1], "")

    assert cli.pull_branch("https://github.com/o/r/pull/3", run=fake) == ""


def test_a_failing_github_call_gives_no_branch():
    def fake(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 20)

    assert cli.pull_branch("https://github.com/o/r/pull/3", run=fake) == ""


def test_swarm_branch_records_the_worktree_branch_on_the_agent_task(env, monkeypatch, capsys):
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setattr(cli, "worktree_branch", lambda: "engineer-a1b2c3-0001")
    writes, update = [], ledger.update_task
    ledger.update_task = lambda slug, task, fields, by="swarm": (
        writes.append((task, fields, by)) or update(slug, task, fields)
    )
    capsys.readouterr()
    assert run("sw", "--as", "engineer@a1b2c3-0001", "branch") == 0
    assert writes == [("t1", {"branch": "engineer-a1b2c3-0001"}, "engineer@a1b2c3-0001")]
    assert ledger.rows["t1"]["branch"] == "engineer-a1b2c3-0001"
    assert json.loads(capsys.readouterr().out) == {"task": "t1", "branch": "engineer-a1b2c3-0001"}


def test_swarm_branch_writes_nothing_when_the_branch_is_refused(env, monkeypatch, capsys):
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")

    def refused():
        raise SwarmError("branch x is not on origin")

    monkeypatch.setattr(cli, "worktree_branch", refused)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "branch") == 1
    assert "branch x is not on origin" in capsys.readouterr().err and "branch" not in ledger.rows["t1"]


@pytest.mark.parametrize(("head", "fields"), [("engineer-a1b2c3-0001", {"branch": "engineer-a1b2c3-0001"}), ("", {})])
def test_swarm_pr_records_the_pull_request_head_branch(env, monkeypatch, capsys, head, fields):
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setattr(cli, "pull_branch", lambda url: head if url == URL3 else "wrong")
    capsys.readouterr()
    assert run("sw", "--as", "engineer@a1b2c3-0001", "pr", URL3) == 0
    assert ledger.rows["t1"].get("branch") == fields.get("branch")
    out = json.loads(capsys.readouterr().out)
    assert {key: out[key] for key in out if key != "intent"} == {"task": "t1", "pr_url": URL3, **fields}


def test_a_master_handoff_stores_the_doc_for_its_successor(env, tmp_path):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("master@a1b2c3-0001", "master", "master", seat="master@sw"))
    doc = _handoff_doc(tmp_path, intent="Run the swarm; the operator asked for a docs task.")
    assert run("sw", "--as", "master@a1b2c3-0001", "handoff", str(doc)) == 0
    assert store.handoff("sw", "master") == doc.read_text()
    assert store.handoff_envelope("sw", "master")["worktree"] == "/repo"
    assert [a.state for a in store.agents("sw")] == ["finished"]


def test_master_prompt_runs_the_swarm_and_never_codes():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "master", "master@a1b2c3-0002", {"id": "master", "handoff": "caps go to four"})
    led = "agentihooks ledger --slug sw --as master@a1b2c3-0002"
    for needle in (
        f"{led} join --role orchestrator",
        f"{led} ack",
        f"{led} task add",
        f"{led} time-left",
        f"{led} followup add",
        "agentihooks swarm sw --as master@a1b2c3-0002 say --to operator",
        "agentihooks msg reply",
        "--fyi",
        "agentihooks swarm sw --as master@a1b2c3-0002 send-message",
        "agentihooks swarm sw --as master@a1b2c3-0002 set max-eng-agents=",
        "agentihooks swarm sw --as master@a1b2c3-0002 pause",
        "agentihooks swarm sw --as master@a1b2c3-0002 stop",
        "agentihooks swarm sw --as master@a1b2c3-0002 handoff",
        "browser_close",
        "caps go to four",
    ):
        assert needle in text, needle
    assert (
        "\nYou troubleshoot with read only diagnostics, plan with the operator and configure the swarm, the ledger "
        "and the operator's environment with him through the agentihooks commands and tools. You never edit code "
        "or config files in a repository, commit, merge or claim a task: engineers do that.\n"
    ) in text
    assert "wt.sh new" not in text and "done --pr" not in text


def test_the_tick_wakes_an_idle_pane_holding_a_pending_inbox_item(env):
    from scripts.inbox.store import InboxStore
    from scripts.inbox.wake import WAKE_TEXT

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id="p1"))
    rt.live.add("engineer@a1b2c3-0001")
    item = InboxStore(store.redis).send("operator", "engineer@a1b2c3-0001", "look at the failing check")
    herdr = FakeHerdr({"p1": "idle"})
    actions = cli.run_tick(store, "sw", ledger, rt, herdr)
    assert herdr.prompts == [("p1", WAKE_TEXT)]
    assert f"woke engineer@a1b2c3-0001 for message {item.id}" in actions


def test_the_tick_turns_an_engineer_follow_up_into_a_master_inbox_item(env):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("master@a1b2c3-0001", "master", "master", pane_id="m1", seat="master@sw"))
    rt.live.add("master@a1b2c3-0001")
    ledger.log = []
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({"m1": "working"}))
    added = {
        "rev": 1,
        "at": 1,
        "by": "engineer@a1b2c3-0001",
        "kind": "added",
        "target": "followups/f1",
        "text": "cap retries",
    }
    ledger.log = [added]
    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({"m1": "working"}))
    [item] = InboxStore(store.redis).inbox("master@sw")
    assert (item.sender, item.state) == ("swarm", "pending") and "cap retries" in item.text
    assert any("followups/f1" in action for action in actions)


def test_status_carries_health_findings_for_the_master_to_read(env, capsys, monkeypatch, tmp_path):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1", idle_ticks=4))
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0001", title="Fold the chat panel")
    monkeypatch.setattr(cli.activity, "default_root", lambda: tmp_path)
    run("sw", "status", "--json")
    found = json.loads(capsys.readouterr().out.splitlines()[-1])["findings"]
    assert [(f["kind"], f["subject"], f["evidence"]) for f in found] == [
        ("idle with claim", "engineer@a1b2c3-0001", ["task Fold the chat panel (claimed)"])
    ]
    run("sw", "status")
    assert capsys.readouterr().out.splitlines()[-4:] == [
        "finding  idle with claim  engineer@a1b2c3-0001: idle for 4 ticks while holding a task",
        "  - task Fold the chat panel (claimed)",
        "  threshold 3 idle ticks",
        "  id idle-with-claim/engineer@a1b2c3-0001",
    ]


def test_status_prints_each_evidence_entry_on_its_own_line(env, capsys, monkeypatch):
    run("sw", "create", "--repo", "/repo")
    entries = ("Split the parser, gain 4", "Cache the index, gain 1.5", "q2, no gain stated")
    finding = cli.health.Finding(
        "scope inflation", "engineer@a1b2c3-0001", "queued 3 tasks for its own lane", entries, "3 tasks"
    )
    monkeypatch.setattr(cli.health, "findings", lambda *a: [finding])
    run("sw", "status")
    assert capsys.readouterr().out.splitlines()[-6:] == [
        "finding  scope inflation  engineer@a1b2c3-0001: queued 3 tasks for its own lane",
        "  - Split the parser, gain 4",
        "  - Cache the index, gain 1.5",
        "  - q2, no gain stated",
        "  threshold 3 tasks",
        "  id scope-inflation/engineer@a1b2c3-0001",
    ]


def test_a_page_line_to_the_master_becomes_an_item_and_the_reply_closes_the_loop(env):
    from scripts.swarm import operator_mail
    from scripts.swarm_ledger.watch_ledger import line

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("master@a1b2c3-0001", "master", "master", pane_id="m1", seat="master@sw"))
    store.seats.occupy("master@sw", "master@a1b2c3-0001", 1)
    rt.live.add("master@a1b2c3-0001")
    box = InboxStore(store.redis)
    said = {
        "rev": 7,
        "at": 60,
        "by": "operator",
        "kind": "message added",
        "target": "chat",
        "id": "m",
        "text": "how far",
    }
    operator_mail.relay(box, store, "sw", {}, [said], line)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({"m1": "working"}))
    [item] = box.inbox("master@sw")
    assert (item.sender, item.state) == ("operator", "pending") and "how far" in item.text
    answer = box.reply(item.id, "master@a1b2c3-0001", "two tasks left")
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({"m1": "working"}))
    assert ("two tasks left", "master@a1b2c3-0001") in ledger.said
    assert box.get(item.id).state == "done" and box.get(answer.id).state == "done"


def test_remove_clears_the_swarm_and_its_counts_so_a_recreated_swarm_starts_at_zero(env, monkeypatch, tmp_path):
    store, _, _ = env
    monkeypatch.setattr(cli.activity, "default_root", lambda: tmp_path)
    run("sw", "create", "--repo", "/repo")
    run("other", "create", "--repo", "/repo")
    for slug in ("sw", "other"):
        cli.activity.record("Monitor", {}, {"AGENTIHOOKS_SWARM": slug, "AGENTIHOOKS_AGENT_NAME": f"{slug}-eng-1"})
        store.next_name(slug, "eng")
    assert run("missing", "remove") == 1
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1"))
    assert run("sw", "remove") == 1
    assert store.slugs() == ["other", "sw"]
    store.drop_agent("sw", "engineer@a1b2c3-0001")
    assert run("sw", "remove") == 0
    assert store.slugs() == ["other"]
    assert cli.activity.counts("sw") == {}
    assert cli.activity.counts("other") == {"other-eng-1": {"watch": 1, "act": 0, "since": 1}}
    run("sw", "create", "--repo", "/repo")
    assert cli.activity.counts("sw") == {}
    assert store.next_name("sw", "eng") == "engineer@a1b2c5-0001"
    assert store.next_name("other", "eng") == "engineer@a1b2c4-0002"


@pytest.mark.parametrize("slug", ["", "*", "s?", "[sw]"])
def test_remove_refuses_an_empty_or_glob_name_and_every_swarm_survives(env, monkeypatch, tmp_path, slug):
    store, _, _ = env
    monkeypatch.setattr(cli.activity, "default_root", lambda: tmp_path)
    run("sw", "create", "--repo", "/repo")
    cli.activity.record("Monitor", {}, {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "sw-eng-1"})
    store.redis.hset(store.key(slug, "config"), mapping=store.redis.hgetall(store.key("sw", "config")))
    keys = set(store.redis.keys("*"))
    with pytest.raises(SwarmError, match="refusing to remove swarm .*: an empty or pattern name"):
        store.remove(slug)
    with pytest.raises(SwarmError, match="no swarm Xsw"):
        store.remove("Xsw")
    assert run(slug, "remove") == 1
    assert set(store.redis.keys("*")) == keys
    assert store.slugs() == ["sw"]
    assert cli.activity.counts("sw") == {"sw-eng-1": {"watch": 1, "act": 0, "since": 1}}


def _idle_finding(env, monkeypatch, tmp_path):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("master@a1b2c3-0001", "master", "master"))
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1", idle_ticks=4))
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0001", title="Fold the chat panel")
    monkeypatch.setattr(cli.activity, "default_root", lambda: tmp_path)


def _findings(capsys):
    run("sw", "status", "--json")
    return json.loads(capsys.readouterr().out.splitlines()[-1])["findings"]


def test_a_master_verdict_hides_the_finding_from_status(env, capsys, monkeypatch, tmp_path):
    _idle_finding(env, monkeypatch, tmp_path)
    [found] = _findings(capsys)
    assert (found["id"], found["verdict"]) == ("idle-with-claim/engineer@a1b2c3-0001", None)
    assert run("sw", "--as", "master@a1b2c3-0001", "verdict", found["id"], "false-positive", "--note", "on checks") == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "false-positive"
    assert _findings(capsys) == []


def test_the_operator_may_give_a_verdict_and_a_worker_may_not(env, capsys, monkeypatch, tmp_path):
    _idle_finding(env, monkeypatch, tmp_path)
    _findings(capsys)
    assert run("sw", "--as", "engineer@a1b2c3-0001", "verdict", "idle-with-claim/engineer@a1b2c3-0001", "resolved") == 1
    assert "only the master or the operator" in capsys.readouterr().err
    assert run("sw", "--as", "operator", "verdict", "idle-with-claim/engineer@a1b2c3-0001", "resolved") == 0


def test_status_skips_idle_with_claim_while_the_pull_request_waits_on_checks(env, capsys, monkeypatch, tmp_path):
    store, ledger, _ = env
    _idle_finding(env, monkeypatch, tmp_path)
    ledger.rows["t1"].update(state="pr", pr_url="https://github.com/o/r/pull/7")
    monkeypatch.setattr(checks, "pending", lambda url, run=None, approval=False: url.endswith("/7"))
    assert _findings(capsys) == []


def test_status_skips_idle_only_when_green_checks_wait_for_operator_approval(env, capsys, monkeypatch, tmp_path):
    from functools import partial

    from tests.swarm.test_health_checks import PASSED, runner

    _, ledger, _ = env
    _idle_finding(env, monkeypatch, tmp_path)
    ledger.rows["t1"].update(state="pr", pr_url="https://github.com/o/r/pull/7")
    monkeypatch.setattr(checks, "cached", partial(checks.cached, run=runner(PASSED)))
    for autonomy, expected in [
        ("assist", []),
        ("delegate", ["idle with claim"]),
        ("full", ["idle with claim"]),
        ("assist", []),
    ]:
        run("sw", "set", f"autonomy={autonomy}")
        assert [f["kind"] for f in _findings(capsys)] == expected
    run("sw", "set", "autonomy=manual")
    ledger.rows["t1"].update(state="blocked")
    assert _findings(capsys) == []


@pytest.mark.parametrize(
    "out, code", [("lint\tfail\t8s\thttps://x/1\n", 1), ("", 0), ("lint\tpass\t8s\thttps://x/1\n", 1)]
)
def test_assist_still_reports_idle_when_checks_are_failed_or_unknown(env, capsys, monkeypatch, tmp_path, out, code):
    from functools import partial

    from tests.swarm.test_health_checks import runner

    _, ledger, _ = env
    _idle_finding(env, monkeypatch, tmp_path)
    run("sw", "set", "autonomy=assist")
    ledger.rows["t1"].update(state="pr", pr_url="https://github.com/o/r/pull/7")
    monkeypatch.setattr(checks, "cached", partial(checks.cached, run=runner(out, code)))
    assert [f["kind"] for f in _findings(capsys)] == ["idle with claim"]


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_create_from_a_template_sets_the_caps_and_the_lane_map(env, home):
    store, _, _ = env
    assert run("sw", "create", "--repo", "/repo", "--template", "codex-ci") == 0
    config = store.config("sw")
    assert (config.template, config.max_eng, config.max_ci) == ("codex-ci", 2, 1)
    assert (config.lanes["eng"]["agent"], config.lanes["ci"]["agent"]) == ("claude", "codex")


def test_a_cap_flag_wins_over_the_template(env, home):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo", "--template", "codex-ci", "--max-eng-agents", "5")
    assert (store.config("sw").max_eng, store.config("sw").max_ci) == (5, 1)


def test_create_with_an_unknown_template_is_refused(env, home):
    store, _, _ = env
    assert run("sw", "create", "--repo", "/repo", "--template", "nope") == 1
    assert store.slugs() == []


def test_set_changes_one_lane_field_and_refuses_a_bad_one(env, home):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "eng-model=sonnet", "eng-agent=claude") == 0
    eng = store.config("sw").lanes["eng"]
    assert (eng["model"], eng["agent"], eng["effort"]) == ("sonnet", "claude", "auto")
    assert run("sw", "set", "eng-agent=gemini") == 1
    assert run("sw", "set", "qa-agent=codex") == 1
    assert store.config("sw").lanes["eng"]["agent"] == "claude"


def test_save_then_create_round_trips(env, home):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo", "--template", "codex-ci", "--max-eng-agents", "3")
    run("sw", "set", "eng-model=sonnet", "eng-role=a reviewer", "compact-limit=300")
    assert run("sw", "save-template", "mine") == 0
    assert (home / "swarm-templates" / "mine.json").is_file()
    assert run("sw2", "create", "--repo", "/repo", "--template", "mine") == 0
    a, b = store.config("sw"), store.config("sw2")
    assert (b.template, b.max_eng, b.max_ci, b.compact_limit) == ("mine", 3, 1, 300)
    assert b.lanes == a.lanes and b.lanes["eng"]["role"] == "a reviewer"


def test_templates_lists_built_in_and_user_templates(env, home, capsys):
    run("sw", "create", "--repo", "/repo")
    run("sw", "save-template", "mine")
    capsys.readouterr()
    assert run("templates") == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("default\tbuilt-in") for line in lines)
    assert any(line.startswith("mine\tuser") for line in lines)


def test_create_and_save_template_carry_the_template_links(env, home):
    store, _, _ = env
    link = {"from": "ci", "to": "eng", "kind": "can-observe"}
    (home / "swarm-templates").mkdir(parents=True)
    (home / "swarm-templates" / "linked.json").write_text(json.dumps({"name": "linked", "links": [link]}))
    assert run("sw", "create", "--repo", "/repo", "--template", "linked") == 0
    assert store.config("sw").links == [link]
    assert run("sw", "save-template", "copy") == 0
    assert json.loads((home / "swarm-templates" / "copy.json").read_text())["links"] == [link]


def test_say_refused_by_a_link_posts_nothing_and_names_why(env, capsys):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", links=[{"from": "ci", "to": "eng", "kind": "can-observe"}])
    run("sw", "start")
    ledger.said.clear()
    assert run("sw", "--as", "ci@a1b2c3-0001", "say", "take my task", "--to", "eng") == 1

    assert "can only observe" in capsys.readouterr().err
    assert ledger.said == [] and InboxStore(store.redis).inbox("engineer@a1b2c3-0001") == []


def test_status_shows_each_agent_conversation_id_or_a_dash(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id="w1:p1", conversation_id="5c90d80c"))
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0002", "eng", "t2", pane_id="w1:p2"))
    run("sw", "status")
    lines = {line.split("\t")[0]: line.split("\t") for line in capsys.readouterr().out.splitlines() if "\t" in line}
    assert lines["engineer@a1b2c3-0001"][-3] == "5c90d80c" and lines["engineer@a1b2c3-0002"][-3] == "-"
    run("sw", "status", "--json")
    agents = {a["name"]: a for a in json.loads(capsys.readouterr().out.splitlines()[-1])["agents"]}
    assert (agents["engineer@a1b2c3-0001"]["conversation_id"], agents["engineer@a1b2c3-0002"]["conversation_id"]) == (
        "5c90d80c",
        "",
    )


def test_status_shows_each_restored_agent_outcome_with_its_reason(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    resumed = {
        "name": "engineer@a1b2c3-0001",
        "lane": "eng",
        "task": "t1",
        "outcome": "resumed",
        "reason": "own conversation reopened",
    }
    fresh = {"name": "engineer@a1b2c3-0002", "lane": "eng", "task": "t2", "outcome": "fresh", "reason": "worktree gone"}
    store.put_restored("sw", [resumed, fresh])
    run("sw", "status")
    out = capsys.readouterr().out.splitlines()
    assert "restored  engineer@a1b2c3-0001  resumed  own conversation reopened" in out
    assert "restored  engineer@a1b2c3-0002  fresh  worktree gone" in out
    run("sw", "status", "--json")
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["restored"] == [resumed, fresh]


def test_restore_hands_the_runtime_to_restore_and_prints_every_agent_outcome(env, capsys, monkeypatch):
    _, _, rt = env
    seen = {}

    def restore(store, slug, live, source, runtime):
        seen["runtime"] = runtime
        return [Outcome("engineer@a1b2c3-0001", "eng", "t1", "fresh", "no conversation id")]

    run("sw", "create", "--repo", "/repo")
    monkeypatch.setattr(cli.snapshot, "newest", lambda slug: "/snap.json")
    monkeypatch.setattr(cli.snapshot, "restore", restore)
    run("sw", "restore")
    printed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert [(r["name"], r["outcome"], r["reason"]) for r in printed["restored"]] == [
        ("engineer@a1b2c3-0001", "fresh", "no conversation id")
    ]
    assert seen["runtime"] is rt


@pytest.mark.parametrize("command", ["create", "start", "url"])
def test_create_start_and_url_end_with_the_ledger_page_line(env, capsys, monkeypatch, command):
    monkeypatch.setattr(cli.ledger_link, "serving", lambda: str(cli.ledger_link.folder()))
    if command != "create":
        run("sw", "create", "--repo", "/repo")
        capsys.readouterr()
    argv = ["sw", command, "--repo", "/repo"] if command == "create" else ["sw", command]
    assert run(*argv) == 0
    assert capsys.readouterr().out.splitlines()[-1] == cli.ledger_link.page_line("sw")


def test_status_exposes_pending_inbox_state_duration_tick_and_crew_history(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    agent = AgentRecord("engineer@a1b2c3-0009", "eng", "t1")
    store.put_agent("sw", agent)
    inbox = InboxStore(store.redis)
    inbox.send("operator", agent.name, "Review this work")
    store.redis.set(store.key("sw", "last-tick"), "1234")
    run("sw", "status", "--json")
    status = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert status["last_tick"] == 1234
    assert status["agents"][0]["inbox"][0]["text"] == "Review this work"
    assert status["agents"][0]["state_since"] > 0
    store.drop_agent("sw", agent.name, at=5678)
    run("sw", "status", "--json")
    status = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert status["history"][0]["name"] == agent.name
    assert status["history"][0]["ended_at"] == 5678


def _traced(env, monkeypatch, tmp_path, plan, *p_yes):
    from hooks.classifier import Answer, DecisionResult
    from scripts.swarm import trace_plan

    store, ledger, _ = env
    ledger.followups = []
    ledger.followup = lambda slug, text: ledger.followups.append((slug, text))
    ledger.phases = [{"id": "p1", "title": "Build", "description": "Build the doghouse."}]
    ledger.rows["t1"].update(phase="p1", title="Doghouse", description="A house for the dog.")
    asked = {f"piece_{i}": Answer(type="noul", noul=p) for i, p in enumerate(p_yes)}
    asked["size"] = Answer(type="score", score=1.0, confidence=0.9)
    seen = []
    monkeypatch.setattr(trace_plan, "decide", lambda state, q, **k: seen.append(state) or DecisionResult(asked, "m"))
    folder = tmp_path / "_home" / ".agentihooks" / "swarm" / "sw" / "tasks" / "t1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plan.md").write_text(plan)
    return folder, seen


def test_trace_plan_traces_the_callers_task_and_files_cut_pieces(env, capsys, monkeypatch, tmp_path):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    plan = "- walls | doghouse | it shelters the dog\n- a generator | power | it powers a light\n"
    folder, seen = _traced(env, monkeypatch, tmp_path, plan, 0.9, 0.1)
    capsys.readouterr()
    assert run("sw", "--as", "engineer@a1b2c3-0001", "trace-plan") == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["task"], report["verdict"], report["cut"]) == ("t1", "pass", ["a generator"])
    assert seen[0]["task intent"] == "A house for the dog." and seen[0]["phase intent"] == "Build the doghouse."
    assert seen[0]["project intent"] == "Project intent"
    assert ledger.followups == [("sw", "Cut from the plan of task t1: a generator")]
    assert json.loads((folder / "plan-verdict.json").read_text())["verdict"] == "pass"
    assert ledger.rows["t1"]["state"] != "blocked"


def test_trace_plan_blocks_the_task_on_the_second_failed_plan_when_enforced(env, capsys, monkeypatch, tmp_path):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_GATE_TRACE_PLAN", "off")
    store.update("sw", gates={"trace-plan": "enforce"})
    folder, _ = _traced(env, monkeypatch, tmp_path, "- a generator | power | it powers a light\n", 0.1)
    writes = []
    update, comment = ledger.update_task, ledger.comment
    ledger.update_task = lambda slug, task, fields, by="swarm": writes.append((slug, by)) or update(slug, task, fields)
    ledger.comment = lambda slug, task, text, by: writes.append((slug, by)) or comment(slug, task, text, by)
    inbox = InboxStore(store.redis)
    item = inbox.send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "contract confirmed")
    assert run("sw", "--as", "engineer@a1b2c3-0001", "trace-plan") == 0
    assert ledger.rows["t1"]["state"] != "blocked"
    (folder / "plan.md").write_text("- a petrol generator | power | it powers a light\n")
    capsys.readouterr()
    assert run("sw", "--as", "engineer@a1b2c3-0001", "trace-plan") == 0
    assert json.loads(capsys.readouterr().out)["next"] == "stop now; the plan failed twice and the task is blocked"
    assert ledger.rows["t1"]["state"] == "blocked"
    note = "Blocked by the plan trace after 2 failed plans: 1 of 1 pieces are off the task intent, more than half"
    assert ledger.comments[-1] == ("t1", note, "engineer@a1b2c3-0001")
    assert [a.state for a in store.agents("sw") if a.name == "engineer@a1b2c3-0001"] == ["finished"]
    assert writes == [("sw", "engineer@a1b2c3-0001")] * 2
    assert inbox.get(item.id).reason == "cancelled: engineer@a1b2c3-0001 blocked its task and exited before closing it"
    rows = gate_log.recent("sw")
    assert [(r["gate"], r["kind"], r["agent"], r["task"]) for r in rows] == [
        ("trace-plan", "deny", "engineer@a1b2c3-0001", "t1")
    ] * 2


def test_trace_plan_reads_the_ledger_of_its_swarm_and_runs_without_a_task_row(env, capsys, monkeypatch, tmp_path):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    _, seen = _traced(env, monkeypatch, tmp_path, "- walls | doghouse | it shelters the dog\n", 0.9)
    slugs, state = [], ledger.state
    ledger.state = lambda slug: slugs.append(slug) or state(slug)
    del ledger.rows["t1"]
    assert run("sw", "--as", "engineer@a1b2c3-0001", "trace-plan") == 0
    assert slugs == ["sw"]
    assert (seen[0]["task"], seen[0]["phase"], seen[0]["project intent"]) == ("", "", "Project intent")


def test_trace_plan_without_a_plan_names_the_file_and_format(env, capsys, monkeypatch, tmp_path):
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    capsys.readouterr()
    assert run("sw", "--as", "engineer@a1b2c3-0001", "trace-plan") == 1
    assert capsys.readouterr().err.strip() == (
        f"swarm: write the plan first: {tmp_path}/_home/.agentihooks/swarm/sw/tasks/t1/plan.md, "
        "one piece per line: - what | area, area | why"
    )


def _tick_all(monkeypatch, run_tick, slugs):
    import os
    import types

    from scripts import herdr_gc, operator_env

    filled, swept = [], []
    monkeypatch.setattr(timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(operator_env, "fill", lambda environ: filled.append(environ))
    monkeypatch.setattr(cli, "now_ms", lambda: 77)
    monkeypatch.setattr(herdr_gc, "run", lambda environ, now, apply: swept.append((environ, now, apply)) or ["swept"])
    monkeypatch.setattr(cli, "run_tick", run_tick)
    store = types.SimpleNamespace(slugs=lambda: slugs)
    cli.cmd_tick(store, None)
    assert filled == [os.environ]
    assert swept == [(dict(os.environ), 77, True)]
    return store


def test_the_tick_runs_every_swarm_at_the_same_time(monkeypatch, capsys):
    import threading

    both, seen = threading.Barrier(2, timeout=5), []

    def run_tick(store, slug):
        both.wait()
        seen.append((store, slug, threading.current_thread()))
        return [f"ticked {slug}"]

    store = _tick_all(monkeypatch, run_tick, ["a", "b"])
    assert sorted((s, slug) for s, slug, _ in seen) == [(store, "a"), (store, "b")]
    assert threading.main_thread() not in {thread for _, _, thread in seen}
    out = capsys.readouterr().out.splitlines()
    assert sorted(out[:2]) == ["a: ticked a", "b: ticked b"]
    assert out[2:] == ["herdr: swept"]


def test_a_failing_swarm_tick_leaves_the_others_and_the_sweep_running(monkeypatch, capsys):
    def run_tick(store, slug):
        if slug == "a":
            raise ValueError("ledger down")
        return [f"ok {slug}"]

    _tick_all(monkeypatch, run_tick, ["a", "b"])
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["b: ok b", "herdr: swept"]
    assert captured.err == "a: ValueError: ledger down\n"


def test_every_swarm_gets_its_own_thread_however_many_there_are(monkeypatch, capsys):
    import threading

    slugs = [f"s{n}" for n in range(48)]
    together = threading.Barrier(len(slugs), timeout=10)

    def run_tick(store, slug):
        together.wait()
        return ["ticked"]

    _tick_all(monkeypatch, run_tick, slugs)
    assert sorted(capsys.readouterr().out.splitlines()) == sorted(
        [f"{slug}: ticked" for slug in slugs] + ["herdr: swept"]
    )


def test_no_swarms_still_sweeps_herdr(monkeypatch, capsys):
    _tick_all(monkeypatch, lambda store, slug: pytest.fail("no swarm to tick"), [])
    assert capsys.readouterr().out.splitlines() == ["herdr: swept"]


def test_a_quick_swarm_keeps_its_minute_while_a_slow_one_runs(monkeypatch, capsys):
    import threading
    import time

    monkeypatch.setattr(cli, "TICK_SECONDS", 0.5)
    release, ticks, starts = threading.Event(), {"fast": 0, "slow": 0}, []

    def run_tick(store, slug):
        assert store.slugs() == ["slow", "fast"]
        ticks[slug] += 1
        if slug == "slow":
            release.wait(3)
        else:
            starts.append(time.monotonic())
            if ticks["fast"] > 3:
                pytest.fail("extra ticks went on after every first tick ended")
            if ticks["fast"] == 3:
                release.set()
        return [f"tick {ticks[slug]}"]

    _tick_all(monkeypatch, run_tick, ["slow", "fast"])
    assert ticks == {"fast": 3, "slow": 1}
    assert all(0.45 <= later - earlier < 0.9 for earlier, later in zip(starts, starts[1:]))
    out = capsys.readouterr().out.splitlines()
    assert sorted(out[:-1]) == ["fast: tick 1", "fast: tick 2", "fast: tick 3", "slow: tick 1"]
    assert out[-1] == "herdr: swept"


def test_swarms_that_finish_together_tick_once(monkeypatch, capsys):
    ticks = []
    _tick_all(monkeypatch, lambda store, slug: ticks.append(slug) or ["ok"], ["a", "b", "c"])
    assert sorted(ticks) == ["a", "b", "c"]


def test_a_quick_swarm_stops_its_extra_ticks_at_the_pass_deadline(monkeypatch, capsys):
    import time

    monkeypatch.setattr(cli, "TICK_SECONDS", 1.0)
    monkeypatch.setattr(cli, "EXTRA_TICKS_UNTIL", 1.5)
    ticks = {"fast": 0, "slow": 0}

    def run_tick(store, slug):
        ticks[slug] += 1
        if slug == "slow":
            time.sleep(3.0)
        return []

    _tick_all(monkeypatch, run_tick, ["slow", "fast"])
    assert ticks == {"fast": 2, "slow": 1}


def test_an_extra_tick_starting_exactly_at_the_deadline_still_runs(monkeypatch, capsys):
    import threading
    import time
    import types

    monkeypatch.setattr(cli, "time", types.SimpleNamespace(monotonic=lambda: 100.0, time=time.time, sleep=time.sleep))
    monkeypatch.setattr(cli, "TICK_SECONDS", 0.05)
    monkeypatch.setattr(cli, "EXTRA_TICKS_UNTIL", 0)
    release, ticks = threading.Event(), {"fast": 0, "slow": 0}

    def run_tick(store, slug):
        ticks[slug] += 1
        if slug == "slow":
            release.wait(2)
        elif ticks["fast"] == 2:
            release.set()
        return []

    _tick_all(monkeypatch, run_tick, ["slow", "fast"])
    assert ticks["slow"] == 1 and ticks["fast"] >= 2


def test_a_first_tick_that_dies_still_ends_the_extra_ticks(monkeypatch, capsys):
    monkeypatch.setattr(cli, "TICK_SECONDS", 0.05)
    ticks = {"fast": 0}

    def run_tick(store, slug):
        if slug == "slow":
            raise SystemExit(3)
        ticks["fast"] += 1
        if ticks["fast"] > 3:
            pytest.fail("extra ticks went on after every first tick ended")
        return []

    with pytest.raises(SystemExit):
        _tick_all(monkeypatch, run_tick, ["fast", "slow"])
