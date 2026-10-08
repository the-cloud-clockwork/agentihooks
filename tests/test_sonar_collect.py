import hashlib
import importlib.util
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("sonar_collect", ROOT / ".github/coverage/collect.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGitHub:
    """Serves one run whose artifacts and jobs change at the ticks the spec names."""

    def __init__(self, landed, jobs=None, outage=(), expired=()):
        self.landed = landed
        self.expired = set(expired)
        self.jobs = jobs or {}
        self.outage = set(outage)
        self.tick = 0
        self.calls = []
        self.etags = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.calls.append((fake.tick, self.path, dict(self.headers)))
                if fake.tick in fake.outage and "/blob/" not in self.path:
                    self.reply(502, b"{}")
                elif self.path.startswith("/repos/owner/repo/actions/runs/7/artifacts"):
                    names = sorted(name for name, tick in fake.landed.items() if tick <= fake.tick)
                    artifacts = [
                        {
                            "name": name,
                            "expired": name in fake.expired,
                            "archive_download_url": fake.url(f"/zip/{name}"),
                        }
                        for name in names
                    ]
                    body = json.dumps({"artifacts": artifacts}).encode()
                    etag = fake.etag(body)
                    if self.headers.get("If-None-Match") == etag:
                        self.reply(304, b"")
                        return
                    fake.etags.append(etag)
                    self.reply(200, body, {"ETag": etag})
                elif self.path.startswith("/repos/owner/repo/actions/runs/7/jobs"):
                    jobs = [
                        {"name": name, "status": "completed" if state else "in_progress", "conclusion": state}
                        for name, state in fake.job_states().items()
                    ]
                    self.reply(200, json.dumps({"jobs": jobs}).encode())
                elif self.path.startswith("/zip/"):
                    self.reply(302, b"", {"Location": fake.url(self.path.replace("/zip/", "/blob/"))})
                elif self.path.startswith("/blob/"):
                    archive = io.BytesIO()
                    with zipfile.ZipFile(archive, "w") as handle:
                        handle.writestr(".coverage", self.path.rsplit("/", 1)[1])
                    self.reply(200, archive.getvalue())
                else:
                    self.reply(404, b"{}")

            def reply(self, status, body, headers=None):
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    @staticmethod
    def etag(body):
        return f'W/"{hashlib.sha256(body).hexdigest()}"'

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def job_states(self):
        states = {}
        for name, timeline in self.jobs.items():
            state = None
            for tick, conclusion in timeline:
                if tick <= self.tick:
                    state = conclusion
            states[name] = state
        return states

    def sleep(self, seconds):
        self.tick += 1

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _collect(fake, tmp_path, shards=3):
    module = _module()
    return module.collect(
        fake.url("/repos/owner/repo/actions/runs/7"),
        shards,
        tmp_path,
        "fixture",
        sleep=fake.sleep,
        clock=lambda: fake.tick * module.POLL_SECONDS,
    )


def test_each_shard_downloads_as_soon_as_its_artifact_lands(tmp_path):
    landed = {"coverage-3.12-1": 2, "coverage-3.12-2": 5, "coverage-3.12-3": 5, "durations-3.12-1": 1}
    with FakeGitHub(landed) as fake:
        error = _collect(fake, tmp_path)
    assert error is None
    for shard in (1, 2, 3):
        assert (tmp_path / f"coverage-3.12-{shard}" / ".coverage").read_text() == f"coverage-3.12-{shard}"
    downloads = {path: tick for tick, path, _ in fake.calls if path.startswith("/zip/")}
    assert downloads == {"/zip/coverage-3.12-1": 2, "/zip/coverage-3.12-2": 5, "/zip/coverage-3.12-3": 5}
    assert fake.tick == 5
    assert not any(path.startswith("/zip/durations") for _, path, _ in fake.calls)


def test_unchanged_listings_are_conditional_requests(tmp_path):
    with FakeGitHub({"durations-3.12-1": 1, "coverage-3.12-1": 4}) as fake:
        assert _collect(fake, tmp_path, shards=1) is None
    listings = [headers for _, path, headers in fake.calls if "/artifacts" in path]
    assert len(listings) == 5
    assert len(fake.etags) == 3
    assert "If-None-Match" not in listings[0]
    assert listings[1]["If-None-Match"] == fake.etags[0]
    assert [headers["If-None-Match"] for headers in listings[2:]] == [fake.etags[1]] * 3


def test_the_token_never_follows_the_download_redirect(tmp_path):
    with FakeGitHub({"coverage-3.12-1": 0}) as fake:
        assert _collect(fake, tmp_path, shards=1) is None
    headers = {path.split("/")[1]: headers for _, path, headers in fake.calls}
    assert headers["zip"]["Authorization"] == "Bearer fixture"
    assert "Authorization" not in headers["blob"]


def test_a_failed_shard_is_red_without_waiting_for_the_deadline(tmp_path):
    jobs = {"unit (3.12, 1)": [(1, "success")], "unit (3.12, 2)": [(3, "failure")], "unit (3.11, 2)": [(1, "failure")]}
    with FakeGitHub({"coverage-3.12-1": 1}, jobs) as fake:
        error = _collect(fake, tmp_path, shards=2)
    assert "unit (3.12, 2)" in error
    assert "failure" in error
    assert fake.tick < 2 * _module().JOB_CHECK_POLLS


def test_a_passed_shard_without_coverage_is_red(tmp_path):
    jobs = {"unit (3.12, 1)": [(1, "success")]}
    with FakeGitHub({}, jobs) as fake:
        error = _collect(fake, tmp_path, shards=1)
    assert "unit (3.12, 1)" in error
    assert "without coverage" in error
    assert fake.tick < 3 * _module().JOB_CHECK_POLLS


def test_a_passed_shard_whose_coverage_trails_the_job_check_is_collected(tmp_path):
    module = _module()
    jobs = {"unit (3.12, 1)": [(1, "success")]}
    with FakeGitHub({"coverage-3.12-1": module.JOB_CHECK_POLLS + 1}, jobs) as fake:
        assert _collect(fake, tmp_path, shards=1) is None


def test_an_expired_artifact_is_never_downloaded(tmp_path):
    with FakeGitHub({"coverage-3.12-1": 0}, expired={"coverage-3.12-1"}) as fake:
        error = _collect(fake, tmp_path, shards=1)
    assert "coverage-3.12-1" in error
    assert not any(path.startswith("/zip/") for _, path, _ in fake.calls)


def test_missing_coverage_is_red_at_the_deadline(tmp_path):
    module = _module()
    with FakeGitHub({}) as fake:
        error = _collect(fake, tmp_path, shards=1)
    assert "coverage-3.12-1" in error
    assert fake.tick * module.POLL_SECONDS >= module.DEADLINE_SECONDS


def test_an_api_outage_keeps_polling(tmp_path):
    with FakeGitHub({"coverage-3.12-1": 0}, outage={0, 1, 2}) as fake:
        assert _collect(fake, tmp_path, shards=1) is None
    assert fake.tick == 3
