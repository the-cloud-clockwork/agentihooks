import hashlib
import json
import os

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
    assert ledger_server.code_stamp is server_code.code_stamp
    assert server_code.CODE_DIRS[0] == server_code.ROOT / "scripts" / "swarm_ledger"
    assert [d.name for d in server_code.CODE_DIRS[1:]] == [
        "inbox",
        "swarm",
        "swarm_v2",
        "handoff",
        "doctor",
        "gates",
        "hive",
        "codex_session",
        "scheduler",
        "events",
        "hub",
        "hooks",
    ]


def test_the_record_names_the_process_its_folders_and_the_stamp_it_loaded(tmp_path):
    code = code_tree(tmp_path)
    server_code.record(tmp_path, 4242, 17, (code,))
    held = {"pid": 4242, "stamp": 17, "dirs": [str(code)], "digest": server_code.code_digest((code,))}
    assert json.loads((tmp_path / ".server.code").read_text()) == held
    assert server_code.loaded(tmp_path) == held
    assert not (tmp_path / ".server.code.4242").exists()
    server_code.record(tmp_path, 4242, 17, (code,), "loaded")
    assert server_code.loaded(tmp_path)["digest"] == "loaded"


def test_a_code_file_that_cannot_be_read_counts_as_no_change(tmp_path):
    code = code_tree(tmp_path)
    (code / "broken.py").symlink_to(tmp_path / "missing.py")
    assert server_code.code_stamp((code,)) == 30
    only = tmp_path / "only"
    only.mkdir()
    (only / "broken.py").symlink_to(tmp_path / "missing.py")
    assert server_code.code_stamp((only,)) == 0


def test_the_digest_follows_content_and_names_but_not_file_times(tmp_path):
    page = tmp_path / "page.py"
    page.write_text("old = 1\n")
    (tmp_path / "notes.txt").write_text("ignored")
    first = server_code.code_digest((tmp_path,))
    os.utime(page, ns=(5, 5_000_000_000))
    (tmp_path / "notes.txt").write_text("still ignored")
    assert server_code.code_digest((tmp_path,)) == first
    page.write_text("new = 1\n")
    changed = server_code.code_digest((tmp_path,))
    assert changed != first
    page.rename(tmp_path / "renamed.py")
    assert server_code.code_digest((tmp_path,)) not in (first, changed)


def test_the_digest_hashes_each_path_beside_its_content_hash(tmp_path):
    page = tmp_path / "page.py"
    page.write_text("old = 1\n")
    expected = hashlib.sha256(str(page).encode() + b"\0" + hashlib.sha256(b"old = 1\n").digest()).hexdigest()
    assert server_code.code_digest((tmp_path,)) == expected


def test_a_code_file_that_vanished_is_left_out_of_the_digest(tmp_path):
    (tmp_path / "kept.py").write_text("kept = 1\n")
    alone = server_code.code_digest((tmp_path,))
    (tmp_path / "gone.py").symlink_to(tmp_path / "missing.py")
    assert server_code.code_digest((tmp_path,)) == alone
    assert len(alone) == 64


def test_a_record_without_folders_is_current_while_its_stamp_is_zero(tmp_path):
    (tmp_path / ".server.code").write_text('{"pid": 4242, "stamp": 0}')
    assert not server_code.stale(tmp_path, 4242)
    (tmp_path / ".server.code").write_text('{"pid": 4242, "stamp": 5}')
    assert server_code.stale(tmp_path, 4242)


def test_only_the_server_s_own_record_counts_as_recorded(tmp_path):
    assert not server_code.recorded(tmp_path, 4242)
    server_code.record(tmp_path, 4242, 0, ())
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
    server_code.record(tmp_path, 4242, 30, (code,))
    assert not server_code.stale(tmp_path, 4242)
    assert server_code.stale(tmp_path, 4243)
    os.utime(code / "a.py", ns=(31, 31))
    assert not server_code.stale(tmp_path, 4242)
    (code / "a.py").write_text("changed = 1\n")
    os.utime(code / "a.py", ns=(31, 31))
    assert server_code.stale(tmp_path, 4242)
    os.utime(code / "a.py", ns=(29, 29))
    assert not server_code.stale(tmp_path, 4242)


def test_serve_records_the_code_it_loaded_beside_its_pid_file(monkeypatch, tmp_path):
    recorded = []

    class Server:
        def __init__(self, *args, **kwargs):
            pass

        def server_bind(self):
            pass

        def server_activate(self):
            pass

        def serve_forever(self, poll_interval):
            pass

        def drained(self, timeout):
            return True

        def server_close(self):
            pass

    monkeypatch.setattr(ledger_server.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(ledger_server.server_lifetime, "watch", lambda *args: ledger_server.threading.Event())
    monkeypatch.setattr(ledger_server.legacy, "adopt", lambda stored: None)
    monkeypatch.setattr(ledger_server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(
        ledger_server.server_code,
        "record",
        lambda *args, **kwargs: recorded.append((*args, kwargs, (tmp_path / ".server.pid").exists())),
    )
    ledger_server.serve()
    assert recorded == [
        (tmp_path, os.getpid(), ledger_server.LOADED_STAMP, {"digest": ledger_server.LOADED_DIGEST}, False)
    ]
    assert ledger_server.LOADED_STAMP > 0
