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
