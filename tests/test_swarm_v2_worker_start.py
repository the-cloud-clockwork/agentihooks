import functools
import http.client
import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.swarm_v2 import filesystem, worker_home
from scripts.swarm_v2.broadcast_bridge import API_URL, GRANT_NAME
from scripts.swarm_v2.supervision import Launch, LaunchRefused
from scripts.swarm_v2.worker import start

pytestmark = pytest.mark.unit

EXECUTION = "exe-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
GRANT = "sv2.grant-fixture"
CONTROL = "http://controller.swarm.invalid:8780"
AUTHORITY = {
    "execution_id": EXECUTION,
    "generation": 3,
    "task_id": "t1",
    "seat_id": "eng-1@fixture",
    "swarm_id": "fixture",
    "grant_id": "lgr-" + "7" * 32,
}
RECORD = {
    "schema_version": 1,
    "execution_id": EXECUTION,
    "generation": 3,
    "authority": AUTHORITY,
    "harness": "claude",
    "agent": ["claude"],
    "control_url": CONTROL,
}
ANSWER = {
    "execution_id": EXECUTION,
    "task_id": "t1",
    "task_generation": 1,
    "grant_id": AUTHORITY["grant_id"],
    "registered_at": "2026-10-11T00:00:00Z",
}


def material(tmp_path: Path, record: object = RECORD, grant: str | None = GRANT) -> Path:
    folder = tmp_path / "launch"
    folder.mkdir()
    path = folder / "launch.json"
    path.write_text(record if isinstance(record, str) else json.dumps(record))
    if grant is not None:
        (folder / GRANT_NAME).write_text(grant + "\n")
    return path


class Controller:
    def __init__(self, *answers: tuple[int, dict]) -> None:
        self.answers, self.calls, self.sleeps = list(answers), [], []

    def send(self, url: str, grant: str, body: dict) -> tuple[int, dict]:
        self.calls.append((url, grant, body))
        return self.answers.pop(0)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class Boot:
    def __init__(self) -> None:
        self.requests = []

    def __call__(self, request: worker_home.Request) -> dict:
        self.requests.append(request)
        (request.root / request.attempt / "run").mkdir(parents=True)
        return {}


def prepare(tmp_path, launch, controller, boot=None):
    boot = boot or Boot()
    start.prepare(tmp_path / "attempts" / EXECUTION, launch, controller.send, controller.sleep, boot)
    return boot


def refusal(tmp_path, launch, controller=None) -> str:
    controller = controller or Controller((200, ANSWER))
    boot = Boot()
    with pytest.raises(LaunchRefused) as refused:
        prepare(tmp_path, launch, controller, boot)
    assert boot.requests == []
    assert not (tmp_path / "attempts").exists()
    return str(refused.value)


def test_a_prepared_attempt_holds_the_registration_record_only_the_worker_can_read(tmp_path):
    controller = Controller((200, ANSWER))

    boot = prepare(tmp_path, material(tmp_path), controller)

    attempt = tmp_path / "attempts" / EXECUTION
    record = attempt / "registration.json"
    assert json.loads(record.read_text()) == AUTHORITY
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "attempts").stat().st_mode) == 0o700
    assert (attempt / "run" / GRANT_NAME).read_text() == GRANT
    assert not (attempt / "registration.json.tmp").exists()
    assert controller.calls == [(CONTROL, GRANT, {"execution_id": EXECUTION, "generation": 3})]
    assert controller.sleeps == []
    assert boot.requests == [
        worker_home.Request(
            tmp_path / "attempts",
            EXECUTION,
            Path("/opt/agentihooks/templates"),
            {"claude": "default"},
            Path(sys.executable),
            {},
            {API_URL: CONTROL},
            os.getuid(),
            os.getgid(),
        )
    ]


def test_a_busy_controller_is_asked_again_until_it_answers(tmp_path):
    controller = Controller((503, {}), (503, {}), (200, ANSWER))

    prepare(tmp_path, material(tmp_path), controller)

    assert len(controller.calls) == 3
    assert controller.sleeps == [2, 2]


def test_a_controller_busy_on_every_attempt_refuses_the_start(tmp_path):
    controller = Controller(*[(503, {})] * 10)

    assert refusal(tmp_path, material(tmp_path), controller) == "registration refused with status 503"
    assert len(controller.calls) == 10
    assert controller.sleeps == [2] * 9


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ("{not json", "missing or unreadable launch record"),
        ([], "unsupported launch"),
        ({**RECORD, "schema_version": 2}, "unsupported launch"),
        ({key: value for key, value in RECORD.items() if key != "authority"}, "invalid authority"),
        ({**RECORD, "authority": {**AUTHORITY, "grant_id": "forged"}}, "invalid authority"),
        ({**RECORD, "harness": "vim"}, "unsupported harness"),
        ({key: value for key, value in RECORD.items() if key != "control_url"}, "invalid control url"),
        ({**RECORD, "control_url": "file:///etc/passwd"}, "invalid control url"),
        ({**RECORD, "control_url": "http://"}, "invalid control url"),
        ({**RECORD, "control_url": 8780}, "invalid control url"),
        ({**RECORD, "control_url": "http://controller.swarm.invalid:port"}, "invalid control url"),
    ],
)
def test_a_malformed_launch_record_refuses_before_anything_is_written(tmp_path, record, message):
    assert refusal(tmp_path, material(tmp_path, record)) == message


def test_a_missing_launch_record_refuses(tmp_path):
    assert refusal(tmp_path, tmp_path / "launch" / "launch.json") == "missing or unreadable launch record"


@pytest.mark.parametrize("grant", [None, "  "])
def test_a_missing_launch_grant_refuses(tmp_path, grant):
    assert refusal(tmp_path, material(tmp_path, grant=grant)) == "missing launch grant"


def test_an_attempt_folder_for_another_execution_refuses(tmp_path):
    launch = material(tmp_path, {**RECORD, "authority": {**AUTHORITY, "execution_id": "exe-" + "1" * 32}})

    assert refusal(tmp_path, launch) == "attempt folder does not name the launch execution"


def test_a_refused_registration_refuses_the_start(tmp_path):
    controller = Controller((401, {}))

    assert refusal(tmp_path, material(tmp_path), controller) == "registration refused with status 401"
    assert controller.sleeps == []


@pytest.mark.parametrize("field", ["execution_id", "task_id", "grant_id"])
def test_an_answer_for_another_registration_refuses(tmp_path, field):
    controller = Controller((200, {**ANSWER, field: "other"}))

    assert (
        refusal(tmp_path, material(tmp_path), controller) == "registration answer does not match the launch authority"
    )


def test_an_answer_that_is_not_an_object_refuses(tmp_path):
    controller = Controller((200, []))

    assert (
        refusal(tmp_path, material(tmp_path), controller) == "registration answer does not match the launch authority"
    )


def test_the_start_runs_the_supervisor_on_the_prepared_attempt_under_a_private_umask(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(start.os, "umask", lambda mask: calls.append(("umask", mask)) or 0o022)
    monkeypatch.setattr(start, "prepare", lambda attempt, launch: calls.append(("prepare", attempt, launch)))
    monkeypatch.setattr(start.supervision_runtime, "main", lambda argv: calls.append(("supervise", argv)) or 70)

    assert start.main(["/home/worker/attempts/a", "/var/run/swarm/launch/launch.json"]) == 70

    assert calls == [
        ("umask", 0o077),
        ("prepare", Path("/home/worker/attempts/a"), Path("/var/run/swarm/launch/launch.json")),
        ("supervise", ["/home/worker/attempts/a", "/var/run/swarm/launch/launch.json"]),
    ]


def test_a_refused_start_never_runs_the_supervisor(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(start.os, "umask", lambda mask: 0o022)
    monkeypatch.setattr(start.supervision_runtime, "main", lambda argv: pytest.fail("supervisor ran"))

    assert start.main([str(tmp_path / "attempts" / EXECUTION), str(tmp_path / "missing.json")]) == 64

    assert capsys.readouterr().err == "ERROR worker start refused: missing or unreadable launch record\n"
    assert not (tmp_path / "attempts").exists()


def test_a_failed_bootstrap_refuses_the_start(tmp_path, monkeypatch, capsys):
    def broken(attempt, launch):
        raise worker_home.BootstrapError("profile template not found: default")

    monkeypatch.setattr(start.os, "umask", lambda mask: 0o022)
    monkeypatch.setattr(start, "prepare", broken)

    assert start.main(["a", "b"]) == 64

    assert capsys.readouterr().err == "ERROR worker start refused: profile template not found: default\n"


@pytest.mark.parametrize("argv", [[], ["a"], ["a", "b", "c"]])
def test_the_start_needs_an_attempt_and_a_launch_record(argv, capsys):
    assert start.main(argv) == 64

    assert capsys.readouterr().err == "ERROR worker start requires attempt directory and launch record\n"


class Recorder(BaseHTTPRequestHandler):
    status, answer, seen = 200, b"{}", []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append((self.path, self.headers["Authorization"], self.headers["Content-Type"], body))
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(type(self).answer)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_post_registers_with_the_grant_as_bearer(server, monkeypatch):
    monkeypatch.setattr(Recorder, "status", 200)
    monkeypatch.setattr(Recorder, "answer", json.dumps(ANSWER).encode())

    assert start.post(server, GRANT, {"execution_id": EXECUTION, "generation": 3}) == (200, ANSWER)

    assert Recorder.seen == [
        (
            "/v2/executions/register",
            f"Bearer {GRANT}",
            "application/json",
            json.dumps({"execution_id": EXECUTION, "generation": 3}).encode(),
        )
    ]


def test_post_registers_on_the_same_path_for_a_slash_terminated_control_url(server, monkeypatch):
    monkeypatch.setattr(Recorder, "answer", json.dumps(ANSWER).encode())

    assert start.post(server + "/", GRANT, {}) == (200, ANSWER)

    assert [seen[0] for seen in Recorder.seen] == ["/v2/executions/register"]


def test_post_reports_a_refusal_status_without_its_body(server, monkeypatch):
    monkeypatch.setattr(Recorder, "status", 401)
    monkeypatch.setattr(Recorder, "answer", b'{"error_class": "unauthenticated"}')

    assert start.post(server, GRANT, {}) == (401, {})


def test_post_reports_an_unreadable_answer_as_empty(server, monkeypatch):
    monkeypatch.setattr(Recorder, "status", 200)
    monkeypatch.setattr(Recorder, "answer", b"not json")

    assert start.post(server, GRANT, {}) == (200, {})


def test_post_reports_an_unreachable_controller_as_busy(server):
    closed = server.rsplit(":", 1)[0] + ":1"

    assert start.post(closed, GRANT, {}) == (503, {})


def test_an_https_control_url_reaches_the_controller_and_the_worker_settings(tmp_path):
    secure = "https://controller.swarm.invalid"
    controller = Controller((200, ANSWER))

    boot = prepare(tmp_path, material(tmp_path, {**RECORD, "control_url": secure}), controller)

    assert controller.calls[0][0] == secure
    assert boot.requests[0].endpoints == {API_URL: secure}


def test_a_stale_staged_record_never_loosens_the_registration_record(tmp_path):
    staged = tmp_path / "attempts" / EXECUTION / "registration.json.tmp"
    staged.parent.mkdir(parents=True)
    staged.write_text("stale")
    staged.chmod(0o644)

    prepare(tmp_path, material(tmp_path), Controller((200, ANSWER)))

    record = staged.with_name("registration.json")
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert json.loads(record.read_text()) == AUTHORITY
    assert not staged.exists()


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (OSError("disk full"), "disk full"),
        (KeyError("homes"), "'homes'"),
        (TypeError("bad record"), "bad record"),
        (subprocess.TimeoutExpired("python", 30), "Command 'python' timed out after 30 seconds"),
    ],
)
def test_any_start_failure_refuses_without_the_supervisor(monkeypatch, capsys, error, message):
    def broken(attempt, launch):
        raise error

    monkeypatch.setattr(start.os, "umask", lambda mask: 0o022)
    monkeypatch.setattr(start, "prepare", broken)
    monkeypatch.setattr(start.supervision_runtime, "main", lambda argv: pytest.fail("supervisor ran"))

    assert start.main(["a", "b"]) == 64

    assert tuple(capsys.readouterr()) == ("", f"ERROR worker start refused: {message}\n")


def test_post_waits_at_most_its_timeout_for_the_controller(monkeypatch):
    seen = []

    def unreachable(request, timeout):
        seen.append(timeout)
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(start.urllib.request, "urlopen", unreachable)

    assert start.post(CONTROL, GRANT, {}) == (503, {})
    assert seen == [10]


def test_post_reports_a_broken_http_answer_as_busy(monkeypatch):
    def broken(request, timeout):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(start.urllib.request, "urlopen", broken)

    assert start.post(CONTROL, GRANT, {}) == (503, {})


class Bootstrap(Boot):
    def __call__(self, request: worker_home.Request) -> dict:
        super().__call__(request)
        attempt = request.root / request.attempt
        (attempt / "homes" / "claude").mkdir(parents=True)
        layout = filesystem.mapping(filesystem.load())
        record = {"attempt": request.attempt, "homes": {"claude": "homes/claude"}, "layout": layout}
        (attempt / "execution.json").write_text(json.dumps(record))
        return record


@pytest.fixture
def umask():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def test_the_started_supervisor_loads_a_prepared_attempt_only_the_worker_can_read(tmp_path, monkeypatch, capsys, umask):
    launch = material(tmp_path)
    attempt = tmp_path / "attempts" / EXECUTION
    controller = Controller((200, ANSWER))
    loaded = []
    prepared = functools.partial(start.prepare, send=controller.send, sleep=controller.sleep, boot=Bootstrap())
    monkeypatch.setattr(start, "prepare", prepared)
    monkeypatch.setattr(
        start.supervision_runtime, "main", lambda argv: loaded.append(Launch.load(*map(Path, argv))) or 70
    )

    assert start.main([str(attempt), str(launch)]) == 70

    [run] = loaded
    assert (run.authority, run.harness, run.home) == (AUTHORITY, "claude", attempt.resolve() / "homes" / "claude")
    for path in (attempt / "execution.json", attempt / "registration.json", attempt / "run" / GRANT_NAME):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE(attempt.parent.stat().st_mode) == 0o700
    printed = {"worker_start": "prepared", "attempt": EXECUTION, "uid": os.getuid()}
    assert capsys.readouterr().out == json.dumps(printed) + "\n"


def test_the_image_supervisor_entry_is_the_worker_start_step():
    entry = Path(__file__).resolve().parents[1] / "docker" / "swarm-node" / "supervisor.py"
    spec = importlib.util.spec_from_file_location("swarm_node_supervisor", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main is start.main


def test_the_image_supervisor_entry_runs_the_start_step_as_a_script():
    root = Path(__file__).resolve().parents[1]
    entry = root / "docker" / "swarm-node" / "supervisor.py"
    environ = {**os.environ, "PYTHONPATH": str(root)}

    done = subprocess.run([sys.executable, str(entry)], env=environ, capture_output=True, text=True, timeout=60)

    assert (done.returncode, done.stdout) == (64, "")
    assert done.stderr == "ERROR worker start requires attempt directory and launch record\n"
