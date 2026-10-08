import importlib.util

from scripts.swarm import keyspace
from tests import swarm_v2_isolation


def _fresh():
    spec = importlib.util.spec_from_file_location("keyspace_fresh", keyspace.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_suite_roots_every_swarm_key_at_its_run_prefix():
    assert keyspace.ROOT == swarm_v2_isolation.RUN_PREFIX


def test_the_setting_names_the_root_and_production_is_the_default(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM_KEY_PREFIX", "agentihooks-proof")
    assert _fresh().ROOT == "agentihooks-proof"
    monkeypatch.setenv("AGENTIHOOKS_SWARM_KEY_PREFIX", "")
    assert _fresh().ROOT == "agentihooks"
    monkeypatch.delenv("AGENTIHOOKS_SWARM_KEY_PREFIX")
    assert _fresh().ROOT == "agentihooks"
