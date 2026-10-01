from pathlib import Path

from hooks.proc import Process, _process, _target, processes


def fake_proc(root: Path, pid: int, *, comm="claude", argv=("claude",), ppid=1, start=4242, state="S") -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    rest = [state, str(ppid), str(pid + 1), str(pid + 2)] + ["0"] * 15 + [str(start), "0", "0"]
    (entry / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n", encoding="utf-8")
    (entry / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
    (entry / "comm").write_text(comm + "\n", encoding="utf-8")


def item(comm="node", argv=()):
    return Process(1, 0, 1, 1, 1, "S", comm, tuple(argv))


def test_process_reads_identity_fields(tmp_path):
    fake_proc(tmp_path, 300, argv=("claude", "--name", "eng"), ppid=7, start=987654)
    assert _process(300, tmp_path) == Process(300, 7, 301, 302, 987654, "S", "claude", ("claude", "--name", "eng"))


def test_process_comm_with_spaces_and_parens(tmp_path):
    fake_proc(tmp_path, 301, comm="my (odd) name", argv=("x",), start=55)
    found = _process(301, tmp_path)
    assert (found.ppid, found.start_time, found.comm) == (1, 55, "my (odd) name")


def test_process_missing_is_none(tmp_path):
    assert _process(999, tmp_path) is None


def test_processes_skips_non_numeric_and_broken(tmp_path):
    fake_proc(tmp_path, 400)
    fake_proc(tmp_path, 401)
    (tmp_path / "self").mkdir()
    (tmp_path / "402").mkdir()
    assert sorted(processes(tmp_path)) == [400, 401]


def test_processes_missing_root_is_empty(tmp_path):
    assert processes(tmp_path / "absent") == {}


def test_target_detects_claude_and_codex():
    assert _target(item(comm="claude")) == "claude"
    assert _target(item(argv=("node", "/opt/x/claude-code/cli.js"))) == "claude"
    assert _target(item(argv=("/usr/bin/codex",))) == "codex"
    assert _target(item(comm="codex-linux-x64")) == "codex"
    assert _target(item(comm="bash", argv=("bash", "-c", "claude"))) == ""
