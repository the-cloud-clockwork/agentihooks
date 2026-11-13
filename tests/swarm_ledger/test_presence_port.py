import scripts.swarm_ledger.adapters.presence
import scripts.swarm_ledger.composition
import scripts.swarm_ledger.ports
from scripts.hive import otel_presence


def test_the_presence_adapter_reads_the_hive_presence_source(monkeypatch):
    seen = []
    hives = [{"id": "h"}]

    def read(environ, now_s):
        seen.append((environ, now_s))
        return hives

    monkeypatch.setattr(otel_presence, "read", read)
    env = {"AGENTIHOOKS_PRESENCE_URL": "http://c/metrics"}
    assert scripts.swarm_ledger.adapters.presence.read(env, 12.5) is hives
    assert seen == [(env, 12.5)]


def test_composition_binds_the_presence_adapter():
    adapters = scripts.swarm_ledger.composition.adapters()
    assert adapters["presence"] is scripts.swarm_ledger.adapters.presence


def test_the_presence_port_returns_its_bound_adapter(monkeypatch):
    bound = object()
    monkeypatch.setitem(scripts.swarm_ledger.ports.BOUND, "presence", bound)
    assert scripts.swarm_ledger.ports.presence() is bound
