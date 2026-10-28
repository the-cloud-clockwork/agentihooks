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
            response = json.dumps({"model": payload["model"], "data": data}).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(response)))
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
