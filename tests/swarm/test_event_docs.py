import re
from pathlib import Path

import yaml

from scripts.events import config as bus_config
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.dead import DEAD_STREAM
from scripts.events.memory import open_bus
from scripts.events.nats import EVENT_STREAM, QUEUE_SUBJECT
from scripts.events.subjects import PREFIX
from scripts.hive import presence
from scripts.hive.buckets import NAMES
from scripts.hub import hive_loss, lease, registry
from scripts.swarm.store import EVENTS_ENGINE

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SWARM = DOCS / "pillars" / "swarm.md"
HIVES = DOCS / "pillars" / "hives.md"
MODES = DOCS / "reference" / "deployment-modes.md"
VALUES = ROOT / "deploy" / "helm" / "agentihooks-swarm" / "values.yaml"
LEDGER_SUBJECTS = {f"{PREFIX}.<slug>.task.<id>", f"{PREFIX}.<slug>.phase.<id>", f"{PREFIX}.<slug>.ready.<lane>"}


def _section(page: Path, heading: str) -> str:
    text = page.read_text()
    assert f"\n## {heading}\n" in text, heading
    return text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]


def _swarm():
    return _section(SWARM, "Event driven engine")


def _hives():
    return _section(HIVES, "Pulling work from the hub")


def _modes():
    return _section(MODES, "The hub and the bus")


def _reactors():
    return list(registry.configured(open_bus(BusConfig())))


def _row(text: str, first: str) -> list[str]:
    row = re.search(rf"^\| `{first}` \|(.*)\|$", text, re.M)
    assert row, first
    return row.group(1).split(" | ")


def test_swarm_page_names_the_engine_setting_and_its_values():
    text = _swarm()
    assert f"agentihooks swarm <slug> set engine={EVENTS_ENGINE}" in text
    assert "`legacy`" in text
    assert f"`{EVENTS_ENGINE}`" in text


def test_swarm_page_walks_a_task_through_offer_claim_and_exit():
    text = _swarm()
    for step in ("Ledger write", "Offer", "Pull", "Claim and launch", "Lease", "Redelivery", "Exit", "Reopen"):
        assert f"**{step}.**" in text, step
    for event in ("`task.ready`", "`execution.started`", "`execution.exited`", "`claim_generation`", "`dead_lettered`"):
        assert event in text, event


def test_swarm_page_names_the_bus_streams_and_subjects_from_code():
    text = _swarm()
    for name in (EVENT_STREAM, DEAD_STREAM, f"{QUEUE_SUBJECT}.<name>", *LEDGER_SUBJECTS):
        assert f"`{name}`" in text, name


def test_swarm_page_states_the_queue_redelivery_defaults():
    text = _swarm()
    policy = QueuePolicy()
    assert f"{policy.max_deliver} deliveries" in text
    assert f"{policy.ack_wait_ms // 1000} seconds" in text
    assert f"{BusConfig().dedupe_window_ms // 1000} seconds" in text


def test_swarm_page_lists_what_the_tick_skips_for_event_swarms():
    text = " ".join(_swarm().split())
    for skipped in (
        "spawns no lane agent",
        "reaps no lane agent",
        "runs no orphan pass",
        "collects no acknowledgement",
    ):
        assert skipped in text, skipped


def test_every_subject_the_pages_name_comes_from_code():
    known = LEDGER_SUBJECTS | {reactor.subject for reactor in _reactors()}
    named = set()
    for text in (_swarm(), _hives(), _modes()):
        named |= set(re.findall(rf"`({PREFIX}\.[^`\s]+)`", text))
    assert named - known == set()


def test_hives_page_names_every_hive_bucket():
    text = _hives()
    for name in NAMES:
        assert f"`{name}`" in text, name


def test_hives_page_states_presence_timing_and_hive_loss():
    text = _hives()
    assert f"every {presence.BEAT_SECONDS} seconds" in text
    assert f"{presence.TTL_MS // 1000} seconds" in text
    assert f"`{hive_loss.REASON}`" in text
    assert "agentihooks swarm spoke" in text


def test_modes_page_names_each_reactor_with_its_subject():
    text = _modes()
    assert "agentihooks hub run" in text
    for reactor in _reactors():
        assert f"`{reactor.subject}`" in _row(text, reactor.name)[0], reactor.name


def test_modes_page_names_the_hub_lease_and_durables():
    text = _modes()
    assert f"`{lease.BUCKET}`" in text
    assert "`hub-<name>`" in text
    for reactor in _reactors():
        assert registry.durable(reactor) == f"hub-{reactor.name}"


def test_modes_page_maps_each_mode_to_its_engine():
    text = _modes()
    for mode in ("local", "compose", "distributed"):
        engine = _row(text, mode)[0]
        assert "`legacy`" in engine, mode
        assert f"`{EVENTS_ENGINE}`" in engine, mode


def test_modes_page_states_the_chart_hub_on_anton():
    text = _modes()
    hub = yaml.safe_load(VALUES.read_text())["hub"]
    terms = hub["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert f"{hub['replicas']} replicas" in text
    assert f"`{terms[0]['matchExpressions'][0]['key']}`" in text
    assert "`<release>-hub`" in text


def test_modes_page_names_the_bus_settings():
    text = _modes()
    for env in (bus_config.BACKEND_ENV, bus_config.URL_ENV, bus_config.CREDENTIALS_ENV, bus_config.BIND_ENV):
        assert f"`{env}`" in text, env
    assert "--bus-url" in text
    assert "`engine`" in text
