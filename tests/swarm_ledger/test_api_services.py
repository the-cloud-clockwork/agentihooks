import inspect
import json
import re
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts.swarm_ledger import api
from scripts.swarm_ledger.api import admin, architecture, hives, mutations, proofs, routes, routing
from scripts.swarm_ledger.api.services import HttpPolicy, LedgerService, MediaService, Services, SwarmService
from scripts.swarm_ledger.storage_migration import hosted

SLUG = "fake-2026-01-01"
ROOT = Path(__file__).resolve().parents[2]


def fake_services(calls: list) -> Services:
    ledger = SimpleNamespace(
        core=SimpleNamespace(SLUG_RE=re.compile(r"[a-z0-9-]+"), loads=json.loads),
        authority=SimpleNamespace(caller=lambda headers, admin, slug: "", controller=lambda credential: False),
        hub=None,
        home_summaries=lambda: [{"slug": SLUG, "title": "Fake"}],
        bin_summaries=lambda: [],
        bin_entries=lambda: {},
        bin_ledger=lambda slug: calls.append(("bin", slug)),
    )
    swarm = SimpleNamespace(
        doctor={"doctor_start": ["start"]},
        status=lambda slug: {"state": "running", "agents": [{"name": "eng"}]},
        control=lambda slug, argv, command="swarm": calls.append((command, slug, argv)) or ({}, ""),
        control_argv=lambda body: ["pause"],
    )
    state = {"tasks": [{"id": "t1"}, {"id": "t2"}], "_meta": {"rev": 1}}
    repository = SimpleNamespace(
        token=lambda slug: "tok",
        exists=lambda slug: slug == SLUG,
        current=lambda slug: SimpleNamespace(state=state, views={}),
        read=lambda slug, *keys: state,
        task_version=lambda slug: (state["_meta"]["rev"], 0),
    )
    return Services(
        repository=repository,
        ledger=ledger,
        swarm=swarm,
        media=SimpleNamespace(),
        policy=HttpPolicy(allowed_hosts={"h"}, allowed_origins={"o"}, max_body=1 << 10),
    )


class Headers(dict):
    def get_content_type(self):
        return self.get("Content-Type", "text/plain")


def request(
    command: str, path: str, payload: dict | None = None, origin: str | None = None, extra: dict | None = None
) -> SimpleNamespace:
    headers = Headers({"Host": "h", **({"Origin": origin} if origin else {}), **(extra or {})})
    raw = b""
    if payload is not None:
        raw = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(raw))
    sent = []
    return SimpleNamespace(
        command=command,
        path=path,
        headers=headers,
        rfile=BytesIO(raw),
        exists=lambda slug: slug == SLUG,
        send=lambda status, body, kind: sent.append((status, json.loads(body))),
        sent=sent,
    )


def served(services: Services, *args, **kwargs) -> tuple[int, dict]:
    handler = request(*args, **kwargs)
    api.handle(handler, services)
    return handler.sent[0]


def test_the_api_serves_ledger_reads_from_fake_services():
    status, body = served(fake_services([]), "GET", "/api/v1/ledgers")
    assert status == 200
    assert [(row["slug"], row["title"]) for row in body["data"]] == [(SLUG, "Fake")]


def test_the_api_serves_swarm_reads_from_the_swarm_service():
    status, body = served(fake_services([]), "GET", f"/api/v1/ledgers/{SLUG}/swarm/agents")
    assert status == 200
    assert [row["name"] for row in body["data"]] == ["eng"]


def test_a_ledger_collection_read_pages_by_the_request_query():
    status, body = served(fake_services([]), "GET", f"/api/v1/ledgers/{SLUG}/tasks?limit=1")
    assert status == 200
    assert [row["id"] for row in body["data"]] == ["t1"]


def test_a_swarm_collection_read_pages_by_the_request_query():
    services = fake_services([])
    services.swarm.status = lambda slug: {"state": "running", "agents": [{"name": "eng"}, {"name": "ci"}]}
    status, body = served(services, "GET", f"/api/v1/ledgers/{SLUG}/swarm/agents?limit=1")
    assert status == 200
    assert [row["name"] for row in body["data"]] == ["eng"]


def test_the_event_stream_serves_the_requested_ledger_from_the_ledger_hub(monkeypatch):
    from scripts.swarm_ledger.events import stream

    served_streams = []
    monkeypatch.setattr(stream, "serve", lambda handler, hub, slug, load: served_streams.append((hub, slug, load())))
    services = fake_services([])
    services.ledger.hub = object()
    services.ledger.stream_resources = lambda slug: {"ledger": slug}
    handler = request("GET", f"/api/v1/ledgers/{SLUG}/events", extra={"Accept": "text/event-stream"})
    api.handle(handler, services)
    assert handler.sent == []
    assert served_streams == [(services.ledger.hub, SLUG, {"ledger": SLUG})]


def test_the_api_runs_swarm_controls_through_the_swarm_service():
    calls = []
    status, body = served(fake_services(calls), "POST", f"/api/v1/ledgers/{SLUG}/swarm/actions", {"action": "pause"})
    assert (status, body) == (200, {"action": "pause", "accepted": True})
    assert calls == [("swarm", SLUG, ["pause"])]


def test_the_api_bins_through_the_ledger_service_and_stops_the_swarm():
    calls = []
    status, body = served(
        fake_services(calls), "POST", "/api/v1/bin/actions", {"action": "delete", "slug": SLUG}, origin="o"
    )
    assert (status, body) == (200, {"slug": SLUG, "action": "delete"})
    assert calls == [("bin", SLUG), ("swarm", SLUG, ["stop", "--now"])]


def test_the_api_refuses_hosts_outside_the_policy():
    handler = request("GET", "/api/v1/ledgers")
    handler.headers["Host"] = "elsewhere"
    api.handle(handler, fake_services([]))
    assert handler.sent[0][0] == 403


def test_an_empty_body_is_refused_before_it_is_decoded():
    services = fake_services([])
    services.ledger.core.loads = lambda raw: {}
    handler = request("POST", "/api/v1/layout", extra={"Content-Type": "application/json", "Content-Length": "0"})
    with pytest.raises(routes.APIError) as refused:
        routes.body(handler, services)
    assert (refused.value.status, refused.value.code) == (400, "schema_invalid")


def test_the_api_writes_the_layout_through_the_ledger_service_for_an_allowed_origin():
    calls = []
    services = fake_services(calls)
    services.ledger.layout_rows = {"main": ("left",)}
    services.ledger.layout_lists = ("columns",)
    services.ledger.write_layout = lambda sizes: calls.append(("layout", sizes)) or sizes
    sizes = {"main": {"left": 3}, "columns": {"tasks": [120]}}
    status, body = served(services, "PUT", "/api/v1/layout", sizes, origin="o")
    assert (status, body["data"]) == (200, sizes)
    assert calls == [("layout", sizes)]


def test_a_controller_credential_reads_a_ledger_without_a_page_credential():
    services = fake_services([])
    services.ledger.authority = SimpleNamespace(
        caller=lambda headers, admin, slug: None, controller=lambda credential: credential == "controller"
    )
    path = f"/api/v1/ledgers/{SLUG}/swarm/agents"
    assert served(services, "GET", path, extra={"X-Controller-Credential": "controller"})[0] == 200
    assert served(services, "GET", path)[0] == 403


def test_a_hive_credential_issues_an_agent_token():
    services = fake_services([])
    services.ledger.authority = SimpleNamespace(
        hive_member=lambda credential: "member" if credential == "hive" else None,
        hive_agent=lambda member, slug, agent: member == "member",
        hive_agent_token=lambda member, slug, agent: f"{member}-{agent}",
    )
    extra = {"X-Ledger-Agent": "eng", "X-Hive-Credential": "hive"}
    status, body = served(services, "POST", f"/api/v1/ledgers/{SLUG}/agent-token", extra=extra)
    assert (status, body) == (200, {"data": {"agent": "eng", "token": "member-eng"}})


def test_proof_ledgers_require_the_exact_controller_credential_header():
    services = fake_services([])
    services.ledger.authority = SimpleNamespace(controller=lambda credential: credential == "controller")
    assert (
        proofs.require_controller(request("POST", "/", extra={"X-Controller-Credential": "controller"}), services)
        is None
    )
    with pytest.raises(proofs.APIError):
        proofs.require_controller(request("POST", "/"), services)


def test_the_services_bundle_carries_interfaces_and_no_modules():
    services = fake_services([])
    assert not any(isinstance(getattr(services, field), ModuleType) for field in Services.__dataclass_fields__)
    assert {LedgerService, SwarmService, MediaService} <= set(Services.__annotations__.values())


@pytest.mark.parametrize(
    ("service", "field", "source"),
    [
        ("ledger", "core", "core"),
        ("ledger", "authority", "authority"),
        ("ledger", "hub", "HUB"),
        ("ledger", "layout_rows", "ledger_layout.ROWS"),
        ("ledger", "layout_lists", "ledger_layout.LISTS"),
        ("ledger", "home_summaries", "home_summaries"),
        ("ledger", "bin_summaries", "bin_summaries"),
        ("ledger", "bin_entries", "ledger_bin.entries"),
        ("ledger", "bin_ledger", "ledger_bin.delete"),
        ("ledger", "workspace_tails", "workspace_tails"),
        ("ledger", "stream_resources", "stream_resources"),
        ("ledger", "layout", "ledger_layout.read"),
        ("ledger", "write_layout", "ledger_layout.write"),
        ("ledger", "write_gate", "write_gate"),
        ("ledger", "relay_to_inbox", "relay_to_inbox"),
        ("ledger", "deliver_alerts", "deliver_alerts"),
        ("swarm", "doctor", "DOCTOR"),
        ("swarm", "store", "swarm_store"),
        ("swarm", "status", "swarm_status"),
        ("swarm", "models", "swarm_models"),
        ("swarm", "control", "swarm_control"),
        ("swarm", "lifecycle", "lifecycle_control"),
        ("swarm", "refresh_quota", "refresh_quota"),
        ("swarm", "control_argv", "control_argv"),
        ("swarm", "doctor_phrase", "doctor_phrase"),
        ("media", "media_store", "ledger_media"),
        ("media", "artifact_store", "ledger_artifacts"),
        ("media", "resolve_media", "ledger_media.resolve"),
        ("media", "resolve_artifacts", "ledger_artifacts.resolve"),
    ],
)
def test_the_composition_root_wires_each_service_to_its_server_source(service, field, source):
    from scripts.swarm_ledger import ledger_server

    expected = ledger_server
    for part in source.split("."):
        expected = getattr(expected, part)
    assert getattr(getattr(ledger_server.api_services(), service), field) is expected


def test_the_composition_root_injects_the_repository_and_the_http_policy():
    from scripts.swarm_ledger import ledger_server

    services = ledger_server.api_services()
    assert services.repository is ledger_server.repository
    assert services.policy == HttpPolicy(
        allowed_hosts=ledger_server.ALLOWED_HOSTS,
        allowed_origins=ledger_server.ALLOWED_ORIGINS,
        max_body=ledger_server.MAX_BODY,
    )


def test_no_api_function_takes_the_server_module():
    modules = (admin, architecture, hives, mutations, proofs, routes, routing, hosted)
    taking = [
        f"{module.__name__}.{name}.{parameter.name}"
        for module in modules
        for name, function in inspect.getmembers(module, inspect.isfunction)
        if function.__module__ == module.__name__
        for parameter in inspect.signature(function).parameters.values()
        if parameter.name == "server" or parameter.annotation is ModuleType
    ]
    assert taking == []
    services_parameters = [
        inspect.signature(function).parameters["services"].annotation
        for module in modules
        for _, function in inspect.getmembers(module, inspect.isfunction)
        if function.__module__ == module.__name__ and "services" in inspect.signature(function).parameters
    ]
    assert len(services_parameters) > 20
    assert set(services_parameters) == {Services}


def test_the_api_serves_fake_services_without_loading_a_server_module():
    probe = (
        "import sys\n"
        "from tests.swarm_ledger.test_api_services import fake_services, served\n"
        "assert served(fake_services([]), 'GET', '/api/v1/ledgers')[0] == 200\n"
        "loaded = sorted(name for name in sys.modules if name.endswith('ledger_server'))\n"
        "assert loaded == [], loaded\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
