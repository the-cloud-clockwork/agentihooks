from types import SimpleNamespace

import pytest

from scripts import terminate_agent
from scripts.swarm import runtime
from scripts.swarm.reaper import Outcome


def test_runtime_spares_a_session_whose_environment_no_longer_carries_the_stray_name(tmp_path, monkeypatch):
    held = [SimpleNamespace(name="x@a", process=SimpleNamespace(pid=p)) for p in (5, 7)]
    carried = {5: "", 7: "x@a"}
    monkeypatch.setattr(terminate_agent, "sessions", lambda: held)
    monkeypatch.setattr(terminate_agent, "agent_environ", lambda pid, keys: (carried[pid],))
    reaped = []
    rt = runtime.HerdrRuntime(home=tmp_path)
    rt.hive = SimpleNamespace(holder=lambda name: pytest.fail("a locally held name is never ordered to a hive"))
    rt.reap = lambda pids: reaped.append(pids) or Outcome(tuple(pids))
    assert rt.reap_name("x@a") is True and reaped == [[7]]
    carried[7] = "engineer@other-0002"
    assert rt.reap_name("x@a") is True and reaped[-1] == []
