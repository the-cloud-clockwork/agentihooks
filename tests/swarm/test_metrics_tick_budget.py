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
