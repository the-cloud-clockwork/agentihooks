from hooks.context import swarm_pin

IDENTITY = {
    "AGENTIHOOKS_AGENT_NAME": "engineer@abcdef-0001",
    "AGENTIHOOKS_SWARM": "sw",
    "AGENTIHOOKS_SWARM_LANE": "eng",
    "AGENTIHOOKS_SWARM_TASK": "t1",
    "AGENTIHOOKS_SWARM_LAUNCHER": "10",
}


def _proc(root, pid, comm, ppid, sid=0):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 {sid}\n")


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
    _proc(root, 40, "codex", 12)
    _proc(root, 41, "sh", 40)
    return root


def test_the_launched_session_keeps_its_identity(tmp_path):
    assert not swarm_pin.nested(IDENTITY, 13, _tree(tmp_path))


def test_a_session_started_inside_the_agent_is_nested(tmp_path):
    proc = _tree(tmp_path)
    assert swarm_pin.nested(IDENTITY, 23, proc)
    assert swarm_pin.nested(IDENTITY, 41, proc)


def test_a_session_outside_the_launcher_is_nested(tmp_path):
    proc = _tree(tmp_path)
    assert swarm_pin.nested(IDENTITY, 33, proc)
    assert not swarm_pin.nested({**IDENTITY, "AGENTIHOOKS_SWARM_LAUNCHER": "30"}, 33, proc)


def test_a_process_outside_the_launcher_with_no_agent_above_it_is_nested(tmp_path):
    assert swarm_pin.nested(IDENTITY, 30, _tree(tmp_path))


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


def test_unpin_tolerates_an_identity_already_partial(tmp_path):
    environ = {k: v for k, v in IDENTITY.items() if k != "AGENTIHOOKS_SWARM_LANE"}
    assert swarm_pin.unpin(environ, 23, _tree(tmp_path))
    assert environ == {}


def _orphan(tmp_path, sid):
    root = _tree(tmp_path)
    _proc(root, 50, "claude", 1, sid)
    _proc(root, 51, "sh", 50, 51)
    _proc(root, 52, "python3", 51, 51)
    return root


def test_the_launched_session_keeps_its_identity_after_its_wrapper_dies(tmp_path):
    assert not swarm_pin.nested(IDENTITY, 52, _orphan(tmp_path, 10))


def test_an_orphan_outside_the_launcher_session_is_nested(tmp_path):
    assert swarm_pin.nested(IDENTITY, 52, _orphan(tmp_path, 30))


def test_an_orphan_sharing_the_launcher_session_with_another_agent_is_nested(tmp_path):
    proc = _orphan(tmp_path, 10)
    _proc(proc, 70, "codex", 1, 10)
    assert swarm_pin.nested(IDENTITY, 52, proc)


def test_an_orphaned_codex_keeps_its_identity_across_its_own_wrapper(tmp_path):
    root = _tree(tmp_path)
    _proc(root, 60, "codex", 1, 10)
    _proc(root, 61, "codex", 60, 10)
    _proc(root, 62, "sh", 61, 10)
    assert not swarm_pin.nested(IDENTITY, 62, root)


def test_a_session_started_inside_an_orphaned_agent_is_nested(tmp_path):
    root = _tree(tmp_path)
    _proc(root, 60, "codex", 1, 10)
    _proc(root, 61, "bash", 60, 61)
    _proc(root, 62, "claude", 61, 61)
    _proc(root, 63, "sh", 62, 63)
    assert swarm_pin.nested(IDENTITY, 63, root)


def test_an_orphan_scan_skips_processes_it_cannot_read(tmp_path):
    proc = _orphan(tmp_path, 10)
    (proc / "80").mkdir()
    (proc / "81").mkdir()
    (proc / "81" / "stat").write_text("81 (codex) S\n")
    (proc / "82").mkdir()
    (proc / "82" / "stat").write_text("82 (codex) S x 0 10\n")
    (proc / "self").mkdir()
    assert not swarm_pin.nested(IDENTITY, 52, proc)


def test_an_orphan_scan_keeps_an_opening_parenthesis_inside_the_command_name(tmp_path):
    proc = _orphan(tmp_path, 10)
    (proc / "70").mkdir()
    (proc / "70" / "stat").write_text("70 (x(claude) S 1 70 10\n")
    assert not swarm_pin.nested(IDENTITY, 52, proc)


def test_an_orphan_scan_ends_the_command_name_at_the_last_closing_parenthesis(tmp_path):
    root = _tree(tmp_path)
    _proc(root, 50, "claude", 1, 4)
    _proc(root, 51, "sh", 50, 51)
    _proc(root, 52, "python3", 51, 51)
    (root / "71").mkdir()
    (root / "71" / "stat").write_text("71 (claude)1 2 3 4 ) S 2 71 4\n")
    assert not swarm_pin.nested({**IDENTITY, "AGENTIHOOKS_SWARM_LAUNCHER": "4"}, 52, root)
