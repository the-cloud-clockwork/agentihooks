from types import SimpleNamespace

import pytest

from scripts import terminate_agent
from scripts.swarm import naming, runtime
from scripts.swarm.reaper import Outcome


@pytest.fixture
def held(tmp_path, monkeypatch):
    carried = {5: "", 7: "x@a"}
    sessions = [SimpleNamespace(name="x@a", process=SimpleNamespace(pid=p)) for p in (5, 7)]
    monkeypatch.setattr(terminate_agent, "sessions", lambda: sessions)
    monkeypatch.setattr(
        terminate_agent,
        "agent_environ",
        lambda pid, keys: tuple(carried[pid] if key == "AGENTIHOOKS_AGENT_NAME" else "" for key in keys),
    )
    monkeypatch.setattr(naming, "resolve_name", lambda name: {"x-a": "x@a"}.get(name, name))
    reaped = []
    rt = runtime.HerdrRuntime(home=tmp_path)
    rt.hive = SimpleNamespace(holder=lambda name: pytest.fail("a locally held name is never ordered to a hive"))
    rt.reap = lambda pids: reaped.append(pids) or Outcome(tuple(pids))
    return SimpleNamespace(rt=rt, carried=carried, reaped=reaped)


def test_runtime_spares_a_session_whose_environment_no_longer_carries_the_stray_name(held):
    assert held.rt.reap_name("x@a") is False and held.reaped == [[7]]
    held.carried[7] = "engineer@other-0002"
    assert held.rt.reap_name("x@a") is False and held.reaped[-1] == []
    held.carried[5] = held.carried[7] = "x@a"
    assert held.rt.reap_name("x@a") is True and held.reaped[-1] == [5, 7]


def test_runtime_reaps_a_holder_whose_environment_carries_another_spelling_of_the_stray_name(held):
    held.carried[5], held.carried[7] = "x-a", "y@b"
    assert held.rt.reap_name("x@a") is False and held.reaped == [[5]]
