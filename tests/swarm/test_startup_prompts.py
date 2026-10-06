from pathlib import Path

import pytest

from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord

pytestmark = pytest.mark.xdist_group("fakeredis")

CAPTURE = Path(__file__).parents[1] / "fixtures/swarm/claude-import-prompt.txt"
TITLE = "Allow external CLAUDE.md file imports?"


def test_saved_import_prompt_overrides_herdr_working_status(tmp_path):
    calls = []

    def herdr(args):
        calls.append(args)
        if args[:2] == ["pane", "read"]:
            return {"text": CAPTURE.read_text()}
        return {"agent": {"name": "engineer-one", "pane_id": "w:p1", "agent_status": "working"}}

    runtime = HerdrRuntime(home=tmp_path, herdr=herdr)
    agent = AgentRecord("engineer-one", "eng", "task", pane_id="w:p1")
    assert runtime.status(agent) == "waiting"
    observed = runtime.observe(agent)
    assert observed.state == "waiting"
    assert observed.prompt_title == TITLE
    assert all(call[:2] in (["agent", "get"], ["pane", "read"]) for call in calls)
    assert [call for call in calls if call[:2] == ["pane", "read"]] == [
        ["pane", "read", "w:p1", "--source", "visible", "--format", "text"],
        ["pane", "read", "w:p1", "--source", "visible", "--format", "text"],
    ]


def test_claimed_selection_prompt_counts_consecutive_ticks_and_reports_health():
    import fakeredis

    from scripts.swarm.idle import beat
    from scripts.swarm.pane import PaneObservation
    from scripts.swarm.status import status_report
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import _watch_idle
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("prompt-proof", "/repo", max_eng=1, max_ci=0))
    agent = AgentRecord("engineer-one", "eng", "task", idle_ticks=2)
    store.put_agent("prompt-proof", agent)
    ledger = FakeLedger([{"id": "task", "state": "claimed", "title": "Startup proof"}])
    runtime = FakeRuntime()
    runtime.observe = lambda a: PaneObservation("waiting", TITLE)
    for n in range(1, 5):
        beat(store.redis, "prompt-proof", agent.name, "working", n)
        agent = store.agents("prompt-proof")[0]
        _watch_idle("prompt-proof", store, ledger, runtime, ledger.rows, agent, n)
        report = status_report(store, "prompt-proof", ledger.state("prompt-proof"))
        assert report["agents"][0]["status"] == "waiting"
        assert report["agents"][0]["input_ticks"] == n
        assert report["agents"][0]["input_prompt"] == TITLE
        assert report["agents"][0]["idle_ticks"] == 0
        found = [f for f in report["findings"] if f["kind"] == "waiting on input"]
        assert len(found) == (1 if n > 3 else 0)
    assert found[0]["subject"] == agent.name
    assert TITLE in str(found[0])
    assert runtime.nudged == runtime.killed == []
    runtime.observe = lambda a: PaneObservation("working")
    _watch_idle("prompt-proof", store, ledger, runtime, ledger.rows, store.agents("prompt-proof")[0], 5)
    report = status_report(store, "prompt-proof", ledger.state("prompt-proof"))
    assert report["agents"][0]["status"] == "working"
    assert report["agents"][0]["input_ticks"] == 0
    assert report["agents"][0]["input_prompt"] == ""
    assert not report["findings"]


def test_herdr_capture_reads_only_visible_plain_text(monkeypatch):
    from subprocess import CompletedProcess
    from unittest.mock import Mock

    from scripts.swarm.runtime import herdr_call

    run = Mock(return_value=CompletedProcess([], 0, CAPTURE.read_text(), ""))
    monkeypatch.setattr("scripts.herdr_host.binary", lambda: "/herdr")
    monkeypatch.setattr("scripts.swarm.runtime.subprocess.run", run)
    args = ["pane", "read", "w:p1", "--source", "visible", "--format", "text"]
    assert herdr_call(args) == {"text": CAPTURE.read_text()}
    run.assert_called_once_with(["/herdr", *args], capture_output=True, text=True, timeout=30, check=True)


def test_waiting_pane_beats_a_working_heartbeat_and_declared_wait():
    from scripts.swarm.idle import verdict

    assert verdict("waiting", {"state": "working", "at": 10}, {"until": 20}, 10) == "waiting"


def test_unclaimed_or_finished_agents_do_not_raise_input_findings():
    from scripts.swarm.health.findings import waiting_on_input

    agent = {"name": "engineer", "task": "task", "input_prompt": TITLE, "input_ticks": 4}
    for state in ("open", "done", "blocked", "handoff"):
        assert waiting_on_input([agent], {"task": {"id": "task", "state": state}}) == []
    assert waiting_on_input([{**agent, "input_prompt": ""}], {"task": {"id": "task", "state": "claimed"}}) == []


def test_input_prompt_changes_restart_the_counter_and_native_waiting_has_a_title():
    import fakeredis

    from scripts.swarm.pane import PaneObservation
    from scripts.swarm.store import RedisStore
    from scripts.swarm.tick import _watch_idle, agent_status
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    agent = AgentRecord("engineer", "eng", "task", input_prompt="Old prompt?", input_ticks=8)
    runtime = FakeRuntime()
    runtime.observe = lambda a: PaneObservation("waiting", TITLE)
    ledger = FakeLedger([])
    _watch_idle("sw", store, ledger, runtime, {}, agent, 0)
    changed = store.agents("sw")[0]
    assert changed.input_ticks == 1
    assert changed.input_prompt == TITLE
    runtime.observe = lambda a: PaneObservation("waiting")
    _watch_idle("sw", store, ledger, runtime, {}, changed, 1)
    changed = store.agents("sw")[0]
    assert changed.input_prompt == "Waiting on input"
    assert changed.input_ticks == 1
    assert agent_status(changed) == "waiting"
    assert agent_status(AgentRecord("ended", "eng", "task", state="finished", input_prompt=TITLE)) == "finished"


def test_runtime_read_failure_uses_the_observed_herdr_state(tmp_path):
    def herdr(args):
        if args[:2] == ["pane", "read"]:
            raise RuntimeError("read unavailable")
        return {"agent": {"name": "engineer", "pane_id": "w:p1", "status": "idle"}}

    runtime = HerdrRuntime(home=tmp_path, herdr=herdr)
    observed = runtime.observe(AgentRecord("engineer", "eng", "task", pane_id="w:p1"))
    assert observed.state == "idle"
    assert observed.prompt_title == ""


def test_a_master_selection_prompt_is_visible_without_idle_nudges():
    import fakeredis

    from scripts.swarm.pane import PaneObservation
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0, state="paused"))
    agent = AgentRecord("master", "master", "")
    store.put_agent("sw", agent)
    runtime = FakeRuntime()
    runtime.live.add(agent.name)
    runtime.observe = lambda a: PaneObservation("waiting", TITLE)
    tick("sw", store, FakeLedger([]), runtime, 1)
    master = store.agents("sw")[0]
    assert master.input_prompt == TITLE
    assert master.input_ticks == 1
    assert runtime.nudged == runtime.killed == []
    runtime.observe = lambda a: PaneObservation("idle")
    tick("sw", store, FakeLedger([]), runtime, 2)
    master = store.agents("sw")[0]
    assert master.input_prompt == ""
    assert master.idle_ticks == 0
    assert runtime.nudged == runtime.killed == []


def test_input_finding_keeps_all_evidence_and_skips_other_agents():
    from scripts.swarm.health.findings import Finding, waiting_on_input

    agents = [
        {"name": "ready", "task": "task", "input_prompt": TITLE, "input_ticks": 0},
        {"name": "waiting", "task": "task", "input_prompt": TITLE, "input_ticks": 4},
    ]
    tasks = {"task": {"id": "task", "title": "Startup proof", "state": "claimed"}}
    assert waiting_on_input(agents, tasks) == [
        Finding(
            "waiting on input",
            "waiting",
            "waiting on input for 4 ticks while holding a task",
            (f"prompt: {TITLE}", "task Startup proof (claimed)"),
            "more than 3 consecutive ticks waiting on input",
            4,
        )
    ]


def test_owned_pane_without_status_is_unknown(tmp_path):
    runtime = HerdrRuntime(home=tmp_path, herdr=lambda args: {"agent": {"name": "engineer", "pane_id": "w:p1"}})
    assert runtime.observe(AgentRecord("engineer", "eng", "task", pane_id="w:p1")).state == "unknown"


def test_master_prompt_observation_preserves_previous_retirement_actions():
    import fakeredis

    from scripts.swarm.pane import PaneObservation
    from scripts.swarm.store import RedisStore
    from scripts.swarm.tick import _reap
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent("sw", AgentRecord("finished", "eng", "task", state="finished"))
    store.put_agent("sw", AgentRecord("master", "master", ""))
    runtime = FakeRuntime()
    runtime.live.update(("finished", "master"))
    runtime.observe = lambda a: PaneObservation("waiting", TITLE)
    actions = _reap("sw", store, FakeLedger([]), runtime, {}, 1)
    assert actions == ["retired finished"]


FIXTURES = Path(__file__).parents[1] / "fixtures/swarm"
TRUST = "Quick safety check: Is this a project you created or one you trust?"


def _herdr_showing(capture, status="working"):
    def herdr(args):
        if args[:2] == ["pane", "read"]:
            return {"text": (FIXTURES / capture).read_text()}
        return {"agent": {"name": "engineer-one", "pane_id": "w:p1", "agent_status": status}}

    return herdr


@pytest.mark.parametrize(
    ("capture", "title"),
    [
        ("claude-trust-prompt.txt", TRUST),
        ("codex-update-prompt.txt", "Update available · 0.160.0 → 0.160.1"),
    ],
)
def test_real_claude_and_codex_startup_captures_read_as_waiting(tmp_path, capture, title):
    runtime = HerdrRuntime(home=tmp_path, herdr=_herdr_showing(capture))
    observed = runtime.observe(AgentRecord("engineer-one", "eng", "task", pane_id="w:p1"))
    assert (observed.state, observed.prompt_title) == ("waiting", title)


def test_real_codex_idle_composer_is_not_waiting(tmp_path):
    runtime = HerdrRuntime(home=tmp_path, herdr=_herdr_showing("codex-idle-composer.txt", "idle"))
    observed = runtime.observe(AgentRecord("engineer-one", "eng", "task", pane_id="w:p1"))
    assert (observed.state, observed.prompt_title) == ("idle", "")


def test_status_text_shows_a_pane_held_at_a_prompt_as_waiting_and_the_finding_names_it(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    import fakeredis

    from scripts.swarm import cli
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import _watch_idle
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("prompt-proof", "/repo", max_eng=1, max_ci=0))
    store.put_agent("prompt-proof", AgentRecord("engineer-one", "eng", "task", pane_id="w:p1"))
    ledger = FakeLedger([{"id": "task", "state": "claimed", "title": "Startup proof"}])
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    runtime = FakeRuntime()
    runtime.observe = HerdrRuntime(home=tmp_path, herdr=_herdr_showing("claude-trust-prompt.txt")).observe
    for tick in range(1, 5):
        agent = store.agents("prompt-proof")[0]
        _watch_idle("prompt-proof", store, ledger, runtime, ledger.rows, agent, tick)
        cli.cmd_status(store, SimpleNamespace(slug="prompt-proof", json=False))
        out = capsys.readouterr().out
        row = next(line.split("\t") for line in out.splitlines() if line.startswith("engineer-one\t"))
        assert row[8] == "waiting"
    assert f"prompt: {TRUST}" in out
    assert "finding  waiting on input  engineer-one" in out
