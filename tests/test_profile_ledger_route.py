import json
from types import SimpleNamespace

import pytest

from scripts.profiles import render
from scripts.swarm.store import AgentRecord
from scripts.swarm_ledger import hosted_probe, ledger
from tests.swarm_ledger.test_hive_agent_tokens import hive as _hive
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER
from tests.swarm_ledger.test_ledger_authority import live as _live
from tests.test_profile_render import _write
from tests.test_profile_render import world as _world

pytestmark = pytest.mark.unit
world = _world
live = _live
hive = _hive


def bundle_route(world, url):
    _write(world["bundle"] / ".claude" / "settings.overrides.json", json.dumps({"env": {hosted_probe.HOSTED_ENV: url}}))


@pytest.mark.parametrize("configured", ["", "https://internal.example:8765"])
def test_mounted_profile_retains_the_configured_hosted_route(world, monkeypatch, configured):
    bundle_route(world, "https://public.example")
    monkeypatch.setenv(hosted_probe.HOSTED_ENV, configured)

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["env"][hosted_probe.HOSTED_ENV] == (
        configured or "https://public.example"
    )


def test_hosted_route_change_invalidates_the_mounted_profile_cache(world, monkeypatch):
    bundle_route(world, "https://public.example")
    monkeypatch.setenv(hosted_probe.HOSTED_ENV, "https://first.example")
    first = render.render_claude("rb-role")
    assert render.render_claude("rb-role") is None
    monkeypatch.setenv(hosted_probe.HOSTED_ENV, "https://second.example")

    second = render.render_claude("rb-role")

    assert second is not None
    assert second != first
    assert (
        json.loads((second / "settings.json").read_text())["env"][hosted_probe.HOSTED_ENV] == "https://second.example"
    )


def test_rendered_hive_worker_joins_and_writes_through_its_configured_route(world, monkeypatch, live, hive, capsys):
    bundle_route(world, "http://127.0.0.1:1")
    monkeypatch.setenv(hosted_probe.HOSTED_ENV, f"http://{live['host']}")
    out = render.render_claude("rb-role")
    store, grant = hive
    store.start_execution(SLUG, AgentRecord(WORKER, "eng", "proof", seat=f"eng-1@{SLUG}", hive=grant["id"]))
    monkeypatch.setattr(ledger, "BASE", "")
    monkeypatch.delenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", WORKER)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", grant["ledger_credential"])
    monkeypatch.setenv("SWARM_URL", "http://127.0.0.1:1")
    for key, value in json.loads((out / "settings.json").read_text())["env"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(hosted_probe, "_answers", {})

    ledger.cmd_join(SimpleNamespace(slug=SLUG, name=WORKER, role="member"))
    ledger.cmd_ack(SimpleNamespace(slug=SLUG, name=WORKER))

    assert ledger.call(SLUG)["_meta"]["members"][WORKER]["role"] == "member"
    assert "joined" in capsys.readouterr().out
