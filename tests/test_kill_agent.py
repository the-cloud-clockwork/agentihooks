import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.kill_agent import Process, Session, main, resolve, validate


def process(pid, *, ppid=1, pgid=None, sid=None, start=100, comm="claude", argv=()):
    group = pid if pgid is None else pgid
    return Process(pid, ppid, group, group if sid is None else sid, start, "S", comm, tuple(argv))


def session(session_id="uuid-1", name="engineer", item=None, target="claude"):
    item = item or process(200, pgid=190, sid=190, argv=("claude", "--name", name))
    return Session(session_id, target, name, item, "/repo", "alive")


def test_resolve_exact_name_uuid_and_pid():
    item = session()
    assert resolve([item], "engineer", "any") == item
    assert resolve([item], "uuid-1", "claude") == item
    assert resolve([item], "200", "claude") == item


def test_resolve_duplicate_name_requires_uuid():
    items = [session("uuid-1", item=process(200)), session("uuid-2", item=process(201))]
    with pytest.raises(ValueError, match="ambiguous"):
        resolve(items, "engineer", "claude")


def test_validate_rejects_shared_process_without_override(monkeypatch):
    item = session()
    other = session("uuid-2")
    table = {
        100: process(100, comm="python", argv=("python",)),
        190: process(190, pgid=190, sid=190, comm="bash", argv=("bash", "launcher")),
        200: item.process,
    }
    monkeypatch.setattr("scripts.kill_agent.processes", lambda proc=Path("/proc"): table)
    monkeypatch.setattr("scripts.kill_agent.os.getpid", lambda: 100)
    with pytest.raises(ValueError, match="shared"):
        validate(item, [item, other])


def test_main_dry_run_sends_no_signal(monkeypatch, capsys):
    item = session(name="engineer-270926-2337-b")
    monkeypatch.setattr("scripts.kill_agent.sessions", lambda: [item])
    monkeypatch.setattr("scripts.kill_agent.validate", lambda selected, items, force_shared=False: [selected.process])
    with patch("scripts.kill_agent.os.killpg") as killpg:
        assert main(["engineer-270926-2337-b", "--type", "claude", "--dry-run"]) == 0
    killpg.assert_not_called()
    assert "result=validated signal=none" in capsys.readouterr().out


def test_main_terminates_after_validation(monkeypatch, capsys):
    item = session()
    monkeypatch.setattr("scripts.kill_agent.sessions", lambda: [item])
    monkeypatch.setattr("scripts.kill_agent.validate", lambda selected, items, force_shared=False: [selected.process])
    monkeypatch.setattr("scripts.kill_agent.terminate", lambda selected, members, timeout: True)
    assert main(["uuid-1", "--type", "claude"]) == 0
    assert "result=terminated escalation=SIGKILL" in capsys.readouterr().out


def test_terminate_uses_process_group(monkeypatch):
    from scripts.kill_agent import terminate

    item = session()
    calls = []
    monkeypatch.setattr("scripts.kill_agent._alive", lambda identities, proc: [])
    monkeypatch.setattr("scripts.kill_agent.os.killpg", lambda pgid, sig: calls.append((pgid, sig)))
    assert terminate(item, [item.process], 0.1) is False
    assert calls == [(190, signal.SIGTERM)]


def test_terminate_real_isolated_process_group():
    from scripts.kill_agent import _process, terminate

    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        time.sleep(0.05)
        item = _process(child.pid, Path("/proc"))
        assert item is not None
        target = Session("probe", "claude", "probe", item, "", "alive")
        assert terminate(target, [item], 0.5) is False
        assert child.wait(timeout=1) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
