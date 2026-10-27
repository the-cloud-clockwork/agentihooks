import io
import json
import stat
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import openbao_kv
from scripts.swarm import brain_binding, brain_place, cli

pytestmark = pytest.mark.unit

RECORD = "k8s/swarm-brain-client"
API_FIXTURE, MCP_FIXTURE = "fxa1-place", "fxm1-place"
BINDING = {
    "credentials": {
        "api": {"header": "Authorization", "property": "BRAIN_HTTP_TOKEN", "scheme": "Bearer"},
        "mcp": {"header": "X-API-Key", "property": "MCP_API_KEY", "scheme": ""},
        "record": RECORD,
    },
    "instance": "swarm-brain",
    "origins": {
        "cluster": {
            "api": "http://swarm-brain-api.swarm-brain.svc:8080",
            "mcp": "http://swarm-brain-mcp.swarm-brain.svc:8080/mcp",
        },
        "lan": {
            "api": "https://swarm-brain-api-internal.example.test",
            "mcp": "https://swarm-brain-mcp-internal.example.test/mcp",
        },
    },
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StubStore:
    def __init__(self, records):
        self.records = records
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request.full_url, request.get_header("X-vault-token"), request.get_method(), timeout))
        path = request.full_url.split("/v1/secret/data/", 1)[1]
        if path not in self.records:
            raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, io.BytesIO(b'{"errors": []}'))
        return _Response(json.dumps({"data": {"data": self.records[path], "metadata": {"version": 2}}}).encode())


@pytest.fixture
def home(tmp_path):
    state = tmp_path / ".agentihooks"
    state.mkdir()
    (state / ".env").write_text("AGENTIHOOKS_PLACE_FIXTURE=1\n")
    return state


def _environ(home):
    return {"HOME": str(home.parent), "AGENTIHOOKS_HOME": str(home), "PATH": "/usr/bin:/bin"}


def _bao(store):
    return openbao_kv.OpenBao("https://bao.example.test/", "bao-fixture-token", store)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_place_writes_the_binding_and_both_credentials_owner_only_and_reports_bound(home):
    store = StubStore({RECORD: {"BRAIN_HTTP_TOKEN": API_FIXTURE, "MCP_API_KEY": MCP_FIXTURE, "OTHER": "x"}})
    out = io.StringIO()

    assert brain_place.place(_environ(home), _bao(store), json.dumps(BINDING), out) == 0

    binding_file = home / "swarm" / "brain-binding.json"
    env_file = home / "swarm-brain.env"
    assert json.loads(binding_file.read_text()) == BINDING
    assert env_file.read_text() == f"SWARM_BRAIN_API_TOKEN={API_FIXTURE}\nSWARM_BRAIN_MCP_KEY={MCP_FIXTURE}\n"
    assert _mode(binding_file) == _mode(env_file) == 0o600
    assert _mode(binding_file.parent) == 0o700
    assert out.getvalue() == "brain-binding.json 0600\nswarm-brain.env 0600\nbrain binding bound\n"
    assert store.requests == [
        ("https://bao.example.test/v1/secret/data/k8s/swarm-brain-client", "bao-fixture-token", "GET", 20)
    ]
    assert sorted(path.name for path in home.rglob("*")) == [".env", "brain-binding.json", "swarm", "swarm-brain.env"]
    decided = brain_binding.decide(_environ(home), {"SWARM_BRAIN_API_TOKEN": "a", "SWARM_BRAIN_MCP_KEY": "m"})
    assert decided["status"] == brain_binding.BOUND


def test_place_never_prints_a_credential(home, capsys):
    store = StubStore({RECORD: {"BRAIN_HTTP_TOKEN": API_FIXTURE, "MCP_API_KEY": MCP_FIXTURE}})
    out = io.StringIO()

    brain_place.place(_environ(home), _bao(store), json.dumps(BINDING), out)

    printed = out.getvalue() + "".join(capsys.readouterr())
    assert API_FIXTURE not in printed and MCP_FIXTURE not in printed


def test_place_never_prints_a_password_held_in_a_binding_origin(home):
    doc = json.loads(json.dumps(BINDING))
    doc["origins"]["lan"]["api"] = "https://svc:fxpw-place@swarm-brain-api-internal.example.test"
    store = StubStore({RECORD: {"BRAIN_HTTP_TOKEN": API_FIXTURE, "MCP_API_KEY": MCP_FIXTURE}})
    out = io.StringIO()

    assert brain_place.place(_environ(home), _bao(store), json.dumps(doc), out) == 1

    expected = "endpoint https://swarm-brain-api-internal.example.test is not a plain origin"
    assert out.getvalue().endswith(f"brain binding refused: {expected}\n")
    assert "fxpw-place" not in out.getvalue()


def test_place_honours_the_binding_variable_names_and_quotes_values(home):
    doc = json.loads(json.dumps(BINDING))
    doc["credentials"]["api"]["env"] = "SWARM_BRAIN_OTHER_TOKEN"
    store = StubStore({RECORD: {"BRAIN_HTTP_TOKEN": "a b", "MCP_API_KEY": MCP_FIXTURE}})

    assert brain_place.place(_environ(home), _bao(store), json.dumps(doc), io.StringIO()) == 0

    text = (home / "swarm-brain.env").read_text()
    assert text == f"SWARM_BRAIN_OTHER_TOKEN='a b'\nSWARM_BRAIN_MCP_KEY={MCP_FIXTURE}\n"


def test_place_replaces_an_older_placement(home):
    (home / "swarm").mkdir(mode=0o700)
    (home / "swarm" / "brain-binding.json").write_text("{}")
    (home / "swarm-brain.env").write_text("SWARM_BRAIN_API_TOKEN=old\n")
    store = StubStore({RECORD: {"BRAIN_HTTP_TOKEN": API_FIXTURE, "MCP_API_KEY": MCP_FIXTURE}})

    assert brain_place.place(_environ(home), _bao(store), json.dumps(BINDING), io.StringIO()) == 0

    assert API_FIXTURE in (home / "swarm-brain.env").read_text()
    assert _mode(home / "swarm-brain.env") == 0o600
    assert not list(home.rglob("*.next"))


def test_place_reports_a_refused_decision_with_its_reason(home):
    doc = json.loads(json.dumps(BINDING))
    doc["origins"]["lan"]["api"] = "http://swarm-brain-api-internal.example.test"
    store = StubStore({RECORD: {"BRAIN_HTTP_TOKEN": API_FIXTURE, "MCP_API_KEY": MCP_FIXTURE}})
    out = io.StringIO()

    assert brain_place.place(_environ(home), _bao(store), json.dumps(doc), out) == 1

    expected = "brain binding refused: endpoint http://swarm-brain-api-internal.example.test is not an authorized"
    assert out.getvalue().endswith(f"{expected} brain origin\n")


@pytest.mark.parametrize(
    ("change", "records", "reason"),
    [
        (lambda doc: doc.pop("credentials"), {}, "the binding names no OpenBao record"),
        (lambda doc: doc["credentials"].pop("record"), {}, "the binding names no OpenBao record"),
        (lambda doc: doc["credentials"]["mcp"].pop("property"), {}, "the binding names no mcp property"),
        (lambda doc: doc["credentials"]["api"].pop("property"), {}, "the binding names no api property"),
        (
            lambda doc: doc["credentials"]["api"].update(env="PATH"),
            {},
            "the api credential reference must be a SWARM_BRAIN_ variable",
        ),
        (lambda doc: None, {}, f"OpenBao {RECORD} lacks BRAIN_HTTP_TOKEN, MCP_API_KEY"),
        (lambda doc: None, {RECORD: {"BRAIN_HTTP_TOKEN": API_FIXTURE}}, f"OpenBao {RECORD} lacks MCP_API_KEY"),
        (
            lambda doc: None,
            {RECORD: {"BRAIN_HTTP_TOKEN": 7, "MCP_API_KEY": {"k": "v"}}},
            f"OpenBao {RECORD} lacks BRAIN_HTTP_TOKEN, MCP_API_KEY",
        ),
        (lambda doc: doc["credentials"].update(mcp="x"), {}, "the binding names no mcp property"),
    ],
)
def test_place_refuses_and_writes_nothing(home, change, records, reason):
    doc = json.loads(json.dumps(BINDING))
    change(doc)
    out = io.StringIO()

    assert brain_place.place(_environ(home), _bao(StubStore(records)), json.dumps(doc), out) == 1

    assert out.getvalue() == f"brain-place: {reason}; nothing was written\n"
    assert sorted(path.name for path in home.iterdir()) == [".env"]


def test_place_refuses_a_binding_that_is_not_json(home):
    out = io.StringIO()

    assert brain_place.place(_environ(home), _bao(StubStore({})), "not json", out) == 1

    assert out.getvalue() == "brain-place: the binding is not a JSON object; nothing was written\n"


def test_place_reports_an_openbao_failure_without_writing(home):
    def broken(request, timeout):
        raise urllib.error.URLError("unreachable")

    out = io.StringIO()

    assert brain_place.place(_environ(home), _bao(broken), json.dumps(BINDING), out) == 1

    assert out.getvalue() == "brain-place: OpenBao request failed: unreachable; nothing was written\n"
    assert sorted(path.name for path in home.iterdir()) == [".env"]


def test_main_reads_a_binding_file(home, tmp_path):
    source = tmp_path / "binding.json"
    source.write_text(json.dumps(BINDING))
    store = StubStore({RECORD: {"BRAIN_HTTP_TOKEN": API_FIXTURE, "MCP_API_KEY": MCP_FIXTURE}})
    environ = {**_environ(home), "OPENBAO_URL": "https://bao.example.test", "OPENBAO_TOKEN": "t"}
    out = io.StringIO()

    assert brain_place.main(["--binding", str(source)], environ, out, store) == 0

    assert out.getvalue().endswith("brain binding bound\n")


def _kubectl(monkeypatch, run):
    monkeypatch.setattr(brain_place, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))


def test_main_reads_the_cluster_configmap_by_default(home, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(BINDING), "")

    _kubectl(monkeypatch, run)
    store = StubStore({RECORD: {"BRAIN_HTTP_TOKEN": API_FIXTURE, "MCP_API_KEY": MCP_FIXTURE}})
    environ = {**_environ(home), "OPENBAO_URL": "https://bao.example.test", "OPENBAO_TOKEN": "t"}

    assert brain_place.main([], environ, io.StringIO(), store) == 0

    assert calls == [
        (
            [
                "kubectl",
                "get",
                "configmap",
                "swarm-brain-binding",
                "-n",
                "swarm-brain",
                "-o",
                "jsonpath={.data.binding\\.json}",
            ],
            {"capture_output": True, "text": True, "timeout": 30},
        )
    ]


@pytest.mark.parametrize(("code", "stdout"), [(1, json.dumps(BINDING)), (0, "  \n")])
def test_main_refuses_an_unreadable_configmap(home, monkeypatch, code, stdout):
    _kubectl(monkeypatch, lambda argv, **kw: subprocess.CompletedProcess(argv, code, stdout, ""))
    environ = {**_environ(home), "OPENBAO_URL": "https://bao.example.test", "OPENBAO_TOKEN": "t"}
    out = io.StringIO()

    assert brain_place.main([], environ, out, StubStore({})) == 1

    assert out.getvalue() == "brain-place: the swarm-brain-binding ConfigMap could not be read; nothing was written\n"


@pytest.mark.parametrize("error", [FileNotFoundError("kubectl"), subprocess.TimeoutExpired("kubectl", 30)])
def test_main_refuses_when_kubectl_cannot_run(home, monkeypatch, error):
    def run(argv, **kwargs):
        raise error

    _kubectl(monkeypatch, run)
    environ = {**_environ(home), "OPENBAO_URL": "https://bao.example.test", "OPENBAO_TOKEN": "t"}
    out = io.StringIO()

    assert brain_place.main([], environ, out, StubStore({})) == 1

    assert out.getvalue() == "brain-place: the swarm-brain-binding ConfigMap could not be read; nothing was written\n"


def test_main_refuses_a_binding_file_that_is_not_text(home, tmp_path):
    source = tmp_path / "binding.json"
    source.write_bytes(b"\xff\xfe\xfa")
    environ = {**_environ(home), "OPENBAO_URL": "https://bao.example.test", "OPENBAO_TOKEN": "t"}
    out = io.StringIO()

    assert brain_place.main(["--binding", str(source)], environ, out, StubStore({})) == 1

    assert out.getvalue() == "brain-place: the binding file could not be read; nothing was written\n"


def test_main_refuses_a_missing_binding_file(home, tmp_path):
    environ = {**_environ(home), "OPENBAO_URL": "https://bao.example.test", "OPENBAO_TOKEN": "t"}
    out = io.StringIO()

    assert brain_place.main(["--binding", str(tmp_path / "absent.json")], environ, out, StubStore({})) == 1

    assert out.getvalue() == "brain-place: the binding file could not be read; nothing was written\n"


@pytest.mark.parametrize("missing", ["OPENBAO_URL", "OPENBAO_TOKEN"])
def test_main_needs_openbao(home, missing):
    environ = {**_environ(home), "OPENBAO_URL": "https://bao.example.test", "OPENBAO_TOKEN": "t"}
    del environ[missing]
    out = io.StringIO()

    assert brain_place.main([], environ, out, StubStore({})) == 1

    assert out.getvalue() == "brain-place: OPENBAO_URL and OPENBAO_TOKEN must be set; nothing was written\n"


def test_swarm_command_line_routes_brain_place(monkeypatch):
    seen = []
    monkeypatch.setattr(brain_place, "main", lambda argv: seen.append(argv) or 7)

    assert cli.main(["brain-place", "--binding", "x.json"]) == 7

    assert seen == [["--binding", "x.json"]]


def test_main_defaults_to_the_process_environment(monkeypatch, home):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    monkeypatch.delenv("OPENBAO_URL", raising=False)
    monkeypatch.delenv("OPENBAO_TOKEN", raising=False)

    assert brain_place.main([]) == 1

    assert not Path(home / "swarm").exists()
