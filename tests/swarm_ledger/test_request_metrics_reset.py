import json
import time
from pathlib import Path

import pytest

from scripts.swarm_ledger import request_metrics

pytestmark = pytest.mark.xdist_group("request-metrics-reset")


def test_a_restore_with_real_time_sets_the_held_and_lost_journal_state(tmp_path: Path) -> None:
    now = time.time()
    path = tmp_path / request_metrics.JOURNAL_NAME
    path.write_text(
        json.dumps(["held", now - 100.0])
        + "\n"
        + json.dumps(["start", now - 50.0])
        + "\n"
        + json.dumps(["s", now - 10.0, "GET", "/", 0.1, 0.1, 0.0])
        + "\n"
    )
    request_metrics.restore(path)
    assert (request_metrics.JOURNAL, request_metrics.HELD_SINCE) == (path, now - 100.0)
    assert request_metrics.LOST == [(now - 10.0, request_metrics.STARTED_AT)]
    request_metrics.record("GET", "/", 0.1, 0.1, 0.0)
    assert request_metrics.PENDING


def test_the_next_test_on_the_worker_starts_with_an_empty_request_metrics_journal() -> None:
    assert request_metrics.JOURNAL is None
    assert request_metrics.HELD_SINCE is None
    assert request_metrics.LOST == []
    assert request_metrics.PENDING == []
    assert list(request_metrics.SAMPLES) == []
    assert request_metrics.EVICTED_THROUGH == float("-inf")
    assert (request_metrics.APPENDED, request_metrics.STALE) == (0, False)
