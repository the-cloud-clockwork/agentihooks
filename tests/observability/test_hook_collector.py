import json
import urllib.request

import pytest

from hooks import telemetry
from hooks.config import hook_collector
from hooks.observability import otel
from scripts import install
from tests.test_install import TestInstallGlobalHonoursTarget as InstallHelper

COLLECTOR = "http://collector:4318"


@pytest.fixture
def hook_env(monkeypatch):
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "CLAUDE_CODE_ENABLE_TELEMETRY",
        "AGENTIHOOKS_OTLP_ENDPOINT",
        "AGENTIHOOKS_OTLP_PROTOCOL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTIHOOKS_OTLP_ENDPOINT", COLLECTOR)
    monkeypatch.setenv("AGENTIHOOKS_OTLP_PROTOCOL", "http/protobuf")
    return monkeypatch


@pytest.fixture
def posted(monkeypatch):
    urls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: urls.append(request.full_url))
    return urls


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"AGENTIHOOKS_OTLP_ENDPOINT": COLLECTOR, "AGENTIHOOKS_OTLP_PROTOCOL": "grpc"}, (COLLECTOR, "grpc")),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": COLLECTOR, "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, (COLLECTOR, "grpc")),
        ({"AGENTIHOOKS_OTLP_ENDPOINT": COLLECTOR, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://other:4317"}, (COLLECTOR, "")),
        ({}, ("", "")),
    ],
)
def test_hook_collector_prefers_the_unprefixed_setting(environ, expected):
    assert hook_collector(environ) == expected


def test_brain_log_reaches_the_unprefixed_collector(hook_env, posted):
    telemetry.emit_log("brain.delivery", {"session_id": "s"})
    assert posted == [f"{COLLECTOR}/v1/logs"]


def test_brain_span_fallback_reaches_the_unprefixed_collector(hook_env, posted):
    telemetry._http_fallback("brain.inject", {}, 1.0)
    assert posted == [f"{COLLECTOR}/v1/traces"]


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://collector:4317", "http://collector:4318/v1/traces"),
        ("http://collector:4317/", "http://collector:4318/v1/traces"),
        ("http://127.0.0.1:43175", "http://127.0.0.1:43175/v1/traces"),
        ("http://127.0.0.1:43170/", "http://127.0.0.1:43170/v1/traces"),
    ],
)
def test_brain_span_fallback_swaps_only_the_grpc_port(hook_env, posted, endpoint, expected):
    hook_env.setenv("AGENTIHOOKS_OTLP_ENDPOINT", endpoint)
    telemetry._http_fallback("brain.inject", {}, 1.0)
    assert posted == [expected]


def test_hook_events_init_with_only_the_unprefixed_collector(hook_env):
    assert otel._can_init()


@pytest.mark.parametrize(
    ("protocol", "expected"),
    [
        ("http/protobuf", {signal: f"{COLLECTOR}/v1/{signal}" for signal in ("traces", "metrics", "logs")}),
        ("grpc", {signal: COLLECTOR for signal in ("traces", "metrics", "logs")}),
    ],
)
def test_hook_exporters_target_the_unprefixed_collector(hook_env, protocol, expected):
    hook_env.setenv("AGENTIHOOKS_OTLP_PROTOCOL", protocol)
    assert otel._collector_endpoints() == (protocol, expected)


def test_installed_settings_carry_the_unprefixed_collector(tmp_path, monkeypatch):
    helper = InstallHelper()
    original = helper._tiny_profile

    def profiles(root):
        directory = original(root)
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": COLLECTOR, "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf"}
        (directory / "tiny" / ".claude" / "settings.overrides.json").write_text(json.dumps({"env": env}))
        return directory

    monkeypatch.setattr(helper, "_tiny_profile", profiles)
    monkeypatch.setattr(install, "_seed_user_env_file", lambda: None)
    home = helper._run(tmp_path, monkeypatch, "claude")
    env = json.loads((home / ".claude" / "settings.json").read_text())["env"]
    assert env["AGENTIHOOKS_OTLP_ENDPOINT"] == COLLECTOR
    assert env["AGENTIHOOKS_OTLP_PROTOCOL"] == "http/protobuf"


def test_hook_event_reaches_the_log_emitter(monkeypatch):
    records = []
    monkeypatch.setattr(
        otel, "_log_emitter", type("Emitter", (), {"emit": lambda self, record: records.append(record)})()
    )
    otel._dispatch_op(("event", "agentihooks.probe", {"probe": "p"}))
    assert [(record.body, record.attributes["probe"]) for record in records] == [("agentihooks.probe", "p")]
    assert records[0].timestamp > 0
