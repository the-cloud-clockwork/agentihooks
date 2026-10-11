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
