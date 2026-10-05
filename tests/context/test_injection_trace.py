"""Injection trace: the recorder called by the injecting hooks, and agentihooks trace."""

import json
from unittest.mock import patch

import pytest

from scripts.trace_cli import main as trace

SID = "sess-trace-1"


@pytest.fixture()
def broadcasts(tmp_path):
    path = tmp_path / "broadcast.json"
    messages = [
        {"id": "bc-op", "message": "Deploy freeze", "severity": "alert", "persistent": True, "source": "operator"},
        {"id": "bc-brain", "message": "Hot arcs", "severity": "info", "persistent": False, "source": "brain-adapter"},
    ]
    for message in messages:
        message.update(created_at="2026-10-05T00:00:00Z", ttl_seconds=3600, expires_at="2999-01-01T00:00:00Z")
    path.write_text(json.dumps(messages))
    with patch("hooks.context.broadcast._broadcast_path", return_value=path):
        yield [m["id"] for m in messages]


def _enforcements():
    return [
        {"id": "bundle-rule", "message": "from the bundle", "cadence": 5, "source": "bundle"},
        {"id": "profile-rule", "message": "from the profile", "cadence": 5, "source": "profile"},
        {"id": "runtime-rule", "message": "set at runtime", "cadence": 5, "source": "runtime"},
    ]


def _deliver_one_of_each(broadcast_ids):
    from hooks.context import conditions, enforcement
    from hooks.context.broadcast import get_broadcast_context

    with patch.object(enforcement, "load_all_enforcements", return_value=_enforcements()):
        assert enforcement.get_session_start_enforcements(SID)
    entry = {"file": "pre-bash.guard.sh", "path": "/x/pre-bash.guard.sh"}
    result = conditions.merge(
        "pre", {"session_id": SID, "tool_name": "Bash"}, [(entry, {"returncode": 0, "stdout": "mind it"})]
    )
    assert result.contexts
    assert get_broadcast_context(SID, broadcast_ids)


def _rows(out):
    return [line.split("\t") for line in out.splitlines() if line and not line.startswith("corrections")]


def test_one_directive_from_each_layer_lists_all_six(broadcasts, capsys):
    _deliver_one_of_each(broadcasts)

    assert trace([SID]) == 0
    rows = _rows(capsys.readouterr().out)
    by_source = {row[2]: row[1] for row in rows}
    assert by_source == {
        "bundle-rule": "bundle",
        "profile-rule": "profile",
        "runtime-rule": "enforcement",
        "pre-bash.guard.sh": "condition",
        "bc-op": "broadcast",
        "bc-brain": "brain",
    }
    assert all(row[0].endswith("Z") for row in rows)


def test_a_session_with_nothing_injected_lists_nothing(broadcasts, capsys):
    _deliver_one_of_each(broadcasts)

    assert trace(["another-session"]) == 0
    assert capsys.readouterr().out == ""


def test_a_correction_is_stored_and_shown(broadcasts, capsys):
    _deliver_one_of_each(broadcasts)

    assert trace([SID, "--wrong", "profile-rule", "--repo", "/repos/qitp", "--reason", "not here"]) == 0
    capsys.readouterr()
    assert trace([SID]) == 0
    out = capsys.readouterr().out
    assert "corrections" in out
    assert any("/repos/qitp" in line and "profile" in line and "not here" in line for line in out.splitlines())
    assert trace(["--corrections"]) == 0
    assert "profile-rule" in capsys.readouterr().out


def test_a_correction_for_a_source_the_session_never_received_is_refused(broadcasts, capsys):
    _deliver_one_of_each(broadcasts)

    assert trace([SID, "--wrong", "no-such-rule", "--repo", "/r", "--reason", "x"]) == 2
    assert trace(["--corrections"]) == 0
    assert "no-such-rule" not in capsys.readouterr().out


def test_recording_never_blocks_when_its_store_fails(tmp_path, monkeypatch):
    from hooks.context import enforcement, injection_trace

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", blocker)

    injection_trace.record(SID, "bundle", "x", "y")
    with patch.object(enforcement, "load_all_enforcements", return_value=_enforcements()):
        assert "bundle-rule" in enforcement.get_session_start_enforcements(SID)


def test_an_injection_dropped_for_lack_of_a_pretool_channel_is_not_recorded(capsys):
    from hooks.context import enforcement

    with patch.object(enforcement, "load_all_enforcements", return_value=_enforcements()):
        assert enforcement.get_pretool_enforcements(SID, claim_unseen=False, tool_call_count=5)

    assert trace([SID]) == 0
    assert capsys.readouterr().out == ""
