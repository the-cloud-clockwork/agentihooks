import io
import json

import pytest

from scripts.swarm_v2 import broadcast_bridge, broadcasts, worker_home
from tests.test_swarm_v2_launch import API, FIRST, World
from tests.test_worker_home import fixture, inprocess, request  # noqa: F401

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


def test_a_home_bootstrapped_from_a_distributed_launch_claims_broadcasts_from_its_swarm_api(fixture, monkeypatch):  # noqa: F811
    templates, volume = fixture
    world = World(monkeypatch)
    launch = world.launch(FIRST)
    [spawned] = world.runtime.requests
    worker_home.bootstrap(request(templates, volume, endpoints=spawned.task["endpoints"]))
    attempt = volume / "attempt-1"
    settings = json.loads((attempt / "homes/claude/.claude/settings.json").read_text())
    wrapper = (attempt / "homes/codex/.codex/agentihooks-hook.sh").read_text()
    assert settings["env"]["AGENTIHOOKS_SWARM_API_URL"] == API
    assert f'export AGENTIHOOKS_SWARM_API_URL="${{AGENTIHOOKS_SWARM_API_URL:={API}}}"' in wrapper
    worker_home.store_grant(attempt, launch.grant)
    sent, cached = [], []

    def urlopen(claim, timeout):
        sent.append(claim)
        return io.BytesIO(b'{"deliveries": []}')

    def cache(entries):
        cached.append(entries)
        return 0

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("hooks.context.broadcast.cache_fleet_broadcasts", cache)

    assert broadcast_bridge.claim("s-remote", ["brain"], {**settings["env"], broadcasts.FLAG: "1"}) == 0

    assert cached == [[]]
    [claim] = sent
    assert claim.full_url == f"{API}/v2/broadcasts/claim"
    assert claim.get_header("Authorization") == f"Bearer {launch.grant}"
