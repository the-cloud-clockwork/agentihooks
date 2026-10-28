import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(tmp_path):
    from tests import sv2_idx03_cases
    from tests.sv2_idx03_cases import Index

    inputs = json.loads(sv2_idx03_cases.FIXTURE.read_text())
    index = Index(inputs, tmp_path)
    index.session.append(inputs["turns"][:-1])
    index.session.build()
    index.session.validate()
    index.session.activate("current", 0)
    yield index
    index.session.catalog.close()


@pytest.fixture
def provider():
    requests = []
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers["Authorization"], payload))
            data = [{"index": i, "embedding": [1.0, 0.0, 0.0, 0.0]} for i in range(len(payload["input"]))]
            response = state.get("body", json.dumps({"model": payload["model"], "data": data}).encode())
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(response)))
            if "location" in state:
                self.send_header("Location", state["location"])
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/embeddings", requests, state
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def environment(index, provider):
    return {
        **os.environ,
        "AGENTIHOOKS_SESSION_CATALOG": str(index.session.path),
        "AGENTIHOOKS_SESSION_EMBED_CORPUS": index.domain,
        "AGENTIHOOKS_SESSION_EMBED_PROJECTS": json.dumps(index.inputs["grant"]["projects"]),
        "AGENTIHOOKS_SESSION_EMBED_GENERATION": index.generation,
        "AGENTIHOOKS_SESSION_EMBED_MODEL": "fixture-hash-embedder-4",
        "AGENTIHOOKS_SESSION_EMBED_DIMENSION": "4",
        "AGENTIHOOKS_SESSION_EMBED_URL": provider[0],
        "AGENTIHOOKS_SESSION_EMBED_TOKEN": "fixture-provider-token",
    }


def run(env):
    return subprocess.run(
        [sys.executable, "-m", "hooks.memory.embedding_process", "--once"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_process_embeds_active_chunks_and_restart_is_idempotent(prepared, provider):
    env = environment(prepared, provider)
    result = run(env)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["state"] == "drained"
    assert report["embedded"]
    assert report["pending"] == 0
    prepared.worker.register(
        prepared.domain, prepared.generation, prepared.worker.model(prepared.domain, prepared.generation)
    )
    vectors = prepared.vectors()
    assert len(vectors) == len(report["embedded"])
    assert all(vector["vector"] == [1.0, 0.0, 0.0, 0.0] for vector in vectors)
    assert provider[1][0][0] == "/embeddings"
    assert provider[1][0][1] == "Bearer fixture-provider-token"
    assert provider[1][0][2]["model"] == "fixture-hash-embedder-4"
    count = len(provider[1])
    repeated = run(env)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["embedded"] == []
    assert len(provider[1]) == count


def test_process_preserves_pending_work_on_outage_and_resumes(prepared, provider):
    provider[2]["status"] = 503
    env = environment(prepared, provider)
    failed = run(env)
    assert failed.returncode == 0, failed.stderr
    report = json.loads(failed.stdout)
    assert report["state"] == "deferred"
    assert report["pending"] > 0
    assert report["embedded"] == []
    provider[2]["status"] = 200
    resumed = run(env)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["pending"] == 0
    assert json.loads(resumed.stdout)["embedded"]


def test_process_never_embeds_a_session_outside_its_project_grant(prepared, provider):
    env = {**environment(prepared, provider), "AGENTIHOOKS_SESSION_EMBED_PROJECTS": '["other-project"]'}
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["embedded"] == []
    assert not provider[1]


@pytest.mark.parametrize(
    "setting",
    [
        "CATALOG",
        "EMBED_CORPUS",
        "EMBED_PROJECTS",
        "EMBED_GENERATION",
        "EMBED_MODEL",
        "EMBED_DIMENSION",
        "EMBED_URL",
        "EMBED_TOKEN",
    ],
)
def test_process_refuses_missing_configuration(prepared, provider, setting):
    env = environment(prepared, provider)
    env.pop(f"AGENTIHOOKS_SESSION_{setting}")
    result = run(env)
    assert result.returncode != 0
    assert "required" in result.stderr
    assert "fixture-provider-token" not in result.stderr
    assert not provider[1]


def test_main_runs_in_process_and_closes_its_catalog(prepared, provider, monkeypatch, capsys):
    from hooks.memory import embedding_process

    for key, value in environment(prepared, provider).items():
        monkeypatch.setenv(key, value)
    assert embedding_process.main(["--once"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "drained"
    assert report["pending"] == 0
    assert report["embedded"]
    assert report["skipped"] == 0
    assert report["after"] == ""


@pytest.mark.parametrize(
    "setting",
    [
        "CATALOG",
        "EMBED_CORPUS",
        "EMBED_PROJECTS",
        "EMBED_GENERATION",
        "EMBED_MODEL",
        "EMBED_DIMENSION",
        "EMBED_URL",
        "EMBED_TOKEN",
    ],
)
def test_configure_requires_each_setting(prepared, provider, setting):
    from hooks.memory.embedding_process import configure

    env = environment(prepared, provider)
    env.pop(f"AGENTIHOOKS_SESSION_{setting}")
    with pytest.raises(SystemExit, match="required"):
        configure(env)


@pytest.mark.parametrize("projects", ["{}", "[]", '[""]', "[null]", "[1]"])
def test_configure_refuses_invalid_projects(prepared, provider, projects):
    from hooks.memory.embedding_process import configure

    env = environment(prepared, provider)
    env["AGENTIHOOKS_SESSION_EMBED_PROJECTS"] = projects
    with pytest.raises(ValueError, match="projects"):
        configure(env)


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.example/embed",
        "https:///embed",
        "https://user@host/embed",
        "https://host/embed?token=fixture",
        "https://host/embed#fragment",
    ],
)
def test_configure_refuses_unsafe_provider_urls(prepared, provider, url):
    from hooks.memory.embedding_process import configure

    env = environment(prepared, provider)
    env["AGENTIHOOKS_SESSION_EMBED_URL"] = url
    with pytest.raises(ValueError, match="URL"):
        configure(env)


@pytest.mark.parametrize(
    "setting,value",
    [
        ("EMBED_BATCH_SIZE", "0"),
        ("EMBED_BATCH_CHARS", "-1"),
        ("EMBED_MAX_BATCHES", "0"),
        ("EMBED_SESSION_LIMIT", "0"),
        ("EMBED_INTERVAL", "0"),
        ("EMBED_INTERVAL", "nan"),
        ("EMBED_INTERVAL", "inf"),
    ],
)
def test_configure_refuses_invalid_bounds(prepared, provider, setting, value):
    from hooks.memory.embedding_process import configure

    env = environment(prepared, provider)
    env[f"AGENTIHOOKS_SESSION_{setting}"] = value
    with pytest.raises(ValueError, match="positive"):
        configure(env)


def test_configure_preserves_all_runtime_settings(prepared, provider):
    from hooks.memory.embedding_process import configure

    env = environment(prepared, provider)
    settings = configure(env)
    assert settings.catalog == str(prepared.session.path)
    assert settings.grant.principal == "session-embedding-process"
    assert settings.grant.corpus == prepared.domain
    assert settings.grant.projects == frozenset(prepared.inputs["grant"]["projects"])
    assert settings.generation == prepared.generation
    assert settings.provider.url == provider[0]
    assert settings.provider.model.model == "fixture-hash-embedder-4"
    assert settings.provider.model.dimension == 4
    assert settings.provider.token == "fixture-provider-token"
    assert "fixture-provider-token" not in repr(settings)
    assert (settings.limits.batch_size, settings.limits.batch_chars, settings.limits.max_batches) == (32, 64000, 8)
    assert settings.interval == 5
    assert settings.session_limit == 100
    changed = configure(
        {
            **env,
            "AGENTIHOOKS_SESSION_EMBED_BATCH_SIZE": "1",
            "AGENTIHOOKS_SESSION_EMBED_BATCH_CHARS": "2000",
            "AGENTIHOOKS_SESSION_EMBED_MAX_BATCHES": "2",
            "AGENTIHOOKS_SESSION_EMBED_SESSION_LIMIT": "3",
            "AGENTIHOOKS_SESSION_EMBED_INTERVAL": "0.1",
        }
    )
    assert (changed.limits.batch_size, changed.limits.batch_chars, changed.limits.max_batches) == (1, 2000, 2)
    assert changed.session_limit == 3
    assert changed.interval == 0.1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_provider_defers_retryable_http_errors(prepared, provider, status):
    from hooks.memory.embedding_process import configure
    from hooks.memory.embeddings import TransientEmbeddingError

    provider[2]["status"] = status
    adapter = configure(environment(prepared, provider)).provider
    with pytest.raises(TransientEmbeddingError, match="unavailable"):
        adapter.embed(["text"])


@pytest.mark.parametrize("status", [301, 400, 401, 403])
def test_provider_refuses_permanent_http_errors(prepared, provider, status):
    from hooks.memory.embedding_process import configure

    provider[2]["status"] = status
    adapter = configure(environment(prepared, provider)).provider
    with pytest.raises(ValueError, match="refused"):
        adapter.embed(["text"])


def test_provider_posts_the_model_and_preserves_input_order(prepared, provider):
    from hooks.memory.embedding_process import configure

    adapter = configure(environment(prepared, provider)).provider
    assert adapter.embed(["one", "two"]) == [[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]]
    assert provider[1] == [
        (
            provider[0].removeprefix(provider[0].rsplit("/", 1)[0]),
            "Bearer fixture-provider-token",
            {
                "model": "fixture-hash-embedder-4",
                "input": ["one", "two"],
            },
        )
    ]


@pytest.mark.parametrize(
    "body",
    [
        b'{"model":"other","data":[]}',
        b'{"model":"fixture-hash-embedder-4","data":[{"index":2,"embedding":[1,0,0,0]}]}',
        b'{"model":"fixture-hash-embedder-4","data":[{"index":0,"embedding":[1]}]}',
        b"broken json",
        b"x" * ((2 << 20) + 1),
    ],
)
def test_provider_rejects_invalid_or_oversized_responses(prepared, provider, body):
    from hooks.memory.embedding_process import configure

    provider[2]["body"] = body
    with pytest.raises(ValueError):
        configure(environment(prepared, provider)).provider.embed(["text"])


def test_provider_reorders_vectors_by_response_index(prepared, provider):
    from hooks.memory.embedding_process import configure

    provider[2]["body"] = json.dumps(
        {
            "model": "fixture-hash-embedder-4",
            "data": [
                {"index": 1, "embedding": [0, 1, 0, 0]},
                {"index": 0, "embedding": [1, 0, 0, 0]},
            ],
        }
    ).encode()
    assert configure(environment(prepared, provider)).provider.embed(["first", "second"]) == [
        [1, 0, 0, 0],
        [0, 1, 0, 0],
    ]


def test_provider_never_follows_a_redirect_with_its_bearer(prepared, provider):
    from hooks.memory.embedding_process import configure

    provider[2].update(status=302, location=provider[0] + "/redirect")
    with pytest.raises(ValueError, match="refused"):
        configure(environment(prepared, provider)).provider.embed(["text"])
    assert len(provider[1]) == 1


def test_cycle_wraps_its_bounded_scan_and_does_not_activate_an_index(prepared, provider):
    from dataclasses import replace

    from hooks.memory.embedding_process import configure, cycle

    settings = replace(configure(environment(prepared, provider)), session_limit=1)
    prepared.worker.register(prepared.domain, prepared.generation, settings.provider.model)
    report = cycle(prepared.worker, settings, "")
    assert report["after"] == prepared.inputs["session_uid"]
    assert report["embedded"]
    assert report["skipped"] == 0
    wrapped = cycle(prepared.worker, settings, report["after"])
    assert wrapped["after"] == ""
    assert wrapped["embedded"] == []
    assert prepared.worker.active(prepared.domain) == {"index_generation": None, "revision": 0}


def test_cycle_skips_forbidden_sessions_but_refuses_forbidden_queued_work(prepared, provider):
    from dataclasses import replace

    from hooks.memory.api.ingest import Grant
    from hooks.memory.archive import EvidenceRefused
    from hooks.memory.embedding_process import configure, cycle
    from tests.sv2_idx02_cases import grant

    settings = configure(environment(prepared, provider))
    prepared.worker.register(prepared.domain, prepared.generation, settings.provider.model)
    narrow = replace(settings, grant=Grant("worker", prepared.domain, frozenset({"other"})))
    report = cycle(prepared.worker, narrow, "")
    assert report["skipped"] == 1
    assert report["embedded"] == []
    from tests.sv2_idx03_cases import target

    prepared.worker.enqueue(grant(prepared.inputs), target(prepared.inputs, "index_generation"), 1.0)
    before = prepared.work()
    with pytest.raises(EvidenceRefused):
        cycle(prepared.worker, narrow, "")
    assert prepared.work() == before
    assert provider[1] == []


def test_cycle_defers_a_provider_outage_without_losing_work(prepared, provider):
    from hooks.memory.embedding_process import configure, cycle

    settings = configure(environment(prepared, provider))
    prepared.worker.register(prepared.domain, prepared.generation, settings.provider.model)
    provider[2]["status"] = 503
    deferred = cycle(prepared.worker, settings, "")
    assert deferred["state"] == "deferred"
    assert deferred["failed"]
    assert deferred["pending"] > 0
    provider[2]["status"] = 200
    resumed = cycle(prepared.worker, settings, "")
    assert resumed["state"] == "drained"
    assert resumed["embedded"]
    assert resumed["pending"] == 0


def test_main_waits_between_passes_and_restores_signal_handlers(prepared, provider, monkeypatch, capsys):
    import signal

    from hooks.memory import embedding_process

    for key, value in environment(prepared, provider).items():
        monkeypatch.setenv(key, value)
    real_event = threading.Event
    waits = []

    class StopAfterWait(real_event):
        def wait(self, timeout=None):
            waits.append(timeout)
            signal.raise_signal(signal.SIGTERM)
            return self.is_set()

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    monkeypatch.setattr(embedding_process.threading, "Event", StopAfterWait)
    assert embedding_process.main([]) == 0
    assert waits == [5.0]
    assert json.loads(capsys.readouterr().out)["embedded"]
    assert {sig: signal.getsignal(sig) for sig in previous} == previous
