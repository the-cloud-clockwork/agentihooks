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


def _rule_row(tmp_path, layer="rule", body="# Worktrees\nNever edit the primary checkout.\n"):
    repo = tmp_path / "bundle"
    (repo / "rules").mkdir(parents=True, exist_ok=True)
    (repo / "rules" / "worktrees.md").write_text(body)
    locator = {"repo": str(repo), "path": "rules/worktrees.md", "blob": "b1"}
    return {"layer": layer, "source": "bundle/rules/worktrees.md", "locator": locator, "text": "# Worktrees"}


def _manifest(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))


def test_session_start_records_the_render_and_priming_manifests_once(tmp_path, capsys):
    from hooks import config
    from hooks.context import injection_trace

    home = config.AGENTIHOOKS_HOME
    note = {"layer": "learned", "source": "learned:eng-2@sw#3", "locator": {"seat": "eng-2@sw", "note": 3}}
    _manifest(home / "profiles" / "engineer" / "codex.sources.json", [_rule_row(tmp_path)])
    _manifest(home / "profiles" / "engineer" / "claude.sources.json", [{**_rule_row(tmp_path), "source": "other"}])
    _manifest(home / "swarm" / "sw" / "prompts" / "eng@x-1.sources.json", [{**note, "text": "merge fast"}])
    env = {"AGENTIHOOKS_PROFILE": "engineer", "AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "eng@x-1"}

    injection_trace.record_session_start(SID, env, "codex")
    injection_trace.record_session_start(SID, env, "codex")

    assert trace([SID]) == 0
    rows = _rows(capsys.readouterr().out)
    located = f"repo={tmp_path / 'bundle'} path=rules/worktrees.md blob=b1"
    assert [(row[1], row[2], row[3], row[4]) for row in rows] == [
        ("rule", "bundle/rules/worktrees.md", located, "# Worktrees"),
        ("learned", "learned:eng-2@sw#3", "seat=eng-2@sw note=3", "merge fast"),
    ]


def test_session_start_outside_a_profile_and_swarm_records_nothing(tmp_path, capsys):
    from hooks import config
    from hooks.context import injection_trace

    _manifest(config.AGENTIHOOKS_HOME / "profiles" / "engineer" / "claude.sources.json", [_rule_row(tmp_path)])
    injection_trace.record_session_start(SID, {"AGENTIHOOKS_SWARM": "sw"}, "claude")
    assert trace([SID]) == 0
    assert capsys.readouterr().out == ""


def test_a_swarm_session_without_an_agent_name_has_no_priming_manifest():
    from hooks.context import injection_trace

    assert injection_trace.manifests({"AGENTIHOOKS_SWARM": "sw"}, "claude") == []
    assert injection_trace.manifests({"AGENTIHOOKS_AGENT_NAME": "eng@x-1"}, "claude") == []


def test_an_unreadable_manifest_records_nothing(capsys):
    from hooks import config
    from hooks.context import injection_trace

    path = config.AGENTIHOOKS_HOME / "profiles" / "engineer" / "claude.sources.json"
    path.parent.mkdir(parents=True)
    for text in ("{not json", json.dumps({"rows": []})):
        path.write_text(text)
        injection_trace.record_session_start(SID, {"AGENTIHOOKS_PROFILE": "engineer"}, "claude")
    assert trace([SID]) == 0
    assert capsys.readouterr().out == ""


def test_the_session_start_hook_records_the_manifests_for_its_target(monkeypatch):
    import hooks.common as common
    from hooks import hook_manager
    from hooks.context import injection_trace

    calls = []
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "engineer")
    monkeypatch.setattr(common, "inject_context", lambda *a, **k: None)
    monkeypatch.setattr(injection_trace, "record_session_start", lambda *args: calls.append(args))
    monkeypatch.setattr("hooks.lifecycle.deps_kick.kick", lambda: False)
    hook_manager.on_session_start({"hook_event_name": "SessionStart", "session_id": SID, "cwd": "/tmp"})
    [(session, environ, target)] = calls
    assert (session, environ["AGENTIHOOKS_PROFILE"], target) == (SID, "engineer", "codex")


def _received(tmp_path, layer="rule"):
    from hooks.context import injection_trace

    row = _rule_row(tmp_path, layer)
    injection_trace.record_rows(SID, [row])
    return row


def test_a_quoted_passage_of_a_received_rule_file_is_kept_on_the_correction(tmp_path, capsys):
    from hooks.context import injection_trace

    row = _received(tmp_path)
    quote = "Never edit the   primary checkout."
    assert trace([SID, "--wrong", row["source"], "--repo", "/r", "--reason", "wrong here", "--quote", quote]) == 0
    assert capsys.readouterr().out.splitlines()[-1].split("\t")[-2:] == ["wrong here", quote]
    stored = injection_trace.corrections()[-1]
    assert (stored["layer"], stored["quote"]) == ("rule", quote)


def test_a_correction_without_a_quote_keeps_no_quote(tmp_path, capsys):
    from hooks.context import injection_trace

    row = _received(tmp_path)
    assert trace([SID, "--wrong", row["source"], "--repo", "/r", "--reason", "x"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].split("\t")[-1] == "x"
    assert "quote" not in injection_trace.corrections()[-1]


def test_a_quote_absent_from_the_file_is_refused(tmp_path, capsys):
    from hooks.context import injection_trace

    row = _received(tmp_path)
    assert trace([SID, "--wrong", row["source"], "--repo", "/r", "--reason", "x", "--quote", "not in it"]) == 2
    assert "the quoted passage is not in" in capsys.readouterr().err
    assert injection_trace.corrections() == []


def test_a_quote_on_a_directive_that_is_not_a_file_is_refused(tmp_path, capsys):
    from hooks.context import injection_trace

    row = _received(tmp_path, layer="learned")
    assert trace([SID, "--wrong", row["source"], "--repo", "/r", "--reason", "x", "--quote", "Never"]) == 2
    assert "is a learned directive" in capsys.readouterr().err
    assert injection_trace.corrections() == []


def test_a_quote_on_a_doctrine_file_that_moved_is_refused(tmp_path, capsys):
    from hooks.context import injection_trace

    row = _received(tmp_path, layer="doctrine")
    (tmp_path / "bundle" / "rules" / "worktrees.md").unlink()
    assert trace([SID, "--wrong", row["source"], "--repo", "/r", "--reason", "x", "--quote", "Never"]) == 2
    assert "cannot read" in capsys.readouterr().err
    assert injection_trace.corrections() == []
