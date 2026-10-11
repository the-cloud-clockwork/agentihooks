from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck_status, cli, status
from scripts.swarm.health import standing
from scripts.swarm.store import RedisStore
from tests.swarm.test_cli import env as env

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
NOW = 1_791_700_000_000


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def fire(store, stage="ci_overrun", value=3, threshold=1, at=NOW, unit="minutes"):
    standing.fire(store.redis, "sw", stage, standing.Reading(value, threshold, unit, str(at)), at)


def test_top_compares_threshold_ratios_and_breaks_ties_by_stage(store):
    fire(store, "build", 100, 50, unit="task hours")
    fire(store)
    assert bottleneck_status.report(store, "sw", NOW)["stage"] == "ci_overrun"
    fire(store, "claim_wait", 6, 2)
    assert bottleneck_status.report(store, "sw", NOW)["stage"] == "ci_overrun"


def test_empty_expired_and_nonpositive_thresholds_show_no_top(store):
    assert bottleneck_status.report(store, "sw", NOW)["stage"] == ""
    fire(store, threshold=0)
    assert bottleneck_status.report(store, "sw", NOW)["stage"] == ""
    fire(store, at=NOW - standing.STALE_MS)
    assert bottleneck_status.report(store, "sw", NOW)["stage"] == ""


@pytest.mark.parametrize("value,trend", [(4, "rising"), (2, "falling"), (3, "steady")])
def test_trend_compares_observations_and_survives_identical_ticks(store, value, trend):
    fire(store)
    bottleneck_status.refresh("sw", store, NOW)
    assert bottleneck_status.report(store, "sw", NOW)["trend"] == "unknown"
    fire(store, value=value, at=NOW + 1)
    bottleneck_status.refresh("sw", store, NOW + 1)
    bottleneck_status.refresh("sw", store, NOW + 2)
    assert bottleneck_status.report(store, "sw", NOW + 2)["trend"] == trend


def test_changed_units_do_not_compare_unrelated_values(store):
    fire(store)
    bottleneck_status.refresh("sw", store, NOW)
    fire(store, at=NOW + 1, unit="task hours")
    assert bottleneck_status.report(store, "sw", NOW + 1)["trend"] == "unknown"


def test_action_is_per_stage_and_survives_reading_updates(store):
    fire(store)
    bottleneck_status.record_action(store, "sw", "build", "Added a seat", NOW)
    assert bottleneck_status.report(store, "sw", NOW)["last_action"] == "none recorded"
    bottleneck_status.record_action(store, "sw", "ci_overrun", "Proposed a faster mutation run", NOW)
    fire(store, value=4, at=NOW + 1)
    report = bottleneck_status.report(store, "sw", NOW + 1)
    assert report["last_action"] == "Proposed a faster mutation run"
    assert report["action_at"] == NOW


def test_each_top_transition_sends_one_master_item_and_repeats_send_none(store):
    assert bottleneck_status.refresh("sw", store, NOW) == []
    fire(store)
    bottleneck_status.refresh("sw", store, NOW)
    bottleneck_status.refresh("sw", store, NOW + 1)
    fire(store, "build", 8)
    bottleneck_status.refresh("sw", store, NOW + 2)
    bottleneck_status.refresh("sw", store, NOW + 3)
    standing.clear(store.redis, "sw", "build")
    bottleneck_status.refresh("sw", store, NOW + 4)
    standing.clear(store.redis, "sw", "ci_overrun")
    bottleneck_status.refresh("sw", store, NOW + 5)
    bottleneck_status.refresh("sw", store, NOW + 6)
    items = InboxStore(store.redis).pending_mail("master@sw")
    assert len(items) == 4
    assert [item.text.split(";", 1)[0] for item in items] == [
        "Top bottleneck ci overrun",
        "Top bottleneck build",
        "Top bottleneck ci overrun",
        "Top bottleneck none",
    ]
    assert len({item.ref for item in items}) == 4


def test_status_json_and_text_share_stage_trend_and_action(env, monkeypatch, capsys):
    store, _, _ = env
    cli.main(["sw", "create"])
    capsys.readouterr()
    monkeypatch.setattr(status, "now_ms", lambda: NOW)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    fire(store)
    bottleneck_status.record_action(store, "sw", "ci_overrun", "Proposed a faster mutation run", NOW)
    report = status.status_report(store, "sw", {"tasks": []})["top_bottleneck"]
    assert report["stage"] == "ci_overrun"
    assert report["trend"] == "unknown"
    assert report["last_action"] == "Proposed a faster mutation run"
    cli.cmd_status(store, SimpleNamespace(slug="sw", json=False))
    assert (
        "Top bottleneck ci overrun; trend unknown; last action Proposed a faster mutation run"
        in capsys.readouterr().out
    )
