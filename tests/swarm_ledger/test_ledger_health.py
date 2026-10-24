import http.client
import inspect
import os
import signal
import socket
import urllib.error
import urllib.request

import pytest

from scripts.swarm_ledger import ledger_health as health


class Reply:
    def __init__(self):
        self.read_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        self.read_calls += 1
        return b"{}"


def test_the_probe_posts_an_empty_write_with_a_short_timeout():
    seen, reply = [], Reply()

    def urlopen(request, timeout, **options):
        seen.append((request.full_url, request.get_method(), request.data, timeout))
        return reply

    assert health.probe("http://127.0.0.1:9", urlopen) == ""
    assert seen == [("http://127.0.0.1:9/healthz", "POST", b"", 5.0)]
    assert reply.read_calls == 1
    assert health.WRITE_TIMEOUT_S == 5.0


@pytest.mark.parametrize(
    "error, named",
    [
        (TimeoutError("timed out"), "timed out"),
        (urllib.error.URLError("refused"), "<urlopen error refused>"),
        (socket.timeout(), "TimeoutError"),
        (urllib.error.URLError(ConnectionRefusedError(111, "refused")), "not listening yet"),
        (http.client.RemoteDisconnected("closed"), "closed"),
        (http.client.IncompleteRead(b""), "IncompleteRead(0 bytes read)"),
    ],
)
def test_a_slow_or_failed_probe_names_its_error(error, named):
    def urlopen(request, timeout, **options):
        raise error

    assert health.probe("http://127.0.0.1:9", urlopen) == named


def test_failures_count_up_and_a_success_clears_them(tmp_path):
    assert [health.tally(tmp_path, True) for _ in range(3)] == [1, 2, 3]
    assert (tmp_path / ".health-failures").read_text() == "0"
    assert health.tally(tmp_path, True) == 1
    assert health.tally(tmp_path, False) == 0
    assert not (tmp_path / ".health-failures").exists()
    assert health.tally(tmp_path, False) == 0
    (tmp_path / ".health-failures").write_text("junk")
    assert health.tally(tmp_path, True) == 1


def server_proc(tmp_path, pid, argv):
    (tmp_path / ".server.pid").write_text(str(pid))
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "cmdline").write_bytes(argv)
    return proc


def test_ending_the_server_kills_only_the_ledger_server_on_record(tmp_path):
    proc = server_proc(tmp_path, 4242, b"python\0/code/scripts/swarm_ledger/ledger_server.py\0--serve\0")
    killed = []
    health.end_server(tmp_path, lambda pid, sig: killed.append((pid, sig)), proc)
    assert killed == [(4242, signal.SIGKILL)]
    (proc / "4242" / "cmdline").write_bytes(b"bash\0")
    health.end_server(tmp_path, lambda pid, sig: pytest.fail("another process is never killed"), proc)
    (tmp_path / ".server.pid").write_text("gone")
    health.end_server(tmp_path, lambda pid, sig: pytest.fail("no pid, no kill"), proc)
    (tmp_path / ".server.pid").unlink()
    health.end_server(tmp_path, lambda pid, sig: pytest.fail("no pidfile, no kill"), proc)


def test_ending_a_server_that_already_exited_is_quiet(tmp_path):
    proc = server_proc(tmp_path, 4242, b"python\0ledger_server.py\0")

    def gone(pid, sig):
        raise ProcessLookupError(pid)

    health.end_server(tmp_path, gone, proc)


def run_main(tmp_path, error, killed):
    environ = {"LEDGER_DIR": str(tmp_path), "LEDGER_HOST": "127.0.0.1", "LEDGER_PORT": "9"}
    bases = []

    def probe(base):
        bases.append(base)
        return error

    code = health.main(environ, probe, lambda folder: killed.append(folder))
    assert bases == ["http://127.0.0.1:9"]
    return code


def test_a_good_probe_is_healthy_and_clears_the_count(tmp_path):
    (tmp_path / ".health-failures").write_text("2")
    killed = []
    assert run_main(tmp_path, "", killed) == 0
    assert killed == []
    assert not (tmp_path / ".health-failures").exists()


def test_the_third_failure_in_a_row_ends_the_server_and_starts_the_count_again(tmp_path, capsys):
    killed = []
    assert [run_main(tmp_path, "timed out", killed) for _ in range(2)] == [1, 1]
    assert killed == []
    assert run_main(tmp_path, "timed out", killed) == 1
    assert killed == [tmp_path]
    assert (tmp_path / ".health-failures").read_text() == "0"
    assert capsys.readouterr().err.splitlines() == [
        "probe write failed, 1 of 3: timed out",
        "probe write failed, 2 of 3: timed out",
        "probe write failed, 3 of 3: timed out",
    ]
    assert health.FAILS_MAX == 3


def test_a_server_not_listening_yet_is_unhealthy_without_counting_toward_the_kill(tmp_path, capsys):
    (tmp_path / ".health-failures").write_text("2")
    killed = []
    assert run_main(tmp_path, health.REFUSED, killed) == 1
    assert killed == []
    assert (tmp_path / ".health-failures").read_text() == "2"
    assert capsys.readouterr().err == "probe write failed: not listening yet\n"


def test_the_healthcheck_defaults_reach_the_real_server_and_process_table():
    def defaults(function):
        return {name: p.default for name, p in inspect.signature(function).parameters.items()}

    assert defaults(health.probe)["urlopen"] is urllib.request.urlopen
    assert defaults(health.end_server) == {"folder": inspect.Parameter.empty, "kill": os.kill, "proc": health.PROC}
    assert health.PROC == health.Path("/proc")
    assert defaults(health.main) == {"environ": os.environ, "check": health.probe, "end": health.end_server}
