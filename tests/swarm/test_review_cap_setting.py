"""Swarm set opens the review lane: it stores the review cap that the review pass tick step reads."""

import json
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.events.subjects import subject
from scripts.swarm.store import RedisStore
from tests.swarm import test_cli

env = test_cli.env
pytestmark = pytest.mark.unit

SLUG = "sw"
PULL = "https://github.com/acme/widgets/pull/7"
NOW = 1_700_000_000_000


@pytest.fixture
def created(env: tuple[RedisStore, Any, Any], monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    store, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    test_cli.run(SLUG, "create", "--repo", "/repo")
    store.redis.hdel(store.key(SLUG, "config"), "max_review")
    return store


@pytest.mark.feature("swarm-review-cap-setting")
def test_set_stores_the_review_cap_on_a_swarm_stored_without_one(
    created: RedisStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert created.config(SLUG).max_review == 0
    capsys.readouterr()
    assert test_cli.run(SLUG, "set", "max-review-agents=2") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["max_review"] == 2
    assert created.config(SLUG).max_review == 2
    assert test_cli.run(SLUG, "max-review-agents=0") == 0
    assert created.config(SLUG).max_review == 0
    assert created.config(SLUG).caps_pinned is False


@pytest.mark.feature("swarm-review-cap-setting")
@pytest.mark.parametrize("value", ["x", "-1", "1.5"])
def test_set_refuses_a_review_cap_that_is_not_a_whole_number(created: RedisStore, value: str) -> None:
    assert test_cli.run(SLUG, "set", f"max-review-agents={value}") == 1
    assert created.config(SLUG).max_review == 0


@pytest.mark.feature("swarm-review-cap-setting")
def test_status_shows_the_worker_caps(created: RedisStore, capsys: pytest.CaptureFixture[str]) -> None:
    config = created.config(SLUG)
    capsys.readouterr()
    test_cli.run(SLUG, "status")
    assert f"  eng {config.max_eng}  ci {config.max_ci}  plan {config.max_plan}  " in capsys.readouterr().out


@pytest.mark.feature("swarm-review-cap-setting")
def test_status_shows_the_review_cap(created: RedisStore, capsys: pytest.CaptureFixture[str]) -> None:
    assert test_cli.run(SLUG, "set", "max-review-agents=3") == 0
    capsys.readouterr()
    test_cli.run(SLUG, "status")
    assert f"  plan {created.config(SLUG).max_plan}  review 3  masters step " in capsys.readouterr().out


@pytest.mark.feature("swarm-review-cap-setting")
def test_a_review_cap_set_on_a_running_swarm_lets_the_review_pass_queue(
    created: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, bus, added = import_module("scripts.tick.steps.review"), open_bus(BusConfig()), []
    event = Envelope(f"pull.opened-{PULL}-{NOW}", "pull.opened", SLUG, "pull", PULL, 1, NOW, "github", {"url": PULL})
    bus.events.publish(subject(SLUG, "pull", "opened"), event)
    monkeypatch.setattr(module, "_bus", lambda: bus)
    created.update(SLUG, state="running")
    ledger = SimpleNamespace(add_task=lambda slug, task, by: added.append((slug, task, by)))
    values = {"store": created, "slug": SLUG, "doc": {"tasks": [], "phases": []}, "ledger": ledger}
    tick = SimpleNamespace(now_ms=lambda: NOW)
    assert module._review_pass(tick, values.__getitem__) == []
    assert test_cli.run(SLUG, "set", "max-review-agents=1") == 0
    assert len(module._review_pass(tick, values.__getitem__)) == 1
    assert [(slug, task["lane"]) for slug, task, _ in added] == [(SLUG, "review")]
