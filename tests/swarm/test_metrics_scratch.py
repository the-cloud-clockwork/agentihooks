from types import SimpleNamespace

import pytest

from scripts.swarm import metrics, metrics_outbox, metrics_swarm


@pytest.mark.parametrize("slug", ["live-looking", "proof-looking"])
def test_scratch_marker_prevents_all_metrics_and_shared_spool_work(tmp_path, monkeypatch, slug):
    path = tmp_path / "shared.sqlite"
    sent = []
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: path)
    monkeypatch.setattr(metrics_outbox, "post", lambda *args: sent.append(args) or True)
    monkeypatch.setattr(metrics.metrics_ledger, "record", lambda *args: None)
    monkeypatch.setattr(metrics, "_collect", lambda *args: None)
    store = SimpleNamespace(config=lambda value: SimpleNamespace(scratch=True))
    swarm = metrics_swarm.TickInput(store, {}, [], lambda url: None, None)

    assert (
        metrics.record_pass(
            slug,
            1_800_000_000_000,
            3,
            {"AGENTIHOOKS_METRICS_URL": "http://ch:8123", "AGENTIHOOKS_METRICS_USER": "writer"},
            swarm,
        )
        == []
    )
    assert sent == []
    assert not path.exists()


@pytest.mark.parametrize("slug", ["ordinary", "proof-looking"])
def test_live_marker_keeps_export_enabled_regardless_of_name(tmp_path, monkeypatch, slug):
    path = tmp_path / "shared.sqlite"
    sent = []
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: path)
    monkeypatch.setattr(metrics_outbox, "post", lambda *args: sent.append(args) or True)
    monkeypatch.setattr(metrics.metrics_ledger, "record", lambda *args: None)
    monkeypatch.setattr(metrics, "_collect", lambda *args: None)
    store = SimpleNamespace(config=lambda value: SimpleNamespace(scratch=False))
    swarm = metrics_swarm.TickInput(store, {}, [], lambda url: None, None)

    assert (
        metrics.record_pass(
            slug,
            1_800_000_000_000,
            3,
            {"AGENTIHOOKS_METRICS_URL": "http://ch:8123", "AGENTIHOOKS_METRICS_USER": "writer"},
            swarm,
        )
        == []
    )
    assert path.exists()
    assert any(query == "INSERT INTO swarm.ticks FORMAT JSONEachRow" for _, query, _ in sent)


@pytest.mark.parametrize("scratch", [False, True])
def test_scratch_marker_survives_config_and_snapshot_round_trip(scratch):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0, scratch=scratch))
    assert store.config("sw").scratch is scratch
    restored = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    restored.restore("sw", store.export("sw"))
    assert restored.config("sw").scratch is scratch


def test_legacy_live_swarm_config_without_marker_keeps_export_enabled():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    store.redis.hdel(store.key("sw", "config"), "scratch")
    assert store.config("sw").scratch is False
