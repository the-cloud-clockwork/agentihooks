import json
from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck_status, cli, stages, status
from scripts.swarm.health import standing
from scripts.swarm.store import RedisStore
from tests.swarm.test_cli import env as env

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
NOW = 1_791_700_000_000


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def detect(store, *rows):
    ranked = [{"stage": stage, "label": "", "seconds": seconds, "trend": trend} for stage, seconds, trend in rows]
    found = {"at": NOW, "window_hours": 4, "stages": ranked, "total": sum(row["seconds"] for row in ranked)}
    store.redis.set(store.key("sw", stages.KEY), json.dumps(found))


def test_line_names_the_detectors_first_stage_and_trend_over_alert_ratios(store):
    reading = standing.Reading(88.6, 2, "task hours in the window", "quota")
    standing.fire(store.redis, "sw", "quota_hold", reading, NOW)
    standing.fire(store.redis, "sw", "claim_wait", standing.Reading(157, 4, "task hours in the window", "claim"), NOW)
    detect(store, ("claim_wait", 665404.0, "rising"), ("quota_hold", 216504.0, "falling"))
    found = bottleneck_status.report(store, "sw", NOW + 300_000)
    assert (found["stage"], found["trend"], found["seconds"], found["measured_at"]) == (
        "claim_wait",
        "rising",
        665404.0,
        NOW,
    )
    assert bottleneck_status.line(found) == (
        "Top bottleneck claim wait; trend rising; measured 5 minutes ago; last action none recorded"
    )


def test_trend_is_unknown_only_before_the_detector_runs(store):
    assert bottleneck_status.report(store, "sw", NOW) == {
        "stage": "",
        "label": "none",
        "trend": "unknown",
        "seconds": 0.0,
        "measured_at": 0,
        "measured": "never",
        "last_action": "none recorded",
        "action_at": 0,
    }
    detect(store, ("build", 0.0, "falling"))
    found = bottleneck_status.report(store, "sw", NOW)
    assert (found["stage"], found["label"], found["trend"]) == ("", "none", "none")


@pytest.mark.parametrize(
    "elapsed,measured",
    [(-120_000, "0 minutes ago"), (0, "0 minutes ago"), (59_999, "0 minutes ago"), (60_000, "1 minutes ago")],
)
def test_age_counts_whole_minutes_and_a_ranking_ahead_of_the_clock_reads_as_just_measured(store, elapsed, measured):
    detect(store, ("build", 3600.0, "rising"))
    assert bottleneck_status.report(store, "sw", NOW + elapsed)["measured"] == measured


def test_action_is_per_stage_and_survives_reading_updates(store):
    detect(store, ("ci_overrun", 3600.0, "rising"))
    bottleneck_status.record_action(store, "sw", "build", "Added a seat", NOW)
    assert bottleneck_status.report(store, "sw", NOW)["last_action"] == "none recorded"
    bottleneck_status.record_action(store, "sw", "ci_overrun", "Proposed a faster mutation run", NOW)
    detect(store, ("ci_overrun", 7200.0, "rising"))
    report = bottleneck_status.report(store, "sw", NOW)
    assert report["last_action"] == "Proposed a faster mutation run"
    assert report["action_at"] == NOW


def test_each_top_transition_sends_one_master_item_and_repeats_send_none(store):
    assert bottleneck_status.refresh("sw", store, NOW) == []
    detect(store, ("ci_overrun", 3600.0, "rising"))
    bottleneck_status.refresh("sw", store, NOW)
    bottleneck_status.refresh("sw", store, NOW)
    detect(store, ("build", 7200.0, "rising"), ("ci_overrun", 3600.0, "falling"))
    bottleneck_status.refresh("sw", store, NOW)
    bottleneck_status.refresh("sw", store, NOW)
    detect(store, ("ci_overrun", 3600.0, "unchanged"))
    bottleneck_status.refresh("sw", store, NOW)
    detect(store)
    bottleneck_status.refresh("sw", store, NOW)
    bottleneck_status.refresh("sw", store, NOW)
    items = InboxStore(store.redis).pending_mail("master@sw")
    assert [item.text for item in items] == [
        "Top bottleneck ci overrun; trend rising; measured 0 minutes ago; last action none recorded",
        "Top bottleneck build; trend rising; measured 0 minutes ago; last action none recorded",
        "Top bottleneck ci overrun; trend unchanged; measured 0 minutes ago; last action none recorded",
        "Top bottleneck none; trend none; measured 0 minutes ago; last action none recorded",
    ]
    assert len({item.ref for item in items}) == 4
    assert {item.sender for item in items} == {"swarm"}


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_notification_commit_retries_without_duplicates(store, monkeypatch, committed):
    from redis.exceptions import ConnectionError

    detect(store, ("ci_overrun", 3600.0, "rising"))
    pipeline_type = type(store.redis.pipeline())
    execute = pipeline_type.execute

    def interrupted(pipe, *args, **kwargs):
        if committed:
            execute(pipe, *args, **kwargs)
        raise ConnectionError("interrupted transaction")

    with monkeypatch.context() as patch:
        patch.setattr(pipeline_type, "execute", interrupted)
        with pytest.raises(ConnectionError):
            bottleneck_status.refresh("sw", store, NOW)
    bottleneck_status.refresh("sw", store, NOW)
    assert len(InboxStore(store.redis).pending_mail("master@sw")) == 1
    assert bottleneck_status.read(store, "sw")["transition"] == 1


def test_status_json_and_text_share_the_detectors_stage_trend_and_action(env, monkeypatch, capsys):
    store, _, _ = env
    cli.main(["sw", "create", "--repo", "/repo"])
    capsys.readouterr()
    monkeypatch.setattr(status, "now_ms", lambda: NOW)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    detect(store, ("claim_wait", 7200.0, "rising"), ("quota_hold", 3600.0, "rising"))
    bottleneck_status.record_action(store, "sw", "claim_wait", "Added a seat", NOW)
    report = status.status_report(store, "sw", {"tasks": []})["top_bottleneck"]
    assert (report["stage"], report["trend"], report["last_action"]) == ("claim_wait", "rising", "Added a seat")
    cli.cmd_status(store, SimpleNamespace(slug="sw", json=False))
    out = capsys.readouterr().out
    assert "top stages in the last 4 hours: claim wait 2.0 task hours (rising)" in out
    assert "Top bottleneck claim wait; trend rising; measured 0 minutes ago; last action Added a seat" in out


def test_tick_delivers_new_metric_stage_once_before_status_publication(env, monkeypatch):
    from tests.inbox.test_wake import FakeHerdr

    store, ledger, runtime = env
    cli.main(["sw", "create", "--repo", "/repo"])
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    samples = iter(["ci_overrun", "build", "build"])

    def collect(*_):
        detect(store, (next(samples), 3600.0, "rising"))
        return []

    monkeypatch.setattr(cli.metrics, "record_pass", collect)
    for _ in range(3):
        cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    items = [
        item for item in InboxStore(store.redis).pending_mail("master@sw") if item.ref.startswith("top-bottleneck:")
    ]
    assert len(items) == 2
    assert {item.text.split(";", 1)[0] for item in items} == {"Top bottleneck ci overrun", "Top bottleneck build"}
    assert bottleneck_status.read(store, "sw")["stage"] == "build"
