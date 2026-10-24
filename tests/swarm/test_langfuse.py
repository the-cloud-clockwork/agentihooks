import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parents[1] / "fixtures/swarm/langfuse-summary.json"


def test_recorded_long_turn_sample_without_errors_yields_no_finding():
    import fakeredis

    from scripts.swarm import langfuse
    from scripts.swarm.health import standing
    from scripts.swarm.langfuse_config import Limits

    sample = json.loads(FIXTURE.read_text())
    assert [row["turn_latency_ms"]["p95"] > 600000 for row in sample["summary"]["agents"]] == [True, False, True]
    assert {row["error_rate"] for row in sample["summary"]["agents"]} == {0}
    bindings = [SimpleNamespace(**row) for row in sample["bindings"]]
    redis = fakeredis.FakeRedis(decode_responses=True)
    assert langfuse.apply(redis, "rig-grade-swarm", sample["summary"], bindings, 2000000, Limits()) == 0
    assert standing.read(redis, "rig-grade-swarm") == {}


def test_error_breaches_yield_one_standing_finding_per_breaching_account():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2000000, limits) == 2
    rows = standing.read(redis, "rig-grade-swarm")
    assert set(rows) == {
        "langfuse_account_nestorcoltathotmail_com",
        "langfuse_account_nestor_coltatthecloudclockwork_com",
    }
    hotmail = rows["langfuse_account_nestorcoltathotmail_com"]["detail"]
    assert "agent engineer@323133-1125; task ncx5; error rate 0.36 fraction, threshold 0.2" in hotmail
    clockwork = rows["langfuse_account_nestor_coltatthecloudclockwork_com"]["detail"]
    assert "agent engineer@323133-1138; task t439; error rate 0.3 fraction, threshold 0.2" in clockwork
    assert all("turn latency" not in row["detail"] for row in rows.values())
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

    sample = json.loads(FIXTURE.read_text())
    summary = sample["summary"]
    summary["agents"][0].update(errors=119, error_rate=0.36)
    summary["agents"][2].update(errors=74, error_rate=0.3)
    return (
        fakeredis.FakeRedis(decode_responses=True),
        summary,
        [SimpleNamespace(**row) for row in sample["bindings"]],
        Limits(),
    )


@pytest.fixture(autouse=True)
def configured_endpoint(monkeypatch):
    monkeypatch.setenv("LANGFUSE_MCP_URL", "http://fixture/mcp/")


def test_unconfigured_sampling_reports_once_and_resumes_without_losing_findings(monkeypatch):
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 1, limits)
    before = standing.read(redis, "rig-grade-swarm")
    calls = []

    def read(slug, window, timeout):
        calls.append(slug)
        return {"swarm": slug, "window": window, "agents": []}

    store = SimpleNamespace(redis=redis, execution_registry=SimpleNamespace(records=lambda slug: bindings))
    langfuse.refresh(store, "rig-grade-swarm", 3599999, read=lambda *args: summary)
    before = standing.read(redis, "rig-grade-swarm")
    monkeypatch.delenv("LANGFUSE_MCP_URL")
    assert langfuse.refresh(store, "rig-grade-swarm", 3600000, read=read) == [
        "Langfuse sampling is off: configure LANGFUSE_MCP_URL"
    ]
    assert langfuse.refresh(store, "rig-grade-swarm", 3600001, read=read) == []
    assert calls == []
    assert standing.read(redis, "rig-grade-swarm") == before
    monkeypatch.setenv("LANGFUSE_MCP_URL", "http://fixture/mcp/")
    assert langfuse.refresh(store, "rig-grade-swarm", 3600002, read=read) == [
        "Langfuse summary read: 0 breaching accounts"
    ]
    assert calls == ["rig-grade-swarm"]
    assert standing.read(redis, "rig-grade-swarm") == {}


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


def test_error_breaches_of_two_agents_share_one_account_finding_and_repeat_updates():
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
    assert "error rate 0.36 fraction, threshold 0.2" in row["detail"]
    assert "turn latency" not in row["detail"]
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
    assert json.loads(redis.get(f"{langfuse.ROOT}:swarm:rig-grade-swarm:langfuse-summary")) == {
        "tried_at": 3600000,
        "error": "TimeoutError",
    }
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
    row.update(observations=10, error_rate=0.2, turns=50, turn_latency_ms={"p50": 3000000, "p95": 3000000})
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 1, limits) == 0
    row.update(observations=9, error_rate=0.9)
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2, limits) == 0
    row.update(observations=10)
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 3, limits) == 1
    assert len(standing.read(redis, "rig-grade-swarm")) == 1


def test_only_error_rate_breaches_whatever_the_turn_latency():
    from scripts.swarm.langfuse import breaches

    _, summary, _, limits = recorded_case()
    row = summary["agents"][0]
    row.update(observations=limits.min_observations, error_rate=0.4, turns=0, turn_latency_ms={"p95": None})
    assert breaches(row, limits) == [("error rate", 0.4, limits.error_rate, "fraction")]
    row.update(error_rate=0, turns=100, turn_latency_ms={"p50": 9000000, "p95": 9000000})
    assert breaches(row, limits) == []


def test_missing_turn_latency_no_longer_holds_a_finding_open():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 1, limits)
    summary["agents"][0].update(error_rate=0, errors=0, turns=8, turn_latency_ms={"p50": None, "p95": None})
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2, limits) == 1
    assert set(standing.read(redis, "rig-grade-swarm")) == {"langfuse_account_nestor_coltatthecloudclockwork_com"}


def test_long_turns_without_errors_clear_an_earlier_error_finding():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 1, limits)
    assert "langfuse_account_nestorcoltathotmail_com" in standing.read(redis, "rig-grade-swarm")
    summary["agents"][0].update(
        observations=331, error_rate=0, errors=0, turns=8, turn_latency_ms={"p50": 3000000, "p95": 3000000}
    )
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2, limits) == 1
    assert set(standing.read(redis, "rig-grade-swarm")) == {"langfuse_account_nestor_coltatthecloudclockwork_com"}


def test_account_evidence_retains_units_window_and_stage_numbers():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    row = summary["agents"][0]
    summary["agents"] = [row]
    row.update(error_rate=0.4, turn_latency_ms={"p95": 900000})
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2000000, limits)
    finding = standing.read(redis, "rig-grade-swarm")[langfuse.PREFIX + bindings[0].account]
    assert finding["value"] == 2
    assert finding["threshold"] == 1
    assert finding["unit"] == "threshold ratio"
    assert finding["detail"] == (
        "account nestorcoltathotmail_com; window 2026-10-11T18:00:00Z to 2026-10-11T19:00:00Z; "
        "agent engineer@323133-1125; task ncx5; error rate 0.4 fraction, threshold 0.2"
    )


def test_unbound_agents_and_missing_accounts_produce_no_findings():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    assert langfuse.apply(redis, "rig-grade-swarm", summary, [], 1, limits) == 0
    for binding in bindings:
        binding.account = ""
    assert langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2, limits) == 0
    assert standing.read(redis, "rig-grade-swarm") == {}


def test_foreign_swarm_summary_is_refused_before_findings_change():
    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 1, limits)
    before = standing.read(redis, "rig-grade-swarm")
    summary["swarm"] = "another-swarm"
    with pytest.raises(ValueError) as error:
        langfuse.apply(redis, "rig-grade-swarm", summary, bindings, 2, limits)
    assert str(error.value) == "Langfuse summary belongs to another swarm"
    assert standing.read(redis, "rig-grade-swarm") == before


def test_scheduled_breach_uses_execution_identity_and_persists_success_state(monkeypatch):
    import time

    from scripts.swarm import langfuse
    from scripts.swarm.health import standing

    redis, summary, bindings, limits = recorded_case()
    registry_calls = []

    def records(slug):
        registry_calls.append(slug)
        return bindings

    store = SimpleNamespace(redis=redis, execution_registry=SimpleNamespace(records=records))
    monkeypatch.setenv("TZ", "EST5")
    time.tzset()
    try:
        assert langfuse.refresh(store, "rig-grade-swarm", 7200000, read=lambda *args: summary) == [
            "Langfuse summary read: 2 breaching accounts"
        ]
    finally:
        monkeypatch.undo()
        time.tzset()
    assert registry_calls == ["rig-grade-swarm"]
    rows = standing.read(redis, "rig-grade-swarm")
    assert len(rows) == 2
    assert all(row["updated_at"] == 7200000 for row in rows.values())
    assert json.loads(redis.get(f"{langfuse.ROOT}:swarm:rig-grade-swarm:langfuse-summary")) == {
        "tried_at": 7200000,
        "at": 7200000,
        "accounts": 2,
    }


def test_sampling_window_is_utc_in_a_non_utc_process(monkeypatch):
    import time

    from scripts.swarm import langfuse

    redis, _, _, limits = recorded_case()
    store = SimpleNamespace(redis=redis, execution_registry=SimpleNamespace(records=lambda slug: []))
    calls = []

    def read(slug, window, timeout):
        calls.append(window)
        return {"swarm": slug, "window": window, "agents": []}

    monkeypatch.setenv("TZ", "EST5")
    time.tzset()
    try:
        langfuse.refresh(store, "rig-grade-swarm", 7200000, read=read, limits=limits)
    finally:
        monkeypatch.undo()
        time.tzset()
    assert calls == [{"fromTimestamp": "1970-01-01T01:00:00Z", "toTimestamp": "1970-01-01T02:00:00Z"}]
