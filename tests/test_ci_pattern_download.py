import base64
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/pattern-download"
CLAIMS = base64.urlsafe_b64encode(json.dumps({"scp": "Actions.Results:run-1:job-2"}).encode()).decode()
TOKEN = f"e30.{CLAIMS.rstrip('=')}.signature"
PATTERN = "mutation-stats-*"
Serve = Callable[["Service"], str]


def _fetch() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pattern_download_fetch", ACTION / "fetch.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = _fetch()


def _archive(name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo(f"collected-{name}", date_time=(2026, 11, 19, 0, 0, 0)), name)
    return buffer.getvalue()


class Service:
    """A results service listing named archives, each blob answer following the script given for its name."""

    def __init__(self, listed: list[tuple[str, int]], answers: dict[str, list[str]] | None = None) -> None:
        self.listed = listed
        self.answers = {name: list(kinds) for name, kinds in (answers or {}).items()}
        self.digests = True
        self.listing_failures = 0
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fetched: list[str] = []
        self.closed_by_client = threading.Event()
        self.released = threading.Event()

    def artifacts(self) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "database_id": str(ident),
                "workflow_run_backend_id": f"run-{ident}",
                "workflow_job_run_backend_id": f"job-{ident}",
                **({"digest": f"sha256:{hashlib.sha256(_archive(name)).hexdigest()}"} if self.digests else {}),
            }
            for name, ident in self.listed
        ]

    def answer_for(self, name: str) -> str:
        kinds = self.answers.get(name)
        return kinds.pop(0) if kinds else "ok"


class Handler(BaseHTTPRequestHandler):
    """Answer the service calls and blob downloads from the Service its server holds."""

    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> Service:
        return cast("Server", self.server).service

    def log_message(self, format: str, *args: object) -> None:
        return

    def answer(self, code: int, body: bytes, length: int | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = self.path.rsplit("/", 1)[-1]
        self.service.calls.append((method, request))
        if method == "ListArtifacts" and self.service.listing_failures:
            self.service.listing_failures -= 1
            self.answer(500, b"{}")
        elif method == "ListArtifacts":
            self.answer(200, json.dumps({"artifacts": self.service.artifacts()}).encode())
        else:
            url = f"http://{self.headers['Host']}/blob/{request['name']}"
            self.answer(200, json.dumps({"signed_url": url}).encode())

    def do_GET(self) -> None:
        name = self.path.rsplit("/", 1)[-1]
        self.service.fetched.append(name)
        kind, body = self.service.answer_for(name), _archive(name)
        if kind == "busy":
            self.answer(503, b"<Error><Code>ServerBusy</Code></Error>")
            if self.client_closed():
                self.service.closed_by_client.set()
        elif kind == "torn":
            self.answer(200, body[: len(body) // 2])
        elif kind == "trickle":
            self.trickle(body)
        elif kind == "hold":
            self.service.released.wait(10)
        elif kind in ("cut", "stall", "late"):
            self.partial(body, kind)
        else:
            self.answer(200, body)
        if kind != "ok":
            self.close_connection = True

    def client_closed(self) -> bool:
        try:
            return self.rfile.read() == b""
        except ConnectionResetError:
            return True

    def partial(self, body: bytes, kind: str) -> None:
        if kind == "late" and self.service.released.wait(1):
            return
        self.answer(200, body[:10], length=len(body))
        self.service.released.wait(0 if kind == "cut" else 10)

    def trickle(self, body: bytes) -> None:
        self.answer(200, body[:1], length=len(body))
        with contextlib.suppress(OSError):
            for index in range(1, len(body)):
                if self.service.released.wait(0.05):
                    break
                self.wfile.write(body[index : index + 1])
                self.wfile.flush()


class Server(ThreadingHTTPServer):
    """A loopback server on a port of its own, holding the Service its handlers answer from."""

    daemon_threads = True

    def __init__(self, service: Service) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.service = service


@pytest.fixture
def serve() -> Iterator[Serve]:
    started: list[Server] = []

    def start(service: Service) -> str:
        server = Server(service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return f"http://127.0.0.1:{server.server_port}/"

    yield start
    for server in started:
        server.service.released.set()
        server.shutdown()
        server.server_close()


def _download(base: str, folder: Path, pauses: list[float] | None = None, deadline: float | None = None) -> list[str]:
    results = fetch.Results(base, TOKEN, time.monotonic() + 30 if deadline is None else deadline)
    return fetch.download(results, PATTERN, folder, (pauses if pauses is not None else []).append)


def _files(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


@pytest.mark.feature("shared-mutation-stats-download-ends-with-its-download")
def test_a_kept_alive_error_answer_is_closed_before_the_retry_and_every_part_merges(
    serve: Serve, tmp_path: Path
) -> None:
    service = Service([("mutation-stats-0", 1), ("mutation-stats-1", 2)], {"mutation-stats-1": ["busy"]})
    pauses: list[float] = []
    assert _download(serve(service), tmp_path / "stats", pauses) == []
    assert service.closed_by_client.wait(5)
    assert _files(tmp_path / "stats") == ["collected-mutation-stats-0", "collected-mutation-stats-1"]
    assert sorted(service.fetched) == ["mutation-stats-0", "mutation-stats-1", "mutation-stats-1"]
    assert pauses == [fetch.PAUSE]


@pytest.mark.feature("shared-mutation-stats-download-ends-with-its-download")
def test_a_client_that_resets_its_kept_alive_error_answer_has_closed_it(serve: Serve) -> None:
    service = Service([("mutation-stats-0", 1)], {"mutation-stats-0": ["busy"]})
    port = urllib.parse.urlsplit(serve(service)).port
    assert port is not None
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", "/blob/mutation-stats-0")
    answer = connection.getresponse()
    assert (answer.status, answer.read()) == (503, b"<Error><Code>ServerBusy</Code></Error>")
    assert connection.sock is not None
    connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    connection.close()
    assert service.closed_by_client.wait(5)


def test_only_the_newest_artifact_of_each_matching_name_is_signed_for_its_uploader(
    serve: Serve, tmp_path: Path
) -> None:
    listed = [("mutation-stats-0", 5), ("mutation-stats-0", 9), ("mutation-evidence-0", 7), ("mutation-stats-1", 3)]
    service = Service(listed)
    assert _download(serve(service), tmp_path / "stats") == []
    signed = sorted(
        (request["name"], request["workflow_run_backend_id"], request["workflow_job_run_backend_id"])
        for method, request in service.calls
        if method == "GetSignedArtifactURL"
    )
    assert signed == [("mutation-stats-0", "run-9", "job-9"), ("mutation-stats-1", "run-3", "job-3")]
    listing = [request for method, request in service.calls if method == "ListArtifacts"]
    assert listing == [{"workflow_run_backend_id": "run-1", "workflow_job_run_backend_id": "job-2"}]
    assert sorted(service.fetched) == ["mutation-stats-0", "mutation-stats-1"]


def test_a_part_that_never_answers_200_is_named_after_every_try(serve: Serve, tmp_path: Path) -> None:
    service = Service([("mutation-stats-0", 1)], {"mutation-stats-0": ["busy"] * fetch.TRIES})
    pauses: list[float] = []
    assert _download(serve(service), tmp_path / "stats", pauses) == ["mutation-stats-0"]
    assert service.fetched == ["mutation-stats-0"] * fetch.TRIES
    assert pauses == [fetch.PAUSE] * (fetch.TRIES - 1)
    assert _files(tmp_path / "stats") == []


@pytest.mark.parametrize(("kind", "digests"), [("torn", True), ("torn", False), ("cut", True)])
def test_a_torn_or_cut_archive_is_fetched_again(serve: Serve, tmp_path: Path, kind: str, digests: bool) -> None:
    service = Service([("mutation-stats-0", 1)], {"mutation-stats-0": [kind]})
    service.digests = digests
    assert _download(serve(service), tmp_path / "stats") == []
    assert service.fetched == ["mutation-stats-0", "mutation-stats-0"]
    assert (tmp_path / "stats" / "collected-mutation-stats-0").read_text() == "mutation-stats-0"


def test_a_stalled_blob_is_fetched_again_after_the_read_timeout(
    serve: Serve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch, "TIMEOUT", 2.0)
    service = Service([("mutation-stats-0", 1)], {"mutation-stats-0": ["stall"]})
    assert _download(serve(service), tmp_path / "stats") == []
    assert service.fetched == ["mutation-stats-0", "mutation-stats-0"]
    assert _files(tmp_path / "stats") == ["collected-mutation-stats-0"]


@pytest.mark.wall_clock
@pytest.mark.parametrize("kind", ["hold", "stall", "trickle"])
def test_a_blob_still_arriving_at_the_deadline_is_named_instead_of_outliving_it(
    serve: Serve, tmp_path: Path, kind: str
) -> None:
    service = Service([("mutation-stats-0", 1)], {"mutation-stats-0": [kind]})
    pauses: list[float] = []
    started = time.monotonic()
    assert _download(serve(service), tmp_path / "stats", pauses, deadline=started + 2) == ["mutation-stats-0"]
    assert time.monotonic() - started < 5
    assert service.fetched == ["mutation-stats-0"]
    assert pauses == []


@pytest.mark.parametrize(("failures", "missing"), [(fetch.TRIES - 1, []), (fetch.TRIES, [fetch.LISTING])])
def test_a_listing_outage_is_tried_again_and_named_when_it_outlasts_every_try(
    serve: Serve, tmp_path: Path, failures: int, missing: list[str]
) -> None:
    service = Service([("mutation-stats-0", 1)])
    service.listing_failures = failures
    assert _download(serve(service), tmp_path / "stats") == missing
    assert service.fetched == ([] if missing else ["mutation-stats-0"])


def test_no_matching_artifact_downloads_nothing_and_passes(serve: Serve, tmp_path: Path) -> None:
    service = Service([("mutation-evidence-0", 1)])
    assert _download(serve(service), tmp_path / "stats") == []
    assert _files(tmp_path / "stats") == []
    assert service.fetched == []


def test_main_reads_the_runtime_token_and_names_every_part_that_never_arrived(
    serve: Serve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fetch, "PAUSE", 0)
    service = Service([("mutation-stats-0", 1), ("mutation-stats-1", 2)], {"mutation-stats-1": ["busy"] * fetch.TRIES})
    monkeypatch.setenv("ACTIONS_RESULTS_URL", serve(service))
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", TOKEN)
    assert fetch.main([PATTERN, str(tmp_path / "stats")]) == 1
    assert capsys.readouterr().out.endswith(
        f"Downloaded 1 of 2 artifacts matching {PATTERN} into {tmp_path / 'stats'}\n"
        f"::error::mutation-stats-1 did not arrive within {fetch.TRIES} tries before the {fetch.DEADLINE:g} s deadline.\n"
    )
    assert _files(tmp_path / "stats") == ["collected-mutation-stats-0"]


def test_the_script_run_as_the_action_runs_it_downloads_and_exits(serve: Serve, tmp_path: Path) -> None:
    service = Service([("mutation-stats-0", 1), ("mutation-stats-1", 2)])
    env = {**os.environ, "ACTIONS_RESULTS_URL": serve(service), "ACTIONS_RUNTIME_TOKEN": TOKEN}
    result = subprocess.run(
        [sys.executable, str(ACTION / "fetch.py"), PATTERN, str(tmp_path / "stats")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == f"Downloaded 2 of 2 artifacts matching {PATTERN} into {tmp_path / 'stats'}\n"
    assert _files(tmp_path / "stats") == ["collected-mutation-stats-0", "collected-mutation-stats-1"]


@pytest.mark.wall_clock
def test_the_watchdog_ends_the_script_when_one_socket_read_outlives_the_deadline(serve: Serve, tmp_path: Path) -> None:
    service = Service([("mutation-stats-0", 1)], {"mutation-stats-0": ["late"]})
    runner = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('fetch', sys.argv[1])\n"
        "fetch = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(fetch)\n"
        "fetch.DEADLINE, fetch.GRACE = 2.0, 0.2\n"
        "sys.exit(fetch.main(sys.argv[2:]))\n"
    )
    env = {**os.environ, "ACTIONS_RESULTS_URL": serve(service), "ACTIONS_RUNTIME_TOKEN": TOKEN}
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", runner, str(ACTION / "fetch.py"), PATTERN, str(tmp_path / "stats")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == f"::error::Downloading {PATTERN} ran past its 2 s deadline.\n"
    assert time.monotonic() - started < 10
    assert service.fetched == ["mutation-stats-0"]


def test_the_action_runs_the_script_inside_github_script_without_a_workflow_token() -> None:
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    assert set(action["inputs"]) == {"pattern", "path"}
    assert all(spec["required"] is True for spec in action["inputs"].values())
    (step,) = action["runs"]["steps"]
    assert step["uses"] == "actions/github-script@v7"
    assert step["with"]["github-token"] == "none"
    assert step["env"] == {
        "FETCH_SCRIPT": "${{ github.action_path }}/fetch.py",
        "PATTERN": "${{ inputs.pattern }}",
        "DESTINATION": "${{ inputs.path }}",
    }
    assert step["with"]["script"] == (
        "require('child_process').execFileSync('python3', [process.env.FETCH_SCRIPT, process.env.PATTERN,"
        " process.env.DESTINATION], {stdio: 'inherit'})\n"
    )
