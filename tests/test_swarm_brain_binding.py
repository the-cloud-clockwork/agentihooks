import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts import herdr_host, init_agent, operator_env
from scripts.swarm import brain_binding
from tests.test_profile_render import _write
from tests.test_profile_render import world as world

pytestmark = pytest.mark.unit

ROLES = ("master", "dispatcher", "planner", "engineer", "cicd")
HARNESSES = ("claude", "codex")
API = "https://swarm-brain-api-internal.example.test"
MCP = "https://swarm-brain-mcp-internal.example.test/mcp"
CLUSTER_API = "http://swarm-brain-api.swarm-brain.svc:8080"
CLUSTER_MCP = "http://swarm-brain-mcp.swarm-brain.svc:8080/mcp"
PERSONAL = "http://localhost:8104"
REPO_ENDPOINT = "https://repo-endpoint.example.test"
API_FIXTURE, MCP_FIXTURE, PERSONAL_FIXTURE = "fxa1", "fxm1", "fxp1"
CREDENTIALS = {"SWARM_BRAIN_API_TOKEN": API_FIXTURE, "SWARM_BRAIN_MCP_KEY": MCP_FIXTURE}
RELOADED = (
    "BRAIN_ENABLED",
    "BRAIN_SOURCE_TYPE",
    "BRAIN_SOURCE_PATH",
    "BRAIN_CHANNEL",
    "BRAIN_REFRESH_TOOL_CALLS",
    "BRAIN_URL",
    "BRAIN_HTTP_TOKEN",
    "BRAIN_HTTP_TIMEOUT",
    "BRAIN_WRITER_ENABLED",
    "AMYGDALA_ENABLED",
    "AMYGDALA_SIGNAL_PATH",
    "_ENV_FINGERPRINT",
)
BRAIN_KEYS = (
    "BRAIN_URL",
    "BRAIN_HTTP_TOKEN",
    "KB_ROUTER_TOKEN",
    "BRAIN_ENABLED",
    "BRAIN_SOURCE_TYPE",
    "BRAIN_WRITER_ENABLED",
    "AGENTIHOOKS_SWARM_BRAIN",
)
REFUSED = {"status": "refused", "reason": "r"}
BOUND = {
    "status": "bound",
    "instance": "swarm-brain",
    "site": "lan",
    "api": API,
    "mcp": MCP,
    "api_header": "Authorization",
    "api_scheme": "Bearer",
    "api_env": "SWARM_BRAIN_API_TOKEN",
    "mcp_header": "X-API-Key",
    "mcp_scheme": "",
    "mcp_env": "SWARM_BRAIN_MCP_KEY",
}
MANAGED_SERVER = {"type": "http", "url": MCP, "headers": {"X-API-Key": "${SWARM_BRAIN_MCP_KEY}"}}


def _document(lan=None, cluster=None, credentials=None):
    return {
        "credentials": credentials
        or {
            "api": {"header": "Authorization", "property": "BRAIN_HTTP_TOKEN", "scheme": "Bearer"},
            "mcp": {"header": "X-API-Key", "property": "MCP_API_KEY", "scheme": ""},
            "record": "k8s/swarm-brain-client",
        },
        "instance": "swarm-brain",
        "origins": {
            "cluster": cluster or {"api": CLUSTER_API, "mcp": CLUSTER_MCP},
            "lan": lan or {"api": API, "mcp": MCP},
        },
    }


def _credentials(**changes):
    credentials = _document()["credentials"]
    for path, value in changes.items():
        kind, key = path.split("__")
        credentials[kind][key] = value
    return credentials


def _binding(home: Path, doc=None, mode=0o600) -> Path:
    path = home / "swarm" / "brain-binding.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc or _document()))
    path.chmod(mode)
    return path


def _environ(tmp_path, **extra):
    return {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(tmp_path / ".agentihooks"), **CREDENTIALS, **extra}


def _absent(monkeypatch, *keys):
    for key in keys:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _managed(binding: dict) -> dict:
    return {brain_binding.VARIABLE: json.dumps(binding)}


def _personal_files(tmp_path):
    personal = f"BRAIN_URL={PERSONAL}\nBRAIN_HTTP_TOKEN={PERSONAL_FIXTURE}\n"
    _write(tmp_path / ".agentihooks" / ".env", personal)
    _write(tmp_path / ".agentihooks" / "brain.env", f"KB_ROUTER_TOKEN={PERSONAL_FIXTURE}\nBRAIN_SOURCE_TYPE=file\n")
    _write(tmp_path / ".agentibrain" / ".env", personal)


def test_a_lan_launch_binds_the_published_origins_and_names_only_references(tmp_path):
    _binding(tmp_path / ".agentihooks")

    bound = brain_binding.resolve(_environ(tmp_path))

    assert bound == BOUND
    assert API_FIXTURE not in json.dumps(bound) and MCP_FIXTURE not in json.dumps(bound)


def test_a_cluster_launch_binds_service_dns_and_an_explicit_binding_path(tmp_path):
    path = _binding(tmp_path / "elsewhere")
    environ = _environ(tmp_path, KUBERNETES_SERVICE_HOST="10.0.0.1", AGENTIHOOKS_SWARM_BRAIN_BINDING=str(path))

    assert brain_binding.resolve(environ) == {**BOUND, "site": "cluster", "api": CLUSTER_API, "mcp": CLUSTER_MCP}


def test_named_credential_references_replace_the_defaults(tmp_path):
    credentials = _credentials(api__env="SWARM_BRAIN_HIVE_API", mcp__env="SWARM_BRAIN_HIVE_MCP")
    _binding(tmp_path / ".agentihooks", _document(credentials=credentials))
    environ = {**_environ(tmp_path), "SWARM_BRAIN_HIVE_API": "a", "SWARM_BRAIN_HIVE_MCP": "b"}

    bound = brain_binding.resolve(environ)

    assert (bound["api_env"], bound["mcp_env"]) == ("SWARM_BRAIN_HIVE_API", "SWARM_BRAIN_HIVE_MCP")


def _origin_doc(api):
    return _document(lan={"api": api, "mcp": MCP})


def _credential_doc(**changes):
    return _document(credentials=_credentials(**changes))


UNAUTHORIZED = "is not an authorized brain origin"
NOT_PLAIN = "is not a plain origin"
MALFORMED = "the api credential header is malformed"
NOT_SWARM = "credential reference must be a SWARM_BRAIN_ variable"


@pytest.mark.parametrize(
    ("doc", "unset", "reason"),
    [
        (_origin_doc("http://brain.example.test"), (), f"endpoint http://brain.example.test {UNAUTHORIZED}"),
        (_origin_doc(CLUSTER_API), (), f"endpoint {CLUSTER_API} {UNAUTHORIZED}"),
        (_origin_doc("ftp://brain.example.test"), (), f"endpoint ftp://brain.example.test {UNAUTHORIZED}"),
        (
            _origin_doc("https://someone@brain.example.test"),
            (),
            f"endpoint https://someone@brain.example.test {NOT_PLAIN}",
        ),
        (_origin_doc(API + "?audience=x"), (), f"endpoint {API} {NOT_PLAIN}"),
        (_origin_doc(API + "#x"), (), f"endpoint {API} {NOT_PLAIN}"),
        (_origin_doc("https:///feed"), (), f"endpoint https:// {NOT_PLAIN}"),
        (_origin_doc(7), (), "the binding names no endpoint"),
        (_document(lan={"api": API, "mcp": API + "/mcp"}), (), "the API and MCP endpoints share one origin"),
        ({**_document(), "origins": {"cluster": {}}}, (), "the binding has no lan origins"),
        (_document(credentials={"mcp": {"header": "X-API-Key"}}), (), "the binding names no api credential"),
        (_credential_doc(api__header="Bad Header"), (), MALFORMED),
        (_credential_doc(api__header=3), (), MALFORMED),
        (_credential_doc(api__scheme=3), (), MALFORMED),
        (_credential_doc(api__scheme="Bearer evil"), (), MALFORMED),
        (_credential_doc(mcp__scheme="Token"), (), "the mcp credential header is malformed"),
        (_credential_doc(mcp__env="BRAIN_HTTP_TOKEN"), (), f"the mcp {NOT_SWARM}"),
        (_credential_doc(api__env="KB_ROUTER_TOKEN"), (), f"the api {NOT_SWARM}"),
        (_credential_doc(api__env="ANTHROPIC_API_KEY"), (), f"the api {NOT_SWARM}"),
        (_credential_doc(api__env="SWARM_BRAIN_"), (), f"the api {NOT_SWARM}"),
        (_credential_doc(api__env=4), (), f"the api {NOT_SWARM}"),
        (_credential_doc(api__header="X-API-Key"), (), "the API credential must be an Authorization Bearer token"),
        (_credential_doc(api__scheme=""), (), "the API credential must be an Authorization Bearer token"),
        (_document(), ("SWARM_BRAIN_API_TOKEN",), "credential SWARM_BRAIN_API_TOKEN is unset"),
        (_document(), ("SWARM_BRAIN_MCP_KEY",), "credential SWARM_BRAIN_MCP_KEY is unset"),
        (
            _document(),
            ("SWARM_BRAIN_API_TOKEN", "SWARM_BRAIN_MCP_KEY"),
            "credential SWARM_BRAIN_API_TOKEN, SWARM_BRAIN_MCP_KEY is unset",
        ),
    ],
)
def test_an_unusable_binding_refuses_the_managed_brain(tmp_path, doc, unset, reason):
    _binding(tmp_path / ".agentihooks", doc)
    environ = {k: v for k, v in _environ(tmp_path).items() if k not in unset}

    assert brain_binding.decide(environ) == {"status": "refused", "reason": reason}


def test_a_binding_must_be_an_operator_owned_regular_file(tmp_path):
    home = tmp_path / ".agentihooks"
    path = home / "swarm" / "brain-binding.json"

    def refused(reason):
        return brain_binding.decide(_environ(tmp_path)) == {"status": "refused", "reason": reason}

    assert refused(f"no swarm brain binding at {path}")
    for mode in (0o620, 0o602):
        _binding(home, mode=mode)
        assert refused(f"swarm brain binding {path} is not an operator owned file")
    real = _binding(tmp_path / "repo")
    path.unlink()
    path.symlink_to(real)
    assert refused(f"swarm brain binding {path} is not an operator owned file")
    path.unlink()
    path.write_text("{not json")
    path.chmod(0o600)
    assert refused(f"swarm brain binding {path} is not readable JSON")
    path.write_text("[]")
    assert refused(f"swarm brain binding {path} is not a JSON object")


def test_the_binding_path_defaults_below_home_without_an_agentihooks_home(tmp_path):
    assert brain_binding.path({"HOME": str(tmp_path)}) == tmp_path / ".agentihooks" / "swarm" / "brain-binding.json"
    elsewhere = {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(tmp_path / "state")}
    assert brain_binding.path(elsewhere) == tmp_path / "state" / "swarm" / "brain-binding.json"


def test_trailing_slashes_and_a_missing_instance_name_are_normalized(tmp_path):
    doc = _document(lan={"api": API + "/", "mcp": MCP + "/"})
    del doc["instance"]
    _binding(tmp_path / ".agentihooks", doc)

    assert brain_binding.resolve(_environ(tmp_path)) == {**BOUND, "instance": ""}


def test_an_owner_executable_binding_is_accepted_and_a_directory_is_refused(tmp_path):
    path = _binding(tmp_path / ".agentihooks", mode=0o711)
    assert brain_binding.decide(_environ(tmp_path)) == BOUND
    path.unlink()
    path.mkdir(mode=0o700)
    assert brain_binding.decide(_environ(tmp_path)) == {
        "status": "refused",
        "reason": f"swarm brain binding {path} is not an operator owned file",
    }


def test_the_managed_brain_is_the_only_agentibrain_after_the_overlay():
    servers = {"agentibrain": {"type": "http", "url": PERSONAL + "/mcp"}, "other": {"command": "x"}}

    assert brain_binding.apply(servers, _managed(BOUND)) == {"other": {"command": "x"}, "agentibrain": MANAGED_SERVER}
    schemed = {**BOUND, "mcp_scheme": "Bearer", "mcp_header": "Authorization"}
    assert brain_binding.apply(servers, _managed(schemed))["agentibrain"]["headers"] == {
        "Authorization": "Bearer ${SWARM_BRAIN_MCP_KEY}"
    }
    assert brain_binding.apply(servers, _managed(REFUSED)) == {"other": {"command": "x"}}
    assert brain_binding.apply(servers, {}) is servers


def _shell(script: str, environ: dict) -> dict:
    probe = "".join(
        f'printf "{key}=%s|%s\\n" "${{{key}+set}}" "${{{key}:-}}"\n'
        for key in (*BRAIN_KEYS[:5], brain_binding.VARIABLE)
    )
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script + probe], env=environ, capture_output=True, text=True, check=True
    )
    return dict(line.split("=", 1) for line in done.stdout.splitlines())


def test_bound_exports_win_over_conflicting_personal_env_files(tmp_path):
    _personal_files(tmp_path)
    environ = {**_environ(tmp_path), "PATH": os.environ["PATH"], **_managed(BOUND)}

    seen = _shell(operator_env.source_line(environ) + brain_binding.exports(environ), environ)

    assert seen == {
        "BRAIN_URL": f"set|{API}",
        "BRAIN_HTTP_TOKEN": f"set|{API_FIXTURE}",
        "KB_ROUTER_TOKEN": "|",
        "BRAIN_ENABLED": "set|true",
        "BRAIN_SOURCE_TYPE": "set|none",
        brain_binding.VARIABLE: f"set|{json.dumps(BOUND)}",
    }
    assert API_FIXTURE not in brain_binding.exports(environ)


@pytest.mark.parametrize("missing", ["SWARM_BRAIN_API_TOKEN", "SWARM_BRAIN_MCP_KEY"])
def test_a_credential_missing_in_the_launch_shell_refuses_the_brain(tmp_path, missing):
    _personal_files(tmp_path)
    environ = {**_environ(tmp_path), "PATH": os.environ["PATH"], **_managed(BOUND)}
    shell = {key: value for key, value in environ.items() if key != missing}
    script = operator_env.source_line(environ) + brain_binding.exports(environ)

    seen = _shell(script, shell)

    missed = {"status": "refused", "reason": "a brain credential is unset in the launch shell"}
    assert seen == {
        "BRAIN_URL": "|",
        "BRAIN_HTTP_TOKEN": "|",
        "KB_ROUTER_TOKEN": "|",
        "BRAIN_ENABLED": "set|false",
        "BRAIN_SOURCE_TYPE": "set|none",
        brain_binding.VARIABLE: f"set|{json.dumps(missed)}",
    }


def test_refused_exports_drop_every_personal_brain_setting(tmp_path):
    _personal_files(tmp_path)
    environ = {**_environ(tmp_path), "PATH": os.environ["PATH"], **_managed(REFUSED)}

    seen = _shell(operator_env.source_line(environ) + brain_binding.exports(environ), environ)

    assert seen == {
        "BRAIN_URL": "|",
        "BRAIN_HTTP_TOKEN": "|",
        "KB_ROUTER_TOKEN": "|",
        "BRAIN_ENABLED": "set|false",
        "BRAIN_SOURCE_TYPE": "set|none",
        brain_binding.VARIABLE: f"set|{json.dumps(REFUSED)}",
    }
    assert brain_binding.exports({}) == ""


def test_the_launch_report_and_record_carry_the_sanitized_binding():
    raw = json.dumps(BOUND)

    assert brain_binding.report(_managed(BOUND)) == [f"brain_binding={raw}"]
    assert brain_binding.report({}) == []
    assert brain_binding.record({"brain_binding": raw}) == {"brain": BOUND}
    assert brain_binding.record({}) == {}


def test_the_credential_goes_only_to_the_bound_api_origin(tmp_path):
    _binding(tmp_path / ".agentihooks")
    environ = {**_environ(tmp_path), **_managed(BOUND)}

    assert brain_binding.audience({**environ, "BRAIN_URL": API})
    assert brain_binding.audience({**environ, "BRAIN_URL": API.upper() + "/"})
    assert not brain_binding.audience({**environ, "BRAIN_URL": REPO_ENDPOINT})
    assert not brain_binding.audience({**environ, "BRAIN_URL": PERSONAL})
    assert brain_binding.audience(environ) is False
    forged = {**BOUND, "api": REPO_ENDPOINT}
    assert not brain_binding.audience({**environ, **_managed(forged), "BRAIN_URL": REPO_ENDPOINT})
    assert not brain_binding.audience({**environ, **_managed(REFUSED), "BRAIN_URL": API})
    assert not brain_binding.audience({**_environ(tmp_path), "BRAIN_URL": API})
    (tmp_path / ".agentihooks" / "swarm" / "brain-binding.json").unlink()
    assert not brain_binding.audience({**environ, "BRAIN_URL": API})


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    from hooks import config

    _absent(monkeypatch, *BRAIN_KEYS, "KUBERNETES_SERVICE_HOST")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / ".agentihooks"))
    monkeypatch.setenv("AGENTIBRAIN_HOME", str(tmp_path / ".agentibrain"))
    for key, value in CREDENTIALS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "_BRAIN_FILE_OWNED", set())
    monkeypatch.setattr(config, "_FILE_OWNED_KEYS", set())
    for name in RELOADED:
        monkeypatch.setattr(config, name, getattr(config, name))
    _personal_files(tmp_path)
    return config


def test_a_managed_session_never_adopts_the_personal_brain_dotenv(hook_env, monkeypatch):
    monkeypatch.setenv(brain_binding.VARIABLE, json.dumps(REFUSED))

    hook_env._load_brain_env()

    assert "BRAIN_URL" not in os.environ and "BRAIN_HTTP_TOKEN" not in os.environ
    hook_env._guard_swarm_brain()

    assert (os.environ["BRAIN_ENABLED"], os.environ["BRAIN_WRITER_ENABLED"]) == ("false", "false")
    assert os.environ["BRAIN_SOURCE_TYPE"] == "none"


def test_a_personal_session_keeps_its_brain_dotenv(hook_env):
    hook_env._load_brain_env()
    hook_env._guard_swarm_brain()

    assert os.environ["BRAIN_URL"] == PERSONAL
    assert "BRAIN_SOURCE_TYPE" not in os.environ and "BRAIN_ENABLED" not in os.environ


def test_a_bound_session_keeps_its_api_and_drops_a_repository_endpoint(hook_env, monkeypatch, tmp_path):
    _binding(tmp_path / ".agentihooks")
    monkeypatch.setenv(brain_binding.VARIABLE, json.dumps(BOUND))
    monkeypatch.setenv("BRAIN_URL", API)
    monkeypatch.setenv("BRAIN_HTTP_TOKEN", API_FIXTURE)

    hook_env._guard_swarm_brain()

    assert (os.environ["BRAIN_URL"], os.environ["BRAIN_HTTP_TOKEN"]) == (API, API_FIXTURE)
    assert os.environ["BRAIN_SOURCE_TYPE"] == "none" and "BRAIN_ENABLED" not in os.environ
    monkeypatch.setenv("BRAIN_URL", REPO_ENDPOINT)
    monkeypatch.setenv("KB_ROUTER_TOKEN", API_FIXTURE)

    hook_env._guard_swarm_brain()

    assert not {"BRAIN_URL", "BRAIN_HTTP_TOKEN", "KB_ROUTER_TOKEN"} & os.environ.keys()
    assert os.environ["BRAIN_ENABLED"] == "false"


def test_a_reload_reapplies_the_managed_guard(hook_env, monkeypatch):
    monkeypatch.setenv(brain_binding.VARIABLE, json.dumps(REFUSED))

    resolved = hook_env.reload_brain_env(force=True)

    assert (resolved["brain_url"], resolved["brain_enabled"], resolved["brain_source_type"]) == ("", False, "none")


def _brain_roles(world):
    profiles = world["bundle"] / "profiles"
    overlay = {"mcpServers": {"agentibrain": {"type": "http", "url": PERSONAL + "/mcp"}}}
    _write(profiles / "rb-brain" / "profile.yml", "name: rb-brain\n")
    _write(profiles / "rb-brain" / ".claude" / ".mcp.json", json.dumps(overlay))
    for role in ROLES:
        _write(profiles / role / "profile.yml", f"name: {role}\nextends: [rb-base, rb-brain]\n")


def _rendered(harness, role):
    from scripts.profiles import render

    render.render(harness, role)
    out = render.profile_dir(role) / harness
    if harness == "claude":
        return json.loads((out / ".claude.json").read_text())["mcpServers"]
    return tomllib.loads((out / "config.toml").read_text()).get("mcp_servers", {})


def _brains(servers):
    return {name: spec for name, spec in servers.items() if "brain" in name or "8104" in str(spec)}


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize("role", ROLES)
def test_every_role_mounts_one_managed_brain_and_restores_the_overlay_without_it(world, monkeypatch, role, harness):
    _brain_roles(world)
    monkeypatch.setenv("AGENTIHOOKS_MCP_URLS", json.dumps({"agentibrain": REPO_ENDPOINT + "/mcp"}))
    monkeypatch.setenv("SWARM_BRAIN_MCP_KEY", MCP_FIXTURE)
    _absent(monkeypatch, brain_binding.VARIABLE)

    personal = _brains(_rendered(harness, role))
    monkeypatch.setenv(brain_binding.VARIABLE, json.dumps(BOUND))
    managed = _brains(_rendered(harness, role))
    monkeypatch.setenv(brain_binding.VARIABLE, json.dumps(REFUSED))
    refused = _brains(_rendered(harness, role))
    monkeypatch.delenv(brain_binding.VARIABLE)
    restored = _brains(_rendered(harness, role))

    assert list(personal) == ["agentibrain"] and personal["agentibrain"]["url"] == REPO_ENDPOINT + "/mcp"
    assert restored == personal
    assert refused == {}
    assert list(managed) == ["agentibrain"] and managed["agentibrain"]["url"] == MCP
    if harness == "claude":
        assert managed["agentibrain"]["headers"] == {"X-API-Key": "${SWARM_BRAIN_MCP_KEY}"}
    else:
        assert managed["agentibrain"]["env_http_headers"] == {"X-API-Key": "SWARM_BRAIN_MCP_KEY"}
    assert MCP_FIXTURE not in json.dumps(managed)


@pytest.fixture
def launch(monkeypatch, tmp_path):
    from scripts import select_profile
    from scripts.profiles import binding

    monkeypatch.setattr(herdr_host, "binary", lambda: None)
    monkeypatch.setattr("scripts.profile_telemetry.installed_langfuse_env", lambda target: {})
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: (requested, "rotation"))
    monkeypatch.setattr(init_agent, "_handoff_exclude", lambda agent, environ: "")
    monkeypatch.setattr(binding, "continuation", lambda flags, agent, environ=None: flags)
    _absent(monkeypatch, brain_binding.VARIABLE)
    seen = []

    def prepare(name, agent, model, effort, flags, environ, overlays=()):
        seen.append(os.environ.get(brain_binding.VARIABLE))
        home = tmp_path / name / agent
        home.mkdir(parents=True, exist_ok=True)
        return {"AGENTIHOOKS_PROFILE": name, binding.HOMES[agent]: str(home)}, flags

    monkeypatch.setattr(select_profile, "prepare", prepare)
    _personal_files(tmp_path)

    def run(role, harness, mode, swarm=True, filed=CREDENTIALS, **extra):
        runtime = tmp_path / "runtime"
        _write(tmp_path / ".agentihooks" / "swarm-brain.env", "".join(f"{k}={v}\n" for k, v in filed.items()))
        environ = {
            "HOME": str(tmp_path),
            "AGENTIHOOKS_HOME": str(tmp_path / ".agentihooks"),
            "XDG_RUNTIME_DIR": str(runtime),
            "SHELL": "/bin/bash",
            "BRAIN_URL": PERSONAL,
            "BRAIN_HTTP_TOKEN": PERSONAL_FIXTURE,
            "AGENTIHOOKS_TARGET": harness,
            **extra,
        }
        if swarm:
            environ.update(AGENTIHOOKS_SWARM="rig", AGENTIHOOKS_SWARM_SPAWN="1", AGENTIHOOKS_AGENT_NAME=role)
        argv = ["--dir", str(tmp_path), "--name", role, "--agent", harness, "--profile", role, "--dry-run"]
        argv += {"launch": [], "resume": ["--resume", "conversation"], "handoff": ["--handoff", "--prompt", "doc"]}[
            mode
        ]
        assert init_agent.main(argv, environ) == 0
        launcher = max(runtime.glob("*/*.sh"), key=lambda p: p.stat().st_mtime_ns)
        return launcher.read_text(), seen[-1]

    return run


@pytest.mark.parametrize("mode", ["launch", "resume", "handoff"])
@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize("role", ROLES)
def test_every_managed_launch_and_recycle_carries_one_bound_brain(launch, tmp_path, capsys, role, harness, mode):
    _binding(tmp_path / ".agentihooks")

    text, rendered = launch(role, harness, mode)

    out = capsys.readouterr().out
    assert json.loads(rendered) == BOUND
    assert f"brain_binding={json.dumps(BOUND, sort_keys=True)}" in out.splitlines()
    assert f"export BRAIN_URL={API}\n" in text
    assert 'export BRAIN_HTTP_TOKEN="${SWARM_BRAIN_API_TOKEN}"\n' in text
    assert text.index("set -a;") < text.index("export BRAIN_URL=")
    for fixture in (API_FIXTURE, MCP_FIXTURE, PERSONAL_FIXTURE):
        assert fixture not in text and fixture not in out


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_managed_launch_without_a_token_refuses_the_brain_and_never_falls_back(launch, tmp_path, harness):
    _binding(tmp_path / ".agentihooks")

    filed = {"SWARM_BRAIN_MCP_KEY": MCP_FIXTURE}
    text, rendered = launch("engineer", harness, "launch", filed=filed, SWARM_BRAIN_API_TOKEN="")

    assert json.loads(rendered) == {"status": "refused", "reason": "credential SWARM_BRAIN_API_TOKEN is unset"}
    assert "unset BRAIN_URL BRAIN_HTTP_TOKEN\n" in text and "export BRAIN_URL=" not in text


def test_a_credential_only_in_the_launching_process_never_mounts_the_brain(launch, tmp_path):
    _binding(tmp_path / ".agentihooks")

    text, rendered = launch("engineer", "claude", "launch", filed={}, **CREDENTIALS)

    assert json.loads(rendered) == {
        "status": "refused",
        "reason": "credential SWARM_BRAIN_API_TOKEN, SWARM_BRAIN_MCP_KEY is unset",
    }
    assert "export BRAIN_URL=" not in text
    assert brain_binding.apply({"agentibrain": {"url": PERSONAL}}, _managed(json.loads(rendered))) == {}


def test_a_managed_launch_to_an_arbitrary_endpoint_refuses_the_brain(launch, tmp_path):
    _binding(tmp_path / ".agentihooks", _document(lan={"api": "http://repo-endpoint.example.test", "mcp": MCP}))

    text, rendered = launch("engineer", "claude", "launch")

    assert json.loads(rendered)["status"] == "refused"
    assert "export BRAIN_URL=" not in text and "export BRAIN_ENABLED=false" in text


def test_a_cluster_launch_binds_the_service_origins(launch, tmp_path):
    _binding(tmp_path / ".agentihooks")

    text, rendered = launch("engineer", "codex", "launch", KUBERNETES_SERVICE_HOST="10.0.0.1")

    assert json.loads(rendered) == {**BOUND, "site": "cluster", "api": CLUSTER_API, "mcp": CLUSTER_MCP}
    assert f"export BRAIN_URL={CLUSTER_API}\n" in text


def test_a_personal_handoff_drops_an_inherited_binding(launch, tmp_path):
    _binding(tmp_path / ".agentihooks")

    text, rendered = launch("engineer", "claude", "handoff", swarm=False, **_managed(BOUND))

    assert rendered is None
    assert brain_binding.VARIABLE not in text


def test_an_unrelated_personal_launch_keeps_its_installation(launch, tmp_path, monkeypatch, capsys):
    _binding(tmp_path / ".agentihooks")
    monkeypatch.setenv(brain_binding.VARIABLE, json.dumps(BOUND))

    text, rendered = launch("engineer", "claude", "launch", swarm=False, **_managed(BOUND))

    assert rendered is None
    assert brain_binding.VARIABLE not in text and "export BRAIN_URL=" not in text
    assert "brain_binding=" not in capsys.readouterr().out
