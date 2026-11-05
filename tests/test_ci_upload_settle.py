import base64
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[1] / ".github/actions/retry-upload/settle.py"
_SPEC = importlib.util.spec_from_file_location("retry_upload_settle", _PATH)
settle = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(settle)

_IDS = {"workflow_run_backend_id": "run-1", "workflow_job_run_backend_id": "job-2"}


def _token():
    claims = json.dumps({"scp": "Actions.GenericRead:x Actions.Results:run-1:job-2"}).encode()
    return "h." + base64.urlsafe_b64encode(claims).decode().rstrip("=") + ".s"


def test_the_backend_ids_come_from_the_runtime_token_scope():
    assert settle.backend_ids(_token()) == _IDS


def _scripted(answers, calls):
    def call(base, token, method, body):
        calls.append((method, body))
        return answers.pop(0)

    return call


@pytest.mark.parametrize(
    "answers, found, methods",
    [
        ([(200, {"artifacts": [{"name": "a"}]})], "finalized", ["ListArtifacts"]),
        ([(200, {"artifacts": []}), (200, {})], "free", ["ListArtifacts", "CreateArtifact"]),
        ([(200, {}), (409, {})], "held", ["ListArtifacts", "CreateArtifact"]),
        ([(0, {}), (0, {})], "unknown", ["ListArtifacts", "CreateArtifact"]),
    ],
    ids=["finalized", "free", "held", "unreachable"],
)
def test_the_state_lists_first_then_probes_a_create(monkeypatch, answers, found, methods):
    calls = []
    monkeypatch.setattr(settle, "call", _scripted(answers, calls))
    assert settle.state("https://r/", _token(), "a") == found
    assert [method for method, _ in calls] == methods
    assert calls[0][1] == {**_IDS, "name_filter": "a"}
    if len(calls) > 1:
        assert calls[1][1] == {**_IDS, "name": "a", "version": 4}


def test_a_held_name_is_asked_again_until_it_is_finalized(monkeypatch, capsys):
    states = ["held", "unknown", "finalized"]
    slept = []
    monkeypatch.setattr(settle, "state", lambda base, token, name: states.pop(0))
    monkeypatch.setattr(settle.time, "sleep", slept.append)
    assert settle.settle("https://r/", "t", "flakes-3.12-13", 120) == "finalized"
    assert slept == [10, 10]
    assert capsys.readouterr().out.count("flakes-3.12-13 is ") == 2


def test_a_name_still_held_at_the_bound_ends_the_wait(monkeypatch):
    slept = []
    monkeypatch.setattr(settle, "state", lambda base, token, name: "held")
    monkeypatch.setattr(settle.time, "sleep", slept.append)
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(settle.time, "monotonic", lambda: next(clock))
    assert settle.settle("https://r/", "t", "a", 35) == "held"
    assert slept == [10, 10]


@pytest.mark.parametrize(
    "found, said",
    [
        ("free", "a is free; the next attempt can upload it."),
        ("finalized", "a is finalized; the next attempt can upload it."),
        ("held", "::warning title=Retried upload::a is still held after 120s."),
    ],
)
def test_main_reports_the_outcome_and_never_fails(monkeypatch, capsys, found, said):
    monkeypatch.setenv("ARTIFACT", "a")
    monkeypatch.setenv("SETTLE_SECONDS", "120")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "t")
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "https://r/")
    seen = []
    monkeypatch.setattr(settle, "settle", lambda *args: seen.append(args) or found)
    assert settle.main() == 0
    assert seen == [("https://r/", "t", "a", 120.0)]
    assert said in capsys.readouterr().out


def test_main_without_a_runtime_token_retries_without_settling(monkeypatch, capsys):
    monkeypatch.setenv("ARTIFACT", "a")
    monkeypatch.delenv("ACTIONS_RUNTIME_TOKEN", raising=False)
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "https://r/")
    assert settle.main() == 0
    assert "No artifact service token; a is retried without settling." in capsys.readouterr().out


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_call_posts_json_to_the_service_with_the_bearer_token(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append((request, timeout))
        return _Response(b'{"artifacts": []}')

    monkeypatch.setattr(settle.urllib.request, "urlopen", urlopen)
    assert settle.call("https://r/base/", "tok", "ListArtifacts", {"a": 1}) == (200, {"artifacts": []})
    [(request, timeout)] = seen
    assert request.full_url == "https://r/base/twirp/github.actions.results.api.v1.ArtifactService/ListArtifacts"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"a": 1}
    assert timeout == 30


@pytest.mark.parametrize(
    "error, status",
    [
        (urllib.error.HTTPError("u", 409, "Conflict", {}, io.BytesIO(b"")), 409),
        (urllib.error.URLError("reset"), 0),
        (TimeoutError(), 0),
    ],
    ids=["http", "url", "timeout"],
)
def test_call_turns_failures_into_a_status(monkeypatch, error, status):
    def urlopen(request, timeout):
        raise error

    monkeypatch.setattr(settle.urllib.request, "urlopen", urlopen)
    assert settle.call("https://r/", "tok", "CreateArtifact", {}) == (status, {})
