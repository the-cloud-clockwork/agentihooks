import pytest

from scripts.doctor import context_overruns

pytestmark = pytest.mark.unit

AGENTS = [
    {"name": "engineer@323133-0001", "conversation_id": "marked", "state": "working"},
    {"name": "master@323133-0175", "conversation_id": "c214557b", "state": "working"},
    {"name": "ci@323133-0002", "conversation_id": "", "state": "working"},
]


def test_a_swarm_session_past_the_hard_gate_with_no_marker_is_a_finding():
    asked = []

    def overrun(session):
        asked.append(session)
        return 930_500 if session == "c214557b" else None

    found = context_overruns.findings(AGENTS, overrun)
    assert asked == ["marked", "c214557b"]
    assert [f.id for f in found] == ["context-overrun/master@323133-0175"]
    assert found[0].summary == "swarm session passed the hard gate with no handoff preparation or recycle"
    assert found[0].measure == 930
    assert (
        found[0].threshold == "context past the session's compact limit plus margin with no prepare or recycle marker"
    )
    assert found[0].evidence == ("session c214557b", "context 930k tokens", "state working")


def test_no_overrun_no_finding():
    assert context_overruns.findings(AGENTS, lambda session: None) == []
