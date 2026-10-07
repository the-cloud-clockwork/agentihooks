from hooks.context import swarm_pin

IDENTITY = {
    "AGENTIHOOKS_AGENT_NAME": "engineer@abcdef-0001",
    "AGENTIHOOKS_SWARM": "sw",
    "AGENTIHOOKS_SWARM_LANE": "eng",
    "AGENTIHOOKS_SWARM_TASK": "t1",
    "AGENTIHOOKS_SWARM_LAUNCHER": "10",
}


def _proc(root, pid, comm, ppid):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0\n")


def _tree(tmp_path):
    root = tmp_path / "proc"
    _proc(root, 9, "herdr", 1)
    _proc(root, 10, "bash", 9)
    _proc(root, 11, "agentihooks", 10)
    _proc(root, 12, "claude", 11)
    _proc(root, 13, "bash", 12)
    _proc(root, 14, "python3", 13)
    _proc(root, 20, "bash", 12)
    _proc(root, 21, "codex", 20)
    _proc(root, 22, "codex-code-mode", 21)
    _proc(root, 23, "sh", 22)
    _proc(root, 24, "python3", 23)
    _proc(root, 30, "bash", 1)
    _proc(root, 31, "codex", 30)
    _proc(root, 32, "codex", 31)
    _proc(root, 33, "sh", 32)
    _proc(root, 34, "python3", 33)
    return root


def test_the_launched_session_keeps_its_identity(tmp_path):
    assert not swarm_pin.nested(IDENTITY, 13, _tree(tmp_path))


def test_a_session_started_inside_the_agent_is_nested(tmp_path):
    assert swarm_pin.nested(IDENTITY, 23, _tree(tmp_path))


def test_a_session_outside_the_launcher_is_nested(tmp_path):
    proc = _tree(tmp_path)
    assert swarm_pin.nested(IDENTITY, 33, proc)
    assert not swarm_pin.nested({**IDENTITY, "AGENTIHOOKS_SWARM_LAUNCHER": "30"}, 33, proc)


def test_an_unpinned_or_unreadable_session_keeps_its_identity(tmp_path):
    unpinned = {k: v for k, v in IDENTITY.items() if k != "AGENTIHOOKS_SWARM_LAUNCHER"}
    assert not swarm_pin.nested(unpinned, 23, _tree(tmp_path))
    assert not swarm_pin.nested(IDENTITY, 23, tmp_path / "absent")


def test_unpin_drops_the_identity_of_a_nested_session_only(tmp_path):
    environ = {**IDENTITY, "AGENTIHOOKS_SWARM_REDIS_URL": "redis://r"}
    proc = _tree(tmp_path)
    assert not swarm_pin.unpin(environ, 13, proc)
    assert environ["AGENTIHOOKS_SWARM"] == "sw"
    assert swarm_pin.unpin(environ, 23, proc)
    assert environ == {"AGENTIHOOKS_SWARM_REDIS_URL": "redis://r"}
