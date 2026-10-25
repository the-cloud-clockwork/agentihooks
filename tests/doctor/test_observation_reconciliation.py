import json

import pytest

from hooks.observability import agent_trace
from scripts.doctor import registry
from tests.doctor.test_registry import AGENT, Langfuse, _session, _trace, cursors

__all__ = ["cursors"]


@pytest.mark.parametrize("missing", [False, True])
def test_live_reader_reconciles_paged_identities_instead_of_the_inflated_aggregate(cursors, missing):
    transcript = _session(
        cursors,
        "sid",
        ["2026-10-07T08:00:00Z", "2026-10-07T08:00:01Z"],
        1,
        paged={"spans": 9},
        accepted_at="2026-10-07T08:00:10Z",
    )
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    first = agent_trace.session_spans(records[:1], agent_trace.Identity("sid"))
    ids = {f"{span.span_id:016x}" for span in first}
    rows = [{"id": key, "startTime": "2026-10-07T08:00:00Z", "endTime": "2026-10-07T08:00:00Z"} for key in sorted(ids)]
    if missing:
        rows.pop()
    backend = Langfuse([_trace("tr", "sid")], rows, total=len(rows))
    [binding], failures = registry.read("s", [{**AGENT, "conversation_id": "sid"}], backend, {}, 15, 50)
    assert failures == []
    result = binding["remote"]["reconciliation"]
    assert result["accepted"] == 2
    assert result["drift"] == 7
    assert result["missing"] == (sorted(ids - {row["id"] for row in rows}))
    assert len(result["missing"]) == int(missing)
