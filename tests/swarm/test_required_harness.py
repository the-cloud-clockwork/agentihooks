from types import SimpleNamespace

import pytest

from scripts.swarm import capacity, runtime
from scripts.swarm.store import AgentRecord, SwarmConfig
from scripts.swarm.tick import SpawnError
from tests.swarm.profile_fixture import validated


def claude(name="cc", sessions=0, cap=6, state="OPEN"):
    return capacity.Account("claude", name, state, sessions, 90, 90, cap)


def codex(name="cx", sessions=0, cap=6, state="OPEN"):
    return capacity.Account("codex", name, state, sessions, 90, 90, cap)


@pytest.fixture(autouse=True)
def mountable(monkeypatch):
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda profile: False)


def _spawned(tmp_path, task, rows, lanes=None):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    rt = runtime.HerdrRuntime(home=tmp_path, run=run)
    rt._quota_accounts = rows
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes=lanes or {}, autonomy="delegate"
    )
    rt.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "profile": "engineer", **task})
    argv = seen["argv"]
    return argv[argv.index("--agent") + 1], argv[argv.index("--route") + 1]


@pytest.mark.feature("task-requires-harness")
@pytest.mark.parametrize(
    ("task", "placed"),
    [
        ({"requires_harness": "claude"}, ("claude", "cc")),
        ({}, ("codex", "cx")),
        ({"requires_harness": "codex"}, ("codex", "cx")),
    ],
)
def test_a_task_requiring_claude_is_spawned_only_on_claude(tmp_path, task, placed):
    assert _spawned(tmp_path, task, [claude(sessions=3), codex()]) == placed


@pytest.mark.feature("task-requires-harness")
def test_a_task_requiring_claude_waits_while_no_claude_seat_is_free(tmp_path):
    with pytest.raises(SpawnError) as refused:
        _spawned(tmp_path, {"requires_harness": "claude"}, [claude(sessions=6), codex()])
    assert refused.value.status == "unavailable"


@pytest.mark.feature("task-requires-harness")
def test_a_task_requiring_claude_drops_a_saved_codex_launch(tmp_path):
    saved = {"launch_assignment": {"harness": "codex", "account": "cx", "profile": "engineer"}}
    assert _spawned(tmp_path, {"requires_harness": "claude", **saved}, [claude(), codex()]) == ("claude", "cc")


@pytest.mark.feature("task-requires-harness")
def test_a_lane_pinned_to_codex_refuses_a_task_requiring_claude(tmp_path):
    with pytest.raises(SpawnError) as refused:
        _spawned(tmp_path, {"requires_harness": "claude"}, [claude(), codex()], {"eng": {"agent": "codex"}})
    assert refused.value.status == "unsupported"


@pytest.mark.feature("task-requires-harness")
def test_the_quota_plan_reserves_only_the_required_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "accounts", lambda environ, now, refresh=True: [claude(sessions=5), codex()])
    rt = runtime.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", str(tmp_path), max_eng=2, max_ci=0, max_plan=0)
    ready = [{"id": "t1", "requires_harness": "claude"}, {"id": "t2"}]
    requirements = rt.quota_requirements(config, {"eng": ready, "ci": [], "plan": []})
    assert requirements["eng"] == [("claude",), ("claude", "codex")]
    tasks = rt.quota_capacity(
        config, [AgentRecord("master", "master", "master")], 100, {"eng": 2, "ci": 0, "plan": 0}, requirements
    )["tasks"]
    assert tasks == {"t1": "claude", "t2": "codex"}
