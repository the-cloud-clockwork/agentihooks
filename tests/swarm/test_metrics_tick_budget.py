import pytest

from scripts.swarm import metrics_outbox

pytestmark = pytest.mark.unit


def test_pruning_does_not_scan_metrics_still_inside_retention(tmp_path):
    now = 2 * metrics_outbox.DAY_MS
    table = metrics_outbox.Table("samples", (("value", "Int64"),))
    box = metrics_outbox.Outbox(
        tmp_path / "metrics.sqlite", metrics_outbox.Settings("https://sink", "user", "password"), send=lambda *_: True
    )
    try:
        box.append(
            table,
            [
                {
                    "event_id": str(i),
                    "ledger": "sw",
                    "plan": "",
                    "phase": "",
                    "slice": "",
                    "task": "",
                    "ts_ms": now,
                    "value": i,
                }
                for i in range(10_000)
            ],
        )
        box.flush(now)
        box.append(
            table,
            [
                {
                    "event_id": "expired",
                    "ledger": "sw",
                    "plan": "",
                    "phase": "",
                    "slice": "",
                    "task": "",
                    "ts_ms": 1,
                    "value": -1,
                }
            ],
        )
        box.db.execute("UPDATE spool SET shipped = 1 WHERE event_id = 'expired'")
        box.db.commit()
        steps = []
        box.db.set_progress_handler(lambda: steps.append(1) or 0, 100)

        assert box.flush(now) == 0

        box.db.set_progress_handler(None, 0)
        assert len(steps) < 20, f"Retention scanned current metrics: at least {len(steps) * 100} SQLite operations"
        assert len(box.recent("samples", now)) == 10_000
        assert box.db.execute("SELECT count(*) FROM spool WHERE event_id = 'expired'").fetchone()[0] == 0
    finally:
        box.close()


def test_slow_metrics_sink_yields_and_resumes_every_unshipped_row(tmp_path, monkeypatch):
    import time

    now = 2 * metrics_outbox.DAY_MS
    elapsed, received = [0.0], []
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])

    def send(sink, query, body):
        import json

        elapsed[0] += 2.0
        if query.startswith("INSERT"):
            received.extend(json.loads(line)["event_id"] for line in body.splitlines())
        return True

    path = tmp_path / "metrics.sqlite"
    sink = metrics_outbox.Settings("https://sink", "user", "password")
    box = metrics_outbox.Outbox(path, sink, send)
    try:
        for index in range(16):
            box.append(
                metrics_outbox.Table(f"samples_{index}"),
                [
                    {
                        "event_id": str(index),
                        "ledger": "sw",
                        "plan": "",
                        "phase": "",
                        "slice": "",
                        "task": "",
                        "ts_ms": now,
                    }
                ],
            )
        box.flush(now)
        assert elapsed[0] <= 10, f"One metrics flush held the tick for {elapsed[0]} seconds"
    finally:
        box.close()

    for _ in range(64):
        box = metrics_outbox.Outbox(path, sink, send)
        try:
            started = elapsed[0]
            box.flush(now)
            assert elapsed[0] - started <= 10
        finally:
            box.close()
        if len(received) == 16:
            break
    assert sorted(received, key=int) == [str(index) for index in range(16)]
