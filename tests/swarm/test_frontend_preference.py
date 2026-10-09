from types import SimpleNamespace

import pytest

from scripts.swarm import capacity, profile_choice, runtime
from scripts.swarm.store import SwarmConfig
from tests.swarm.profile_fixture import validated


def claude(name="cc", sessions=0, cap=6, state="OPEN"):
    return capacity.Account("claude", name, state, sessions, 90, 90, cap)


def codex(name="cx", sessions=0, cap=6, state="OPEN"):
    return capacity.Account("codex", name, state, sessions, 90, 90, cap)


@pytest.fixture(autouse=True)
def mountable(monkeypatch):
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda profile: False)


def test_only_a_frontend_profile_names_a_harness_order():
    assert profile_choice.preferred("frontend") == ("claude", "codex")
    assert profile_choice.preferred("engineer") == ()
    assert profile_choice.preferred("qa") == ()


def _spawned(tmp_path, profile, rows, lanes=None):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    rt = runtime.HerdrRuntime(home=tmp_path, run=run)
    rt._quota_accounts = rows
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes=lanes or {}, autonomy="delegate"
    )
    rt.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "profile": profile})
    argv = seen["argv"]
    return argv[argv.index("--agent") + 1], argv[argv.index("--route") + 1]


@pytest.mark.parametrize(
    ("profile", "rows", "placed"),
    [
        ("frontend", [claude(sessions=3), codex()], ("claude", "cc")),
        ("engineer", [claude(sessions=3), codex()], ("codex", "cx")),
        ("frontend", [claude(cap=0, state="CLOSED"), codex(sessions=4)], ("codex", "cx")),
        ("frontend", [claude(sessions=6), codex(sessions=4)], ("codex", "cx")),
    ],
)
def test_a_frontend_spawn_takes_an_open_claude_seat_else_codex(tmp_path, profile, rows, placed):
    assert _spawned(tmp_path, profile, rows) == placed


def test_a_pinned_lane_agent_still_wins_over_the_frontend_order(tmp_path):
    assert _spawned(tmp_path, "frontend", [claude(), codex(sessions=3)], {"eng": {"agent": "codex"}}) == (
        "codex",
        "cx",
    )


def _planned(tmp_path, monkeypatch, rows, ready):
    monkeypatch.setattr(capacity, "accounts", lambda environ, now, refresh=True: list(rows))
    rt = runtime.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", str(tmp_path), max_eng=len(ready), max_ci=0, max_plan=0)
    requirements = rt.quota_requirements(config, {"eng": ready, "ci": [], "plan": []})
    return rt.quota_capacity(config, [], 100, {"eng": len(ready), "ci": 0, "plan": 0}, requirements)["tasks"]


def test_the_quota_plan_gives_a_frontend_task_the_open_claude_seat(tmp_path, monkeypatch):
    ready = [{"id": "t1", "profile": "engineer"}, {"id": "t2", "profile": "frontend"}]
    rows = [claude(sessions=5), codex(sessions=0)]
    assert _planned(tmp_path, monkeypatch, rows, ready) == {"t1": "codex", "t2": "claude"}


def test_the_quota_plan_sends_frontend_tasks_past_the_claude_seats_to_codex(tmp_path, monkeypatch):
    ready = [{"id": "t1", "profile": "frontend"}, {"id": "t2", "profile": "frontend"}]
    rows = [claude(sessions=5), codex(sessions=3)]
    assert _planned(tmp_path, monkeypatch, rows, ready) == {"t1": "claude", "t2": "codex"}


def test_the_quota_plan_keeps_claude_seats_for_tasks_pinned_to_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda profile: profile == "kit")
    ready = [{"id": "t1", "profile": "frontend"}, {"id": "t2", "profile": "kit"}]
    rows = [claude(sessions=5), codex(sessions=3)]
    assert _planned(tmp_path, monkeypatch, rows, ready) == {"t1": "codex", "t2": "claude"}


@pytest.mark.parametrize(
    ("ready", "rows", "placed"),
    [
        (["frontend", "kit"], [claude(sessions=4), codex(sessions=3)], {"t0": "claude", "t1": "claude"}),
        (["frontend", "frontend"], [claude(sessions=4), codex(sessions=3)], {"t0": "claude", "t1": "claude"}),
        (["frontend"], [claude(sessions=6), codex(sessions=6)], {}),
    ],
)
def test_the_quota_plan_counts_each_claude_seat_once(tmp_path, monkeypatch, ready, rows, placed):
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda profile: profile == "kit")
    tasks = [{"id": f"t{i}", "profile": profile} for i, profile in enumerate(ready)]
    assert _planned(tmp_path, monkeypatch, rows, tasks) == placed


def test_a_frontend_quota_handoff_follows_its_predecessor_harness(tmp_path, monkeypatch):
    launch = {"profile": "frontend", "harness": "codex", "account": "default", "model": "gpt", "effort": "high"}
    ready = [{"id": "q", "handoff_envelope": {"reason": "quota", "launch": launch}}]
    rows = [claude(), codex("default"), codex("cx2")]
    assert _planned(tmp_path, monkeypatch, rows, ready) == {"q": "codex"}
