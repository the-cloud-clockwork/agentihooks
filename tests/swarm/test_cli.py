import json
import subprocess

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, runtime, timer
from scripts.swarm.resume import Outcome
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def env(monkeypatch, tmp_path):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "ci"}])
    ledger.said = []
    ledger.comments = []
    ledger.say = lambda slug, text, by=None: ledger.said.append((text, by))
    ledger.comment = lambda slug, task, text, by: ledger.comments.append((task, text, by))
    rt = FakeRuntime()
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: rt)
    monkeypatch.setattr(cli.timer, "ensure", lambda binary: True)
    ledger.chat = lambda slug: [{"id": "old", "by": "operator", "at": 50, "text": "old talk"}]
    monkeypatch.setattr(cli.delivery, "HerdrMessenger", lambda: FakeHerdr({}))
    return store, ledger, rt


def run(*argv):
    return cli.main(list(argv))


def test_create_is_paused_then_start_spawns_and_status_lists(env, capsys):
    store, ledger, rt = env
    assert run("sw", "create", "--repo", "/repo", "--max-eng-agents", "1", "--max-ci-agents", "1") == 0
    assert store.config("sw").state == "paused" and rt.spawned == []
    assert run("sw", "start") == 0
    assert [s[1] for s in rt.spawned] == ["sw-eng-1", "sw-ci-1"]
    run("sw", "status")
    assert "sw-eng-1" in capsys.readouterr().out


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
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "sw-eng-1")
    assert run("sw", "done", "--pr", "https://github.com/o/r/pull/9") == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["pr_url"]) == ("done", "https://github.com/o/r/pull/9")
    assert [a.state for a in store.agents("sw") if a.name == "sw-eng-1"] == ["finished"]
    assert store.claimant("sw", "t1") is None


def test_block_comments_parks_and_finishes(env):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as", "sw-ci-1", "block", "waiting on a token only the operator can create") == 0
    assert ledger.rows["t2"]["state"] == "blocked"
    assert ledger.comments == [("t2", "waiting on a token only the operator can create", "sw-ci-1")]


def test_say_addresses_and_strangers_are_refused(env, capsys):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as", "sw-eng-1", "say", "the docs task is merged", "--to", "ci") == 0
    assert ledger.said == [("@ci the docs task is merged", "sw-eng-1")]
    [item] = InboxStore(store.redis).inbox("sw-ci-1")
    assert (item.sender, item.text, item.state) == ("sw-eng-1", "the docs task is merged", "pending")
    assert run("sw", "--as", "sw-eng-1", "say", "status for the page only") == 0
    assert ledger.said[-1] == ("status for the page only", "sw-eng-1")
    assert [i.text for i in InboxStore(store.redis).inbox("sw-ci-1")] == ["the docs task is merged"]
    assert run("sw", "--as", "stranger", "say", "hello") == 1
    assert "not an agent" in capsys.readouterr().err


def test_stop_now_terminates_reopens_claimed_but_not_finished_work(env, monkeypatch):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "sw-ci-1")
    run("sw", "done", "--pr", "https://github.com/o/r/pull/4")
    assert run("sw", "stop", "--now") == 0
    assert sorted(rt.killed) == ["sw-ci-1", "sw-eng-1", "sw-master-1"]
    assert store.config("sw").state == "stopped" and store.agents("sw") == []
    assert (ledger.rows["t1"]["state"], ledger.rows["t2"]["state"]) == ("open", "done")


def test_create_refuses_ids_that_break_agent_names(env, capsys):
    assert run("2026-q4", "create", "--repo", "/repo") == 1
    assert "starting with a letter" in capsys.readouterr().err


def test_as_equals_is_not_mistaken_for_set(env, monkeypatch):
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as=sw-eng-1", "issue", "https://github.com/o/r/issues/1") == 0


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


def test_runtime_spawns_through_init_agent_with_a_private_prompt(tmp_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="pane_id=w3:p1\nstatus=started\nroute_status=direct\naccount=acct\nagent=codex\n", stderr=""
        )

    rt = runtime.HerdrRuntime(home=tmp_path, run=fake_run, choose=lambda r, e: ("codex", "priority"))
    config = cli.SwarmConfig("sw", "/repo", 1, 1)
    placed = rt.spawn(config, "ci", "sw-ci-1", {"id": "t2", "title": "speed up the tests"})
    prompt_path = tmp_path / "sw" / "prompts" / "sw-ci-1.md"
    assert placed == runtime.Placed("w3:p1", "codex", "acct")
    assert seen[0][1:4] == ["init-agent", "--host", "herdr"]
    assert seen[0][seen[0].index("--name") + 1 : seen[0].index("--name") + 4] == ["sw-ci-1", "--agent", "codex"]
    assert oct(prompt_path.stat().st_mode)[-3:] == "600"
    text = prompt_path.read_text()
    assert "speed up the tests" in text and "CI speed" in text and "agentihooks swarm sw done --pr" in text


def test_runtime_spawn_failure_names_the_reason(tmp_path):
    def fail(argv, **kw):
        return subprocess.CompletedProcess(argv, 3, stdout="status=failed\n", stderr="herdr: no server\n")

    rt = runtime.HerdrRuntime(home=tmp_path, run=fail, choose=lambda r, e: ("claude", "priority"))
    with pytest.raises(runtime.SpawnError, match="no server"):
        rt.spawn(cli.SwarmConfig("sw", "/repo", 1, 1), "eng", "sw-eng-1", {"id": "t1", "title": "x"})


def test_agent_record_round_trips_through_status_json(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("sw-eng-9", "eng", "t1"))
    run("sw", "status", "--json")
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["agents"][0]["name"] == "sw-eng-9"


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
        rt.spawn(cli.SwarmConfig("sw", "/repo", 1, 1), "eng", "sw-eng-1", {"id": "t1", "title": "x"})
    assert calls == []


def test_runtime_treats_a_failed_route_as_a_failed_spawn_and_cleans_up(tmp_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv[1])
        out = "pane_id=w3:p1\nstatus=started\nroute_status=failed\n" if argv[1] == "init-agent" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    rt = runtime.HerdrRuntime(home=tmp_path, run=fake_run, choose=lambda r, e: ("claude", "priority"))
    with pytest.raises(runtime.SpawnError):
        rt.spawn(cli.SwarmConfig("sw", "/repo", 1, 1), "eng", "sw-eng-1", {"id": "t1", "title": "x"})
    assert seen == ["init-agent", "terminate-agent"]


def test_runtime_retire_reports_a_failed_terminate_and_closes_leftover_panes(tmp_path):
    closed = []
    rt = runtime.HerdrRuntime(
        home=tmp_path,
        run=lambda argv, **kw: subprocess.CompletedProcess(argv, 2, stdout="", stderr="ambiguous"),
        herdr=lambda args: closed.append(args) or {},
    )
    agent = AgentRecord("sw-eng-1", "eng", "t1", pane_id="w3:p1")
    assert rt.retire(agent, live=True) is False and closed == []
    assert rt.retire(agent, live=False) is True and closed == [["pane", "close", "w3:p1"]]


def test_runtime_retire_forces_past_the_agents_own_subagents(tmp_path):
    seen = []
    rt = runtime.HerdrRuntime(
        home=tmp_path,
        run=lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0),
        herdr=lambda a: {},
    )
    assert rt.retire(AgentRecord("sw-eng-1", "eng", "t1"), live=True)
    assert seen[0][1:] == ["terminate-agent", "sw-eng-1", "--force-shared"]


def test_handoff_finishes_the_agent_keeps_the_claim_and_stores_the_doc(env, tmp_path, capsys):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc = tmp_path / "handoff.md"
    doc.write_text("issue 7 is open, tests red on seam 2")
    assert run("sw", "--as", "sw-eng-1", "handoff", str(doc)) == 0
    assert [a.state for a in store.agents("sw") if a.name == "sw-eng-1"] == ["finished"]
    assert store.claimant("sw", "t1") == "sw-eng-1"
    assert store.handoff("sw", "t1") == "issue 7 is open, tests red on seam 2"
    assert store.handoff_seat("sw", "t1") == "eng-1@sw"
    assert ledger.rows["t1"]["state"] == "claimed"
    assert "stop now" in capsys.readouterr().out


def test_handoff_refuses_a_missing_document(env, capsys):
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as", "sw-eng-1", "handoff", "/no/such/doc.md") == 1
    assert "handoff" in capsys.readouterr().err


def test_agent_prompt_starts_by_reading_the_ledger_json():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "eng", "sw-eng-1", {"id": "t1", "title": "x"})
    assert text.index("~/development-ledger/sw.json") < text.index("Work it end to end")


def test_status_json_gives_every_agent_a_status_and_its_model(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    records = {
        "sw-eng-1": AgentRecord("sw-eng-1", "eng", "t1", state="starting", model="opus", effort="high"),
        "sw-eng-2": AgentRecord("sw-eng-2", "eng", "t1", idle_ticks=1),
        "sw-eng-3": AgentRecord("sw-eng-3", "eng", "t1", idle_ticks=3),
        "sw-eng-4": AgentRecord("sw-eng-4", "eng", "t1", state="finished", idle_ticks=5),
        "sw-eng-5": AgentRecord("sw-eng-5", "eng", "t1"),
    }
    for record in records.values():
        store.put_agent("sw", record)
    run("sw", "status", "--json")
    agents = {a["name"]: a for a in json.loads(capsys.readouterr().out.splitlines()[-1])["agents"]}
    assert {n: a["status"] for n, a in agents.items()} == {
        "sw-eng-1": "working",
        "sw-eng-2": "idle",
        "sw-eng-3": "stalled",
        "sw-eng-4": "finished",
        "sw-eng-5": "working",
    }
    assert (agents["sw-eng-1"]["model"], agents["sw-eng-1"]["effort"]) == ("opus", "high")


def test_status_text_shows_each_agent_model_and_effort_or_unknown(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1", harness="codex", model="gpt-6.1-sol", effort="high"))
    store.put_agent("sw", AgentRecord("sw-eng-2", "eng", "t2", harness="claude"))
    run("sw", "status")
    lines = {line.split("\t")[0]: line.split("\t") for line in capsys.readouterr().out.splitlines() if "\t" in line}
    assert "gpt-6.1-sol high" in lines["sw-eng-1"]
    assert "unknown" in lines["sw-eng-2"]


def test_agent_prompt_joins_the_ledger_watches_it_and_leaves_before_done():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "eng", "sw-eng-1", {"id": "t1", "title": "x", "phase": "p1"})
    led = "agentihooks ledger --slug sw --as sw-eng-1"
    assert f"{led} join" in text
    assert "agentihooks ledger watch sw --as sw-eng-1" in text
    assert f"{led} ack" in text
    assert f"{led} comment phases/p1" in text
    assert f"{led} followup add" in text
    assert text.index(f"{led} join") < text.index("Work it end to end")
    assert text.index(f"{led} leave") < text.index("agentihooks swarm sw done --pr")


def test_agent_prompt_runs_gates_and_review_before_the_merge_and_ends_with_leave_then_done():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "eng", "sw-eng-1", {"id": "t1", "title": "x", "phase": "p1"})
    steps = [line for line in text.splitlines() if line[:1].isdigit() and line[1:3] == ". "]
    review = next(i for i, s in enumerate(steps) if "Gates green" in s and "review per the dev-cycle skill" in s)
    merge = next(i for i, s in enumerate(steps) if "Merge on green checks" in s)
    assert review < merge
    assert "Standards and Spec" in steps[review] and "three rounds" in steps[review]
    last = steps[-1]
    assert last.index("agentihooks ledger --slug sw --as sw-eng-1 leave") < last.index("agentihooks swarm sw done --pr")


def test_set_compact_limit_stores_it_on_the_swarm(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "set", "compact-limit=40") == 0
    assert store.config("sw").compact_limit == 40
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["compact_limit"] == 40


def test_the_master_takes_no_task_commands(env, capsys):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("sw-master-1", "master", "master"))
    for argv in (("issue", "https://x/issues/1"), ("pr", "https://x/pull/1"), ("done",), ("block", "why")):
        assert run("sw", "--as", "sw-master-1", *argv) == 1
        assert "master works no task" in capsys.readouterr().err
    assert ledger.comments == [] and [a.state for a in store.agents("sw")] == ["working"]


def test_a_master_handoff_stores_the_doc_for_its_successor(env, tmp_path):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("sw-master-1", "master", "master"))
    doc = tmp_path / "handoff.md"
    doc.write_text("operator asked for a docs task")
    assert run("sw", "--as", "sw-master-1", "handoff", str(doc)) == 0
    assert store.handoff("sw", "master") == "operator asked for a docs task"
    assert [a.state for a in store.agents("sw")] == ["finished"]


def test_master_prompt_runs_the_swarm_and_never_codes():
    from scripts.swarm import prompt

    text = prompt.build("sw", "/repo", "master", "sw-master-2", {"id": "master", "handoff": "caps go to four"})
    led = "agentihooks ledger --slug sw --as sw-master-2"
    for needle in (
        f"{led} join --role orchestrator",
        "agentihooks ledger watch sw --as sw-master-2",
        f"{led} ack",
        f"{led} task add",
        f"{led} time-left",
        f"{led} followup add",
        "agentihooks swarm sw --as sw-master-2 say --to operator",
        "agentihooks msg reply",
        "agentihooks swarm sw --as sw-master-2 send-message",
        "agentihooks swarm sw --as sw-master-2 set max-eng-agents=",
        "agentihooks swarm sw --as sw-master-2 pause",
        "agentihooks swarm sw --as sw-master-2 stop",
        "agentihooks swarm sw --as sw-master-2 handoff",
        "browser_close",
        "caps go to four",
    ):
        assert needle in text, needle
    assert "never edit code, commit or merge" in text
    assert "wt.sh new" not in text and "done --pr" not in text


def test_the_tick_wakes_an_idle_pane_holding_a_pending_inbox_item(env):
    from scripts.inbox.store import InboxStore
    from scripts.inbox.wake import WAKE_TEXT

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1"))
    rt.live.add("sw-eng-1")
    item = InboxStore(store.redis).send("operator", "sw-eng-1", "look at the failing check")
    herdr = FakeHerdr({"p1": "idle"})
    actions = cli.run_tick(store, "sw", ledger, rt, herdr)
    assert herdr.prompts == [("p1", WAKE_TEXT)]
    assert f"woke sw-eng-1 for message {item.id}" in actions


def test_status_carries_health_findings_for_the_master_to_read(env, capsys, monkeypatch, tmp_path):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1", idle_ticks=4))
    ledger.rows["t1"].update(state="claimed", claimed_by="sw-eng-1", title="Fold the chat panel")
    monkeypatch.setattr(cli.activity, "default_root", lambda: tmp_path)
    run("sw", "status", "--json")
    found = json.loads(capsys.readouterr().out.splitlines()[-1])["findings"]
    assert [(f["kind"], f["subject"], f["evidence"]) for f in found] == [
        ("idle with claim", "sw-eng-1", ["task Fold the chat panel (claimed)"])
    ]
    run("sw", "status")
    assert capsys.readouterr().out.splitlines()[-4:] == [
        "finding  idle with claim  sw-eng-1: idle for 4 ticks while holding a task",
        "  - task Fold the chat panel (claimed)",
        "  threshold 3 idle ticks",
        "  id idle-with-claim/sw-eng-1",
    ]


def test_status_prints_each_evidence_entry_on_its_own_line(env, capsys, monkeypatch):
    run("sw", "create", "--repo", "/repo")
    entries = ("Split the parser, gain 4", "Cache the index, gain 1.5", "q2, no gain stated")
    finding = cli.health.Finding("scope inflation", "sw-eng-1", "queued 3 tasks for its own lane", entries, "3 tasks")
    monkeypatch.setattr(cli.health, "findings", lambda *a: [finding])
    run("sw", "status")
    assert capsys.readouterr().out.splitlines()[-6:] == [
        "finding  scope inflation  sw-eng-1: queued 3 tasks for its own lane",
        "  - Split the parser, gain 4",
        "  - Cache the index, gain 1.5",
        "  - q2, no gain stated",
        "  threshold 3 tasks",
        "  id scope-inflation/sw-eng-1",
    ]


def test_a_page_line_to_the_master_becomes_an_item_and_the_reply_closes_the_loop(env):
    from scripts.swarm import operator_mail
    from scripts.swarm_ledger.watch_ledger import line

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("sw-master-1", "master", "master", pane_id="m1", seat="master@sw"))
    store.seats.occupy("master@sw", "sw-master-1", 1)
    rt.live.add("sw-master-1")
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
    answer = box.reply(item.id, "sw-master-1", "two tasks left")
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({"m1": "working"}))
    assert ("two tasks left", "sw-master-1") in ledger.said
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
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1"))
    assert run("sw", "remove") == 1
    assert store.slugs() == ["other", "sw"]
    store.drop_agent("sw", "sw-eng-1")
    assert run("sw", "remove") == 0
    assert store.slugs() == ["other"]
    assert cli.activity.counts("sw") == {}
    assert cli.activity.counts("other") == {"other-eng-1": {"watch": 1, "act": 0}}
    run("sw", "create", "--repo", "/repo")
    assert cli.activity.counts("sw") == {}
    assert store.next_name("sw", "eng") == "sw-eng-1"
    assert store.next_name("other", "eng") == "other-eng-2"


def _idle_finding(env, monkeypatch, tmp_path):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("sw-master-1", "master", "master"))
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1", idle_ticks=4))
    ledger.rows["t1"].update(state="claimed", claimed_by="sw-eng-1", title="Fold the chat panel")
    monkeypatch.setattr(cli.activity, "default_root", lambda: tmp_path)


def _findings(capsys):
    run("sw", "status", "--json")
    return json.loads(capsys.readouterr().out.splitlines()[-1])["findings"]


def test_a_master_verdict_hides_the_finding_from_status(env, capsys, monkeypatch, tmp_path):
    _idle_finding(env, monkeypatch, tmp_path)
    [found] = _findings(capsys)
    assert (found["id"], found["verdict"]) == ("idle-with-claim/sw-eng-1", None)
    assert run("sw", "--as", "sw-master-1", "verdict", found["id"], "false-positive", "--note", "on checks") == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "false-positive"
    assert _findings(capsys) == []


def test_the_operator_may_give_a_verdict_and_a_worker_may_not(env, capsys, monkeypatch, tmp_path):
    _idle_finding(env, monkeypatch, tmp_path)
    _findings(capsys)
    assert run("sw", "--as", "sw-eng-1", "verdict", "idle-with-claim/sw-eng-1", "resolved") == 1
    assert "only the master or the operator" in capsys.readouterr().err
    assert run("sw", "--as", "operator", "verdict", "idle-with-claim/sw-eng-1", "resolved") == 0


def test_status_skips_idle_with_claim_while_the_pull_request_waits_on_checks(env, capsys, monkeypatch, tmp_path):
    store, ledger, _ = env
    _idle_finding(env, monkeypatch, tmp_path)
    ledger.rows["t1"].update(state="pr", pr_url="https://github.com/o/r/pull/7")
    monkeypatch.setattr(cli.checks, "pending", lambda url, run=None, approval=False: url.endswith("/7"))
    assert _findings(capsys) == []


def test_status_skips_idle_only_when_green_checks_wait_for_operator_approval(env, capsys, monkeypatch, tmp_path):
    from functools import partial

    from tests.swarm.test_health_checks import PASSED, runner

    _, ledger, _ = env
    _idle_finding(env, monkeypatch, tmp_path)
    ledger.rows["t1"].update(state="pr", pr_url="https://github.com/o/r/pull/7")
    monkeypatch.setattr(cli.checks, "cached", partial(cli.checks.cached, run=runner(PASSED)))
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
    monkeypatch.setattr(cli.checks, "cached", partial(cli.checks.cached, run=runner(out, code)))
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
    assert run("sw", "--as", "sw-ci-1", "say", "take my task", "--to", "eng") == 1
    assert "can only observe" in capsys.readouterr().err
    assert ledger.said == [] and InboxStore(store.redis).inbox("sw-eng-1") == []


def test_set_codex_share_and_status_shows_the_share_against_the_target(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "set", "codex-share=40", "codex-min-week-left=10") == 0
    config = store.config("sw")
    assert (config.codex_share, config.codex_min_week_left) == (40, 10)
    for harness in ("codex", "claude", "claude", "claude"):
        store.count_spawn("sw", harness)
    capsys.readouterr()
    run("sw", "status")
    assert "codex 1/4 spawns 25%  target 40%  min week left 10%" in capsys.readouterr().out.splitlines()[0]


def test_status_takes_the_codex_target_from_the_environment_without_a_swarm_setting(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM_CODEX_SHARE", "50")
    monkeypatch.delenv("AGENTIHOOKS_SWARM_CODEX_MIN_WEEK_LEFT", raising=False)
    run("sw", "create", "--repo", "/repo")
    capsys.readouterr()
    run("sw", "status")
    assert "codex 0/0 spawns 0%  target 50%  min week left 5%" in capsys.readouterr().out.splitlines()[0]


@pytest.mark.parametrize("setting, expected", [(None, 30), (50, 50), (0, 0)])
def test_status_json_reports_the_effective_codex_target(env, capsys, monkeypatch, setting, expected):
    monkeypatch.delenv("AGENTIHOOKS_SWARM_CODEX_SHARE", raising=False)
    if setting is not None:
        monkeypatch.setenv("AGENTIHOOKS_SWARM_CODEX_SHARE", str(setting))
    run("sw", "create", "--repo", "/repo")
    capsys.readouterr()
    run("sw", "status", "--json")
    assert json.loads(capsys.readouterr().out)["config"]["codex_share"] == expected


def test_set_refuses_a_codex_share_over_one_hundred(env):
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "set", "codex-share=101") == 1


def test_status_shows_each_agent_conversation_id_or_a_dash(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1", pane_id="w1:p1", conversation_id="5c90d80c"))
    store.put_agent("sw", AgentRecord("sw-eng-2", "eng", "t2", pane_id="w1:p2"))
    run("sw", "status")
    lines = {line.split("\t")[0]: line.split("\t") for line in capsys.readouterr().out.splitlines() if "\t" in line}
    assert lines["sw-eng-1"][-1] == "5c90d80c" and lines["sw-eng-2"][-1] == "-"
    run("sw", "status", "--json")
    agents = {a["name"]: a for a in json.loads(capsys.readouterr().out.splitlines()[-1])["agents"]}
    assert (agents["sw-eng-1"]["conversation_id"], agents["sw-eng-2"]["conversation_id"]) == ("5c90d80c", "")


def test_status_shows_each_restored_agent_outcome_with_its_reason(env, capsys):
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    resumed = {
        "name": "sw-eng-1",
        "lane": "eng",
        "task": "t1",
        "outcome": "resumed",
        "reason": "own conversation reopened",
    }
    fresh = {"name": "sw-eng-2", "lane": "eng", "task": "t2", "outcome": "fresh", "reason": "worktree gone"}
    store.put_restored("sw", [resumed, fresh])
    run("sw", "status")
    out = capsys.readouterr().out.splitlines()
    assert "restored  sw-eng-1  resumed  own conversation reopened" in out
    assert "restored  sw-eng-2  fresh  worktree gone" in out
    run("sw", "status", "--json")
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["restored"] == [resumed, fresh]


def test_restore_hands_the_runtime_to_restore_and_prints_every_agent_outcome(env, capsys, monkeypatch):
    _, _, rt = env
    seen = {}

    def restore(store, slug, live, source, runtime):
        seen["runtime"] = runtime
        return [Outcome("sw-eng-1", "eng", "t1", "fresh", "no conversation id")]

    run("sw", "create", "--repo", "/repo")
    monkeypatch.setattr(cli.snapshot, "newest", lambda slug: "/snap.json")
    monkeypatch.setattr(cli.snapshot, "restore", restore)
    run("sw", "restore")
    printed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert [(r["name"], r["outcome"], r["reason"]) for r in printed["restored"]] == [
        ("sw-eng-1", "fresh", "no conversation id")
    ]
    assert seen["runtime"] is rt


@pytest.mark.parametrize("command", ["create", "start", "url"])
def test_create_start_and_url_end_with_the_ledger_page_line(env, capsys, monkeypatch, command):
    monkeypatch.setattr(cli.ledger_link, "answering", lambda: True)
    if command != "create":
        run("sw", "create", "--repo", "/repo")
        capsys.readouterr()
    argv = ["sw", command, "--repo", "/repo"] if command == "create" else ["sw", command]
    assert run(*argv) == 0
    assert capsys.readouterr().out.splitlines()[-1] == cli.ledger_link.page_line("sw")
