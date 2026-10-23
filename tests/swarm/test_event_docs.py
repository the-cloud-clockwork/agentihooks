from pathlib import Path

from scripts.events import config as bus_config
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.dead import DEAD_STREAM
from scripts.events.memory import open_bus
from scripts.events.nats import EVENT_STREAM, QUEUE_SUBJECT
from scripts.events.subjects import PREFIX
from scripts.hive import presence
from scripts.hive.buckets import NAMES
from scripts.hub import registry
from scripts.swarm.store import EVENTS_ENGINE

DOCS = Path(__file__).resolve().parents[2] / "docs"
SWARM = DOCS / "pillars" / "swarm.md"
HIVES = DOCS / "pillars" / "hives.md"
MODES = DOCS / "reference" / "deployment-modes.md"


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


def test_swarm_page_names_the_engine_setting_and_its_values():
    text = _swarm()
    assert f"agentihooks swarm <slug> set engine={EVENTS_ENGINE}" in text
    assert "`legacy`" in text
    assert f"`{EVENTS_ENGINE}`" in text


def test_swarm_page_walks_a_task_through_offer_claim_and_exit():
    text = _swarm()
    for step in ("`task.ready`", "`execution.started`", "`execution.exited`", "`claim_generation`", "`dead_lettered`"):
        assert step in text, step


def test_swarm_page_names_the_bus_streams_and_subjects_from_code():
    text = _swarm()
    for name in (EVENT_STREAM, DEAD_STREAM, f"{QUEUE_SUBJECT}.<name>", f"{PREFIX}.<slug>.ready.<lane>"):
        assert f"`{name}`" in text, name


def test_swarm_page_states_the_queue_redelivery_defaults():
    text = _swarm()
    policy = QueuePolicy()
    assert f"{policy.max_deliver} deliveries" in text
    assert f"{policy.ack_wait_ms // 1000} seconds" in text
    assert f"{BusConfig().dedupe_window_ms // 1000} seconds" in text


def test_swarm_page_lists_what_the_tick_skips_for_event_swarms():
    text = _swarm()
    for skipped in ("spawns no", "reaps no", "orphan", "acknowledgement"):
        assert skipped in text, skipped


def test_hives_page_names_every_hive_bucket():
    text = _hives()
    for name in NAMES:
        assert f"`{name}`" in text, name


def test_hives_page_states_presence_timing_and_hive_loss():
    text = _hives()
    assert f"every {presence.BEAT_SECONDS} seconds" in text
    assert f"{presence.TTL_MS // 1000} seconds" in text
    assert "`hive-lost`" in text
    assert "agentihooks swarm spoke" in text


def test_modes_page_names_the_hub_command_and_every_reactor():
    text = _modes()
    assert "agentihooks hub run" in text
    for reactor in registry.configured(open_bus(BusConfig())):
        assert f"`{reactor.name}`" in text, reactor.name


def test_modes_page_names_the_bus_settings():
    text = _modes()
    for env in (bus_config.BACKEND_ENV, bus_config.URL_ENV, bus_config.CREDENTIALS_ENV, bus_config.BIND_ENV):
        assert f"`{env}`" in text, env
    assert "--bus-url" in text
    assert "`engine`" in text
