from scripts.gates import intent
from scripts.gates.verdicts import Verdicts
from scripts.swarm import cli
from scripts.swarm.ledger_events import PullRequest
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr


def test_ready_intent_is_delivered_before_a_slow_runtime_launch(env, monkeypatch):  # noqa: F811
    store, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state="running", gates={"intent": "observe"})
    url = "https://github.com/o/r/pull/9"
    ledger.rows["t1"].update(
        state="pr", pr_url=url, title="Ready intent", description="Judge this pull request promptly."
    )
    ledger.pulls[url] = PullRequest("OPEN", None, None, False, head="h1")
    clock, delivered, at_launch = [1_000_000], [], []
    monkeypatch.setattr(cli, "now_ms", lambda: clock[0])
    monkeypatch.setattr(intent, "pr_view", lambda url: {"title": "Ready", "body": "", "files": [], "head": "h1"})

    def judge(state):
        delivered.append(clock[0])
        return {"verdict": "pass", "reason": "the phase can use it"}

    launch = runtime.spawn

    def slow_launch(*args, **kwargs):
        at_launch.append(Verdicts("sw", "intent").read("t1"))
        clock[0] += 780_000
        return launch(*args, **kwargs)

    monkeypatch.setattr(intent, "judged", judge)
    monkeypatch.setattr(runtime, "spawn", slow_launch)
    cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    wait_ms = delivered[0] - 1_000_000
    print(f"ready_to_verdict_ms={wait_ms}; runtime_launch_delay_ms=780000")
    assert at_launch and all(record and record["verdict"] == "pass" for record in at_launch)
    assert wait_ms == 0


def test_a_merge_wait_observes_a_merge_during_a_slow_runtime_launch(env, monkeypatch):  # noqa: F811
    from scripts.swarm import idle

    store, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    store.update("sw", max_eng=2, gates={"intent": "off"})
    agent = next(a for a in store.agents("sw") if a.lane == "eng")
    url = "https://github.com/o/r/pull/9"
    ledger.rows[agent.task].update(state="pr", pr_url=url)
    ledger.rows["t3"] = {"id": "t3", "state": "open", "lane": "eng", "claimed_by": "", "out_of_scope": False}
    ledger.pulls[url] = PullRequest("OPEN", None, None, False, queued=True)
    clock = [cli.now_ms()]
    monkeypatch.setattr(cli, "now_ms", lambda: clock[0])
    idle.declare_wait(
        store.redis, "sw", agent.name, clock[0] + 3_600_000, "", clock[0], {"kind": "merge", "target": url}
    )
    launch = runtime.spawn

    def slow_launch(*args, **kwargs):
        clock[0] += 780_000
        ledger.pulls[url] = PullRequest("MERGED", clock[0], None, False)
        return launch(*args, **kwargs)

    monkeypatch.setattr(runtime, "spawn", slow_launch)
    actions = cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert any(f"ended the wait of {agent.name}: pull request {url}, now merged" == action for action in actions)
    assert idle.wait(store.redis, "sw", agent.name) is None
