import pytest

from scripts.swarm import cli
from scripts.swarm.health import findings
from scripts.swarm.store import SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.mark.parametrize("mode", ["enforce", "observe", "off"])
def test_the_operator_sets_the_talk_gate_mode(env, monkeypatch, mode):  # noqa: F811
    store, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "set", f"talk-gate={mode}") == 0
    assert store.config("sw").gates == {"talk": mode}


def test_the_page_sets_it_as_the_operator(env, monkeypatch):  # noqa: F811
    store, _, _ = env
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
    run("sw", "create", "--repo", "/repo")
    store.update("sw", gates={"identity": "enforce"})
    assert run("sw", "set", "talk-gate=enforce", "max-eng-agents=3") == 0
    assert (store.config("sw").gates, store.config("sw").max_eng) == ({"identity": "enforce", "talk": "enforce"}, 3)


def test_an_agent_cannot_set_a_gate_mode(env, monkeypatch, capsys):  # noqa: F811
    store, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    run("sw", "create", "--repo", "/repo")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@a1b2c3-0001")
    assert run("sw", "set", "talk-gate=off") == 1
    assert "only the operator sets talk-gate" in capsys.readouterr().err
    assert store.config("sw").gates == {}


def test_an_unknown_gate_mode_is_refused():
    with pytest.raises(SwarmError, match=r"talk-gate takes deny, log only, skip"):
        cli.gate_mode("talk-gate", "loud", {})
    assert cli.gate_mode("talk-gate", "off", {"AGENTIHOOKS_AGENT_NAME": "operator"}) == {"talk": "off"}


def test_a_config_saved_before_gates_existed_reads_no_gates(env):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.redis.hdel(store.key("sw", "config"), "gates")
    assert store.config("sw").gates == {}


def test_block_records_the_state_change_before_its_note(env):  # noqa: F811
    store, ledger, _ = env
    order = []
    update, comment = ledger.update_task, ledger.comment
    ledger.update_task = lambda *a, **k: order.append("state") or update(*a, **k)
    ledger.comment = lambda *a, **k: order.append(("note", a[0])) or comment(*a, **k)
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as", "ci@a1b2c3-0001", "block", "waiting on a token only the operator can create") == 0
    assert order[-2:] == ["state", ("note", "sw")]


def test_the_tick_stamps_resolved_checks_as_the_owners_outcome(env, monkeypatch):  # noqa: F811
    from scripts.gates.progress import Mark, Progress
    from scripts.swarm.ledger_events import PullRequest
    from scripts.swarm.store import AgentRecord

    store, ledger, rt = env
    me, url = "engineer@a1b2c3-0001", "https://github.com/o/r/pull/9"
    run("sw", "create", "--repo", "/repo")
    store.put_agent("sw", AgentRecord(me, "eng", "t1", pane_id="p1"))
    rt.live.add(me)
    ledger.rows["t1"].update(state="pr", pr_url=url, claimed_by=me)
    ledger.pulls[url] = PullRequest("OPEN", None, 5, False, True)
    monkeypatch.setattr(cli, "now_ms", lambda: 9_000_000)
    actions = cli.run_tick(store, "sw", ledger, rt, cli.delivery.HerdrMessenger())
    assert f"checks resolved green on {url}, an outcome for {me}" in actions
    assert Progress(store.redis, "sw").read(me) == Mark(9_000_000, "checks resolved", 0)


def test_worker_ceremony_reads_the_talk_measure_and_the_master_keeps_the_ratio():
    limits = findings.Limits()
    events = [{"kind": "comment edited", "by": name, "target": "phases/p1"} for name in ("sw-eng-1", "sw-master-1")]
    events = events * 25
    found = findings.ceremony(events, {}, limits, talk={"sw-eng-1": 11, "sw-ci-1": 10, "sw-master-1": 50})
    assert [(f.subject, f.summary, f.evidence, f.threshold, f.measure) for f in found] == [
        (
            "sw-master-1",
            "more ledger transitions than outcomes",
            ("25 ledger transitions", "0 outcomes"),
            "at least 20 transitions and more than 12 per outcome",
            25,
        ),
        (
            "sw-eng-1",
            "talked past the budget since its last outcome",
            ("11 talk writes since its last outcome",),
            "more than 10 talk writes between outcomes",
            11,
        ),
    ]


def test_without_a_talk_measure_workers_keep_the_lifetime_ratio():
    events = [{"kind": "comment edited", "by": "sw-eng-1", "target": "phases/p1"}] * 25
    assert [f.subject for f in findings.ceremony(events, {}, findings.Limits())] == ["sw-eng-1"]
    assert findings.over_budget({"sw-eng-1": 1}) == []
