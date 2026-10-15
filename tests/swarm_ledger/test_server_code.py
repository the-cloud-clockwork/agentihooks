import json
import os

import pytest

from scripts.swarm_ledger import ledger_server, server_code


def code_tree(tmp_path):
    code = tmp_path / "code"
    (code / "nested").mkdir(parents=True)
    for name, stamp in (("a.py", 10), ("nested/b.html", 30), ("c.js", 20), ("d.css", 25), ("e.txt", 90), ("f.pyc", 80)):
        (code / name).touch()
        os.utime(code / name, ns=(stamp, stamp))
    return code


def test_the_stamp_is_the_newest_code_file_and_ignores_other_files(tmp_path):
    code = code_tree(tmp_path)
    assert server_code.code_stamp((code,)) == 30
    (tmp_path / "empty").mkdir()
    assert server_code.code_stamp((tmp_path / "empty",)) == 0


def test_the_server_and_the_swarm_share_one_list_of_code_folders():
    assert ledger_server.CODE_DIRS is server_code.CODE_DIRS
    assert ledger_server.server_code is server_code
    assert server_code.CODE_DIRS[0] == server_code.ROOT / "scripts" / "swarm_ledger"
    assert [d.name for d in server_code.CODE_DIRS[1:]] == [
        "inbox",
        "swarm",
        "swarm_v2",
        "handoff",
        "doctor",
        "gates",
        "hive",
        "hooks",
    ]


def test_the_record_names_the_process_its_folders_and_the_code_it_loaded(tmp_path):
    code = code_tree(tmp_path)
    server_code.record(tmp_path, 4242, {"stamp": 17, "digest": "abc"}, (code,))
    held = {"pid": 4242, "stamp": 17, "digest": "abc", "dirs": [str(code)]}
    assert json.loads((tmp_path / ".server.code").read_text()) == held
    assert server_code.loaded(tmp_path) == held
    assert not (tmp_path / ".server.code.4242").exists()


def test_a_code_file_that_cannot_be_read_counts_as_no_change(tmp_path):
    code = code_tree(tmp_path)
    (code / "broken.py").symlink_to(tmp_path / "missing.py")
    assert server_code.code_stamp((code,)) == 30
    only = tmp_path / "only"
    only.mkdir()
    (only / "broken.py").symlink_to(tmp_path / "missing.py")
    assert server_code.code_stamp((only,)) == 0


def test_a_record_without_folders_is_current_while_its_stamp_is_zero(tmp_path):
    (tmp_path / ".server.code").write_text('{"pid": 4242, "stamp": 0}')
    assert not server_code.stale(tmp_path, 4242)
    (tmp_path / ".server.code").write_text('{"pid": 4242, "stamp": 5}')
    assert server_code.stale(tmp_path, 4242)


def test_only_the_server_s_own_record_counts_as_recorded(tmp_path):
    assert not server_code.recorded(tmp_path, 4242)
    server_code.record(tmp_path, 4242, server_code.loaded_code(()), ())
    assert server_code.recorded(tmp_path, 4242)
    assert not server_code.recorded(tmp_path, 4243)


def test_a_missing_broken_or_odd_record_reads_as_nothing(tmp_path):
    assert server_code.loaded(tmp_path) is None
    (tmp_path / ".server.code").write_text("{not json")
    assert server_code.loaded(tmp_path) is None
    (tmp_path / ".server.code").write_text("[1, 2]")
    assert server_code.loaded(tmp_path) is None


def test_a_server_is_current_only_while_its_own_record_matches_the_code_on_disk(tmp_path):
    code = code_tree(tmp_path)
    assert server_code.stale(tmp_path, 4242)
    server_code.record(tmp_path, 4242, server_code.loaded_code((code,)), (code,))
    assert not server_code.stale(tmp_path, 4242)
    assert server_code.stale(tmp_path, 4243)
    (code / "a.py").write_text("changed = True\n")
    assert server_code.stale(tmp_path, 4242)
    (code / "a.py").write_text("")
    assert not server_code.stale(tmp_path, 4242)


def test_file_times_that_move_without_a_content_change_keep_the_server_current(tmp_path, monkeypatch):
    code = code_tree(tmp_path)
    server_code.record(tmp_path, 4242, server_code.loaded_code((code,)), (code,))
    (code / "c.js").write_text("")
    os.utime(code / "a.py", ns=(99, 99))
    assert server_code.code_stamp((code,)) != server_code.loaded(tmp_path)["stamp"]
    assert not server_code.stale(tmp_path, 4242)
    assert server_code.loaded(tmp_path)["stamp"] == server_code.code_stamp((code,))
    monkeypatch.setattr(server_code, "code_digest", lambda dirs: pytest.fail("digest read again"))
    assert not server_code.stale(tmp_path, 4242)


def test_the_digest_follows_code_content_and_names_only(tmp_path):
    code = code_tree(tmp_path)
    digest = server_code.code_digest((code,))
    os.utime(code / "a.py", ns=(99, 99))
    (code / "e.txt").write_text("not code")
    (code / "f.pyc").write_bytes(b"\0")
    assert server_code.code_digest((code,)) == digest
    (code / "nested" / "b.html").write_text("<p>new</p>")
    changed = server_code.code_digest((code,))
    assert changed != digest
    (code / "nested" / "b.html").write_text("")
    assert server_code.code_digest((code,)) == digest
    (code / "g.py").write_text("")
    assert server_code.code_digest((code,)) != digest
    (code / "g.py").unlink()
    (code / "d.css").rename(code / "h.css")
    assert server_code.code_digest((code,)) != digest


def test_the_digest_skips_an_unreadable_file_and_still_reads_the_ones_after_it(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "a.py").symlink_to(tmp_path / "missing.py")
    (code / "b.py").write_text("old")
    before = server_code.code_digest((code,))
    (code / "b.py").write_text("new")
    assert server_code.code_digest((code,)) != before


def test_the_same_content_moves_the_held_stamp_and_a_change_leaves_it(tmp_path):
    code = code_tree(tmp_path)
    held = server_code.loaded_code((code,))
    assert held == {"stamp": 30, "digest": server_code.code_digest((code,))}
    os.utime(code / "a.py", ns=(40, 40))
    assert server_code.same(held, (code,)) and held["stamp"] == 40
    (code / "a.py").write_text("changed = True\n")
    os.utime(code / "a.py", ns=(50, 50))
    assert not server_code.same(held, (code,)) and held["stamp"] == 40


def test_serve_records_the_code_it_loaded_beside_its_pid_file(monkeypatch, tmp_path):
    recorded = []

    class Server:
        replacing = False

        def __init__(self, *args, **kwargs):
            pass

        def server_bind(self):
            pass

        def server_activate(self):
            pass

        def stop_on(self, signum):
            pass

        def serve_forever(self):
            pass

        def drain(self, hub):
            pass

        def replace(self):
            pass

    monkeypatch.setattr(ledger_server.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(ledger_server.server_lifetime, "watch", lambda *args: ledger_server.threading.Event())
    monkeypatch.setattr(ledger_server.legacy, "adopt", lambda stored: None)
    monkeypatch.setattr(ledger_server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(
        ledger_server.server_code,
        "record",
        lambda *args: recorded.append((*args, (tmp_path / ".server.pid").exists())),
    )
    ledger_server.serve()
    assert recorded == [(tmp_path, os.getpid(), ledger_server.LOADED, False)]
    assert ledger_server.LOADED["stamp"] > 0 and len(ledger_server.LOADED["digest"]) == 64
