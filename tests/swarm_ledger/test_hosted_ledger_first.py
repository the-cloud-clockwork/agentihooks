import http.server
import io
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm import naming, runtime
from scripts.swarm.store import AgentRecord
from scripts.swarm_ledger import authority_context, hosted_probe, ledger, ledger_authority, ledger_link
from scripts.swarm_ledger.storage_migration import hosted as hosted_migration
from tests.swarm_ledger.test_hive_agent_tokens import hive as _hive
from tests.swarm_ledger.test_ledger_authority import SLUG
from tests.swarm_ledger.test_ledger_authority import live as _authority_live
from tests.test_swarm_v2_launch import FIRST, World

pytestmark = pytest.mark.unit

TOKEN = {"AGENTIHOOKS_LEDGER_AGENT_TOKEN": "agent-token"}
HIVE = {"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": "hive-credential"}
PROBE_FILE = hosted_probe.probe_file


@pytest.fixture(autouse=True)
def fresh_probes(monkeypatch, tmp_path):
    monkeypatch.setattr(hosted_probe, "_answers", {})
    monkeypatch.setattr(hosted_probe, "probe_file", lambda environ=None: tmp_path / "hosted-ledger-probe.json")


@pytest.fixture
def answering():
    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"dir": "/data"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def silent():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def workstation(**extra):
    return {"LEDGER_HOST": "127.0.0.1", "LEDGER_PORT": "8765", "LEDGER_DIR": "/scratch/ledger", **extra}


WORKSTATION = "http://127.0.0.1:8765"


def test_a_workstation_agent_resolves_the_answering_hosted_ledger_first(answering):
    environ = workstation(**TOKEN, **{hosted_probe.HOSTED_ENV: answering + "/"})

    assert ledger_link.base(environ) == answering
    assert ledger_link.remote(environ)


def test_a_workstation_agent_falls_back_to_the_workstation_server_when_the_hosted_one_does_not_answer(silent):
    environ = workstation(**TOKEN, **{hosted_probe.HOSTED_ENV: silent})

    assert ledger_link.base(environ) == WORKSTATION
    assert not ledger_link.remote(environ)


def test_a_session_without_a_hosted_credential_keeps_the_workstation_server(answering):
    environ = workstation(**{hosted_probe.HOSTED_ENV: answering})

    assert ledger_link.base(environ) == WORKSTATION
    assert not ledger_link.remote(environ)


def test_a_hive_agent_resolves_the_hosted_ledger_before_its_swarm_address(answering, silent):
    environ = {**HIVE, "AGENTIHOOKS_DEPLOYMENT": "distributed", "SWARM_URL": silent, hosted_probe.HOSTED_ENV: answering}

    assert ledger_link.base(environ) == answering
    assert ledger_link.remote(environ)


def test_a_hive_agent_keeps_its_hosted_address_when_the_health_probe_fails(silent):
    environ = {
        **HIVE,
        "AGENTIHOOKS_DEPLOYMENT": "distributed",
        "SWARM_URL": "https://workstation.example",
        hosted_probe.HOSTED_ENV: silent,
    }

    assert ledger_link.base(environ) == silent


def test_a_routed_swarm_address_still_wins_over_the_hosted_ledger(answering):
    environ = workstation(**TOKEN, **{hosted_probe.HOSTED_ENV: answering})

    with ledger_link.ledger_route.scoped("demo", "https://routed.example"):
        assert ledger_link.base(environ, slug="demo") == "https://routed.example"


def test_the_workstation_tick_binds_the_hosted_secret_so_launches_carry_hosted_tokens(monkeypatch):
    class Store:
        def slugs(self):
            return []

    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.setenv(hosted_probe.HOSTED_ENV, "https://hosted.example")
    monkeypatch.setattr(hosted_migration, "server_secret", lambda: "hosted-secret")

    with authority_context.for_tick(Store()):
        assert authority_context.secret() == "hosted-secret"


def test_the_workstation_tick_runs_without_a_hosted_secret(monkeypatch):
    class Store:
        def slugs(self):
            return []

    def unavailable():
        raise ValueError("trusted OpenBao credentials unavailable")

    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    monkeypatch.setenv(hosted_probe.HOSTED_ENV, "https://hosted.example")
    monkeypatch.setattr(hosted_migration, "server_secret", unavailable)

    with authority_context.for_tick(Store()):
        assert authority_context.secret() is None


def test_a_hive_launch_hands_the_hosted_ledger_address_to_the_worker_home(monkeypatch):
    monkeypatch.setenv(hosted_probe.HOSTED_ENV, "https://hosted.example/")
    world = World(monkeypatch)
    world.launch(FIRST)
    [spawned] = world.runtime.requests

    assert spawned.task["endpoints"][hosted_probe.HOSTED_ENV] == "https://hosted.example"


live = _authority_live
hive = _hive


def test_a_hive_launched_worker_joins_the_hosted_ledger_after_a_failed_probe(monkeypatch, live, hive, silent, capsys):
    hosted = f"http://{live['host']}"
    monkeypatch.setenv(hosted_probe.HOSTED_ENV, hosted)
    world = World(monkeypatch)
    world.launch(FIRST)
    [spawned] = world.runtime.requests
    store, grant = hive
    code = naming.parse(spawned.name).code
    store.names.adopt(SLUG, code, "", "")
    store.redis.hset(store.names.key("name", spawned.name), mapping={"swarm": SLUG, "code": code})
    store.start_execution(SLUG, AgentRecord(spawned.name, "eng", "proof", seat=f"eng-1@{SLUG}", hive=grant["id"]))
    monkeypatch.setattr(ledger, "BASE", "")
    monkeypatch.delenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", spawned.name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", grant["ledger_credential"])
    monkeypatch.setenv("SWARM_URL", silent)
    for key, value in spawned.task["endpoints"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(hosted_probe, "_answers", {hosted: False})

    ledger.cmd_join(SimpleNamespace(slug=SLUG, name=spawned.name, role="member"))

    assert json.loads(capsys.readouterr().out) == {"joined": spawned.name, "slug": SLUG, "role": "member"}
    assert ledger.call(SLUG)["_meta"]["members"][spawned.name]["role"] == "member"


def test_a_hive_launch_without_a_hosted_ledger_hands_only_the_swarm_api(monkeypatch):
    monkeypatch.delenv(hosted_probe.HOSTED_ENV, raising=False)
    world = World(monkeypatch)
    world.launch(FIRST)
    [spawned] = world.runtime.requests

    assert hosted_probe.HOSTED_ENV not in spawned.task["endpoints"]


def test_a_later_command_reuses_a_fresh_probe_answer_instead_of_waiting_on_a_silent_ledger(
    silent, tmp_path, monkeypatch
):
    environ = workstation(**TOKEN, **{hosted_probe.HOSTED_ENV: silent})
    assert ledger_link.base(environ) == WORKSTATION
    monkeypatch.setattr(hosted_probe, "_answers", {})
    monkeypatch.setattr(hosted_probe, "healthy", lambda url: pytest.fail("probed again inside the cache window"))

    assert ledger_link.base(environ) == WORKSTATION


def test_a_later_command_reuses_a_fresh_answer_that_the_hosted_ledger_answered(answering, monkeypatch):
    environ = workstation(**TOKEN, **{hosted_probe.HOSTED_ENV: answering})
    assert ledger_link.base(environ) == answering
    monkeypatch.setattr(hosted_probe, "_answers", {})
    monkeypatch.setattr(hosted_probe, "healthy", lambda url: pytest.fail("probed again inside the cache window"))

    assert ledger_link.base(environ) == answering


def test_the_probe_waits_only_the_probe_window(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, timeout))
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(ledger_link, "urlopen", urlopen)

    assert hosted_probe.answered("https://hosted.example", {})
    assert seen == [("https://hosted.example/healthz", hosted_probe.PROBE_S)]


def test_the_probe_answer_lives_in_the_agentihooks_home():
    assert PROBE_FILE({"AGENTIHOOKS_HOME": "/srv/hooks"}) == Path("/srv/hooks/hosted-ledger-probe.json")
    assert PROBE_FILE({}) == Path.home() / ".agentihooks" / "hosted-ledger-probe.json"


def test_a_probe_answer_exactly_one_window_old_is_checked_again(answering, tmp_path, monkeypatch):
    monkeypatch.setattr(hosted_probe.time, "time", lambda: 1000.0)
    stale = {"url": answering, "at": 1000.0 - hosted_probe.PROBE_TTL_S, "answers": False}
    (tmp_path / "hosted-ledger-probe.json").write_text(json.dumps(stale))

    assert hosted_probe.answered(answering, {})


def test_a_probe_answer_written_this_instant_is_reused(silent, tmp_path, monkeypatch):
    monkeypatch.setattr(hosted_probe.time, "time", lambda: 1000.0)
    fresh = {"url": silent, "at": 1000.0, "answers": True}
    (tmp_path / "hosted-ledger-probe.json").write_text(json.dumps(fresh))

    assert hosted_probe.answered(silent, {})


def test_a_session_keeps_its_probe_answer_in_its_own_agentihooks_home(answering, tmp_path, monkeypatch):
    monkeypatch.setattr(hosted_probe, "probe_file", PROBE_FILE)
    home = tmp_path / "hooks-home"
    home.mkdir()
    environ = workstation(**TOKEN, AGENTIHOOKS_HOME=str(home), **{hosted_probe.HOSTED_ENV: answering})

    assert ledger_link.base(environ) == answering
    assert json.loads((home / "hosted-ledger-probe.json").read_text())["url"] == answering


def test_the_hosted_address_drops_only_spaces_and_trailing_slashes():
    assert (
        hosted_probe.address({hosted_probe.HOSTED_ENV: " https://hosted.example/BOX/ "}) == "https://hosted.example/BOX"
    )
    assert hosted_probe.address({}) == ""


@pytest.mark.parametrize(
    ("status", "body"), [(200, b"<html>sign in</html>"), (200, b'["not", "health"]'), (503, b"{}")]
)
def test_an_access_login_page_or_an_error_status_is_not_the_hosted_ledger_answering(status, body):
    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        environ = workstation(**TOKEN, **{hosted_probe.HOSTED_ENV: url})
        assert ledger_link.base(environ) == WORKSTATION
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_stale_probe_answer_is_checked_again(answering, tmp_path, monkeypatch):
    environ = workstation(**TOKEN, **{hosted_probe.HOSTED_ENV: answering})
    stale = {"url": answering, "at": 0, "answers": False}
    (tmp_path / "hosted-ledger-probe.json").write_text(json.dumps(stale))

    assert ledger_link.base(environ) == answering


def test_the_workstation_tick_hands_each_spawned_agent_a_hosted_token(monkeypatch):
    class Store:
        def slugs(self):
            return []

    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.setenv(hosted_probe.HOSTED_ENV, "https://hosted.example")
    monkeypatch.setattr(hosted_migration, "server_secret", lambda: "hosted-secret")

    with authority_context.for_tick(Store()):
        handed = runtime._ledger_token("demo", "engineer@demo-1")

    assert handed == {
        "AGENTIHOOKS_LEDGER_AGENT_TOKEN": ledger_authority.agent_token("hosted-secret", "demo", "engineer@demo-1")
    }


def test_the_workstation_tick_survives_a_malformed_secret_store_answer(monkeypatch):
    class Store:
        def slugs(self):
            return []

    def malformed():
        raise KeyError("data")

    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    monkeypatch.setenv(hosted_probe.HOSTED_ENV, "https://hosted.example")
    monkeypatch.setattr(hosted_migration, "server_secret", malformed)

    with authority_context.for_tick(Store()):
        assert runtime._ledger_token("demo", "engineer@demo-1") == {}


@pytest.mark.parametrize("cached", [{"at": "soon", "answers": False}, {"at": 10**12, "answers": False}, ["junk"]])
def test_a_corrupt_or_future_probe_answer_is_checked_again(answering, tmp_path, cached):
    environ = workstation(**TOKEN, **{hosted_probe.HOSTED_ENV: answering})
    if isinstance(cached, dict):
        cached = {**cached, "url": answering}
    (tmp_path / "hosted-ledger-probe.json").write_text(json.dumps(cached))

    assert ledger_link.base(environ) == answering
