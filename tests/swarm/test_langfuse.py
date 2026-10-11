import json
from pathlib import Path
from types import SimpleNamespace


def test_recorded_turn_sample_yields_one_standing_finding_per_breaching_account():
    import fakeredis

    from scripts.swarm import langfuse
    from scripts.swarm.health import standing
    from scripts.swarm.langfuse_config import Limits

    sample = json.loads((Path(__file__).parents[1] / "fixtures/swarm/langfuse-summary.json").read_text())
    bindings = [SimpleNamespace(**row) for row in sample["bindings"]]
    redis = fakeredis.FakeRedis(decode_responses=True)
    limits = Limits()
    langfuse.apply(redis, "rig-grade-swarm", sample["summary"], bindings, 2000000, limits)
    rows = standing.read(redis, "rig-grade-swarm")
    assert set(rows) == {
        "langfuse_account_nestorcoltathotmail_com",
        "langfuse_account_nestor_coltatthecloudclockwork_com",
    }
    assert "engineer@323133-1125" in rows["langfuse_account_nestorcoltathotmail_com"]["detail"]
    assert "ncx5" in rows["langfuse_account_nestorcoltathotmail_com"]["detail"]
    assert "719854" in rows["langfuse_account_nestorcoltathotmail_com"]["detail"]
    assert "engineer@323133-1138" in rows["langfuse_account_nestor_coltatthecloudclockwork_com"]["detail"]
    assert "t439" in rows["langfuse_account_nestor_coltatthecloudclockwork_com"]["detail"]
    assert "600000" in rows["langfuse_account_nestorcoltathotmail_com"]["detail"]
    assert len(standing.findings(redis, "rig-grade-swarm", 2000000)) == 2


def test_sampling_uses_the_configured_window_and_interval():
    import fakeredis

    from scripts.swarm import langfuse
    from scripts.swarm.langfuse_config import Limits

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = SimpleNamespace(redis=redis, execution_registry=SimpleNamespace(records=lambda slug: []))
    calls = []

    def read(slug, window, timeout):
        calls.append((slug, window, timeout))
        return {"swarm": slug, "window": window, "agents": []}

    limits = Limits()
    langfuse.refresh(store, "swarm", 3600000, read=read)
    langfuse.refresh(store, "swarm", 3600000 + limits.interval_ms - 1, read=read)
    assert calls == [("swarm", {"fromTimestamp": "1970-01-01T00:00:00Z", "toTimestamp": "1970-01-01T01:00:00Z"}, 30)]
    langfuse.refresh(store, "swarm", 3600000 + limits.interval_ms, read=read)
    assert len(calls) == 2
    assert calls[1][1]["toTimestamp"] == "1970-01-01T01:10:00Z"


def recorded_case():
    import fakeredis

    from scripts.swarm.langfuse_config import Limits

    sample = json.loads((Path(__file__).parents[1] / "fixtures/swarm/langfuse-summary.json").read_text())
    return (
        fakeredis.FakeRedis(decode_responses=True),
        sample["summary"],
        [SimpleNamespace(**row) for row in sample["bindings"]],
        Limits(),
    )


def test_clean_recorded_sample_clears_only_langfuse_findings():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    clean = {**summary, "agents": [summary["agents"][1]]}
    assert clean["agents"][0]["agent"] == "engineer@323133-1135"
    assert langfuse.apply(redis, "rig-grade-swarm", clean, bindings, 2000000, limits) == 0
    assert standing.read(redis, "rig-grade-swarm") == {}
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2000000, limits)
    standing.fire(redis, "rig-grade-swarm", "ci_budget", standing.Reading(4, 3, "minutes", "ci"), 2000000)
    langfuse.apply(redis, "rig-grade-swarm", clean, bindings, 2000001, limits)
    assert set(standing.read(redis, "rig-grade-swarm")) == {"ci_budget"}


def test_error_and_latency_breaches_share_one_account_finding_and_repeat_updates():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    bindings[1].account = bindings[0].account
    summary["agents"][1]["error_rate"] = 0.4
    summary["agents"][1]["errors"] = 40
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2000000, limits)
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2000001, limits)
    row = standing.read(redis, "rig-grade-swarm")["langfuse_account_nestorcoltathotmail_com"]
    assert row["fired"] == 1
    assert row["updated_at"] == 2000001
    assert row["value"] == 2
    assert row["threshold"] == 1
    assert "error rate 0.4 fraction, threshold 0.2" in row["detail"]
    assert "turn latency p95" in row["detail"]
    assert "engineer@323133-1135" in row["detail"]
    assert "t414" in row["detail"]
    summary["window"]["toTimestamp"] = "2026-10-11T19:10:00Z"
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2000002, limits)
    assert standing.read(redis, "rig-grade-swarm")["langfuse_account_nestorcoltathotmail_com"]["fired"] == 2


def test_failed_read_preserves_findings_and_waits_until_interval_before_retry():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2000000, limits)
    before = standing.read(redis, "rig-grade-swarm")
    store = SimpleNamespace(redis=redis, execution_registry=SimpleNamespace(records=lambda slug: bindings))
    calls = []

    def read(slug, window, timeout):
        calls.append(window)
        if len(calls) == 1:
            raise TimeoutError("private upstream message")
        return {"swarm": slug, "window": window, "agents": []}

    langfuse.refresh(store, "rig-grade-swarm", 3600000, read=read)
    langfuse.refresh(store, "rig-grade-swarm", 3600001, read=read)
    assert len(calls) == 1
    assert standing.read(redis, "rig-grade-swarm") == before
    langfuse.refresh(store, "rig-grade-swarm", 3600000 + limits.interval_ms, read=read)
    assert len(calls) == 2
    assert standing.read(redis, "rig-grade-swarm") == {}


def test_threshold_boundaries_and_minimum_sample_counts():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    row = summary["agents"][0]
    summary["agents"] = [row]
    row.update(observations=10, error_rate=0.2, turns=3, turn_latency_ms={"p95": 600000})
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 1, limits) == 0
    row.update(observations=9, error_rate=0.9, turns=2, turn_latency_ms={"p95": 900000})
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2, limits) == 0
    row.update(observations=10, turns=3)
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 3, limits) == 1
    assert len(standing.read(redis, "rig-grade-swarm")) == 1


def test_unavailable_turn_latency_keeps_error_breaches_and_other_accounts():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2000000, limits)
    summary["agents"][0]["turn_latency_ms"]["p95"] = None
    summary["agents"][0]["error_rate"] = 0.4
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2000001, limits) == 2
    rows = standing.read(redis, "rig-grade-swarm")
    assert "error rate 0.4" in rows["langfuse_account_nestorcoltathotmail_com"]["detail"]
    assert "turn latency p95" in rows["langfuse_account_nestor_coltatthecloudclockwork_com"]["detail"]
    summary["agents"][0]["error_rate"] = 0
    before = rows["langfuse_account_nestorcoltathotmail_com"]
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2000002, limits)
    assert standing.read(redis, "rig-grade-swarm")["langfuse_account_nestorcoltathotmail_com"] == before
