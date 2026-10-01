import json
import os

from hooks.lifecycle.liveness import holder_alive, owner_holder, path_in_use, take_snapshot
from hooks.lifecycle.model import Holder

from .conftest import process, snap


def fake_proc(root, pid, *, comm="claude", ppid=1, start=77, cwd=None):
    entry = root / str(pid)
    entry.mkdir(parents=True)
    fields = ["S", str(ppid), str(pid), str(pid)] + ["0"] * 15 + [str(start), "0"]
    (entry / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))
    (entry / "cmdline").write_bytes(comm.encode() + b"\0")
    (entry / "comm").write_text(comm)
    if cwd:
        os.symlink(cwd, entry / "cwd")


def test_snapshot_reads_boot_uptime_cwds_and_verified_sessions(tmp_path):
    proc, sessions, work = tmp_path / "proc", tmp_path / "sessions", tmp_path / "work"
    work.mkdir()
    sessions.mkdir()
    fake_proc(proc, 500, start=77, cwd=work)
    fake_proc(proc, 501, start=88)
    (proc / "uptime").write_text("1234.50 99.0\n")
    (proc / "sys" / "kernel" / "random").mkdir(parents=True)
    (proc / "sys" / "kernel" / "random" / "boot_id").write_text("boot-x\n")
    (sessions / "500.json").write_text(json.dumps({"pid": 500, "procStart": "77", "sessionId": "s-live"}))
    (sessions / "501.json").write_text(json.dumps({"pid": 501, "procStart": "11", "sessionId": "s-reused-pid"}))
    (sessions / "502.json").write_text(json.dumps({"pid": 502, "sessionId": "s-no-procstart", "status": "busy"}))
    taken = take_snapshot(proc, sessions)
    assert (taken.boot_id, taken.uptime) == ("boot-x", 1234.5)
    assert taken.sessions == {500: "s-live"}
    assert str(work) in taken.cwds


def test_holder_alive_needs_matching_start_and_boot_or_a_live_session():
    view = snap(table={10: process(10, start=5)}, sessions={99: "s-1"})
    assert holder_alive(Holder("", 10, 5, "boot-1"), view)
    assert not holder_alive(Holder("", 10, 6, "boot-1"), view)
    assert not holder_alive(Holder("", 10, 5, "boot-0"), view)
    assert not holder_alive(Holder("", 11, 5, "boot-1"), view)
    assert holder_alive(Holder("s-1", 3, 3, "boot-0"), view)


def test_path_in_use_matches_prefix_not_substring():
    view = snap(cwds=("/w/task-1/src", "/w/task-10"))
    assert path_in_use("/w/task-1", view)
    assert path_in_use("/w/task-10", view)
    assert not path_in_use("/w/task", view)


def test_owner_holder_walks_up_to_the_agent_process():
    table = {
        10: process(10, start=5),
        20: process(20, ppid=10, comm="bash"),
        30: process(30, ppid=20, comm="python3"),
        40: process(40, ppid=1, comm="codex"),
        50: process(50, ppid=40, comm="bash"),
        60: process(60, ppid=1, comm="bash"),
    }
    view = snap(table=table, sessions={10: "s-1"})
    assert owner_holder(view, 30) == Holder("s-1", 10, 5, "boot-1")
    assert owner_holder(view, 50) == Holder("", 40, 100, "boot-1")
    assert owner_holder(view, 60) is None
