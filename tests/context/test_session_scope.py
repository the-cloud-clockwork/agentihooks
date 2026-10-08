import json
from pathlib import Path

import pytest

from hooks.context import project_sessions
from hooks.context.brain_writer_hook import _marker_request, _parse_transcript_for_markers
from hooks.context.project_identity import ProjectIdentity
from hooks.context.project_sessions import (
    ScopeRefused,
    SessionGrant,
    attribute,
    observe_transcript,
    record_scope,
    record_session,
    scope_at,
    transitions,
    unattributed_session_events_total,
)

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "swarm_v2" / "session-scope.json").read_text())
GRANT = SessionGrant(frozenset(FIXTURE["grant"]["project_ids"]))


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "state")
    return tmp_path


def _record_all(session_id, grant=GRANT):
    return [record_scope(session_id, item["scope"], item["at"], grant) for item in FIXTURE["transitions"]]


def _transcript(tmp_path, name="session.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(entry) for entry in FIXTURE["transcript"]) + "\n")
    return path


def _attributed(session_id, tmp_path, grant=GRANT):
    markers = _parse_transcript_for_markers(str(_transcript(tmp_path, f"{session_id}.jsonl")), 20)
    return attribute(session_id, [{"at": m.get("at", ""), "attrs": m["attrs"]} for m in markers], grant)


def _projected(results):
    keys = ("attribution", "project_id", "worktree", "task", "task_revision", "lane")
    return [{key: result[key] for key in keys if result.get(key)} for result in results]


def test_task_and_worktree_changes_keep_event_time_scope(home):
    rows = _record_all("first")
    assert [row["sequence"] for row in rows] == [0, 1, 2, 3]
    results = _attributed("first", home)
    assert _projected(results) == FIXTURE["expected"]
    assert unattributed_session_events_total(results) == 1


def test_a_second_independent_fixture_gives_the_same_attribution(home):
    _record_all("first")
    _record_all("second")
    assert _projected(_attributed("second", home)) == FIXTURE["expected"]
    assert len(transitions("first")) == len(transitions("second")) == 4


def test_scope_at_reads_the_transition_in_force(home):
    _record_all("first")
    assert scope_at("first", "2026-10-08T09:00:00Z") is None
    assert scope_at("first", "2026-10-08T10:10:00Z")["worktree"] == "two"
    assert scope_at("first", "2026-10-08T10:19:59+00:00")["task"] == "first"
    assert scope_at("first", "2026-10-08T12:20:00+02:00")["task"] == "second"
    assert scope_at("first", "not a time") is None


def test_unchanged_scope_records_nothing(home):
    first = FIXTURE["transitions"][0]
    assert record_scope("first", first["scope"], first["at"]) is not None
    assert record_scope("first", first["scope"], "2026-10-08T10:01:00+00:00") is None
    assert len(transitions("first")) == 1


def test_marker_request_uses_the_scope_at_marker_time(home):
    _record_all("first")
    markers = _parse_transcript_for_markers(str(_transcript(home)), 20)
    assert [m.get("at") for m in markers][1] == "2026-10-08T10:05:00Z"
    body, _ = _marker_request(markers[1], "first")
    assert body["attrs"]["task"] == "first"
    assert body["attrs"]["worktree"] == "one"
    assert body["attrs"]["attribution"] == "scoped"
    shared, _ = _marker_request(markers[4], "first")
    assert shared["attrs"]["attribution"] == "fleet"
    assert "project_id" not in shared["attrs"]
    assert "project" not in shared["attrs"]


def test_explicit_marker_identity_wins_when_granted(home):
    _record_all("first")
    event = {"at": "2026-10-08T10:05:00Z", "attrs": {"project_id": "github.com/fixture/beta", "project": "beta"}}
    [result] = attribute("first", [event], GRANT)
    assert result["attribution"] == "explicit"
    assert result["project_id"] == "github.com/fixture/beta"
    assert result["task"] == "first"


def test_project_outside_the_grant_is_refused_without_writing(home):
    _record_all("first")
    gamma = home / "gamma"
    gamma.mkdir()
    outside = {**FIXTURE["outside_grant"], "cwd": str(gamma)}
    before = project_sessions._scope_path("first").read_bytes()
    with pytest.raises(ScopeRefused) as refused:
        record_scope("first", outside, "2026-10-08T11:00:00+00:00", GRANT)
    assert "gamma" not in str(refused.value)
    assert project_sessions._scope_path("first").read_bytes() == before
    with pytest.raises(ScopeRefused):
        record_scope("fresh", outside, "2026-10-08T11:00:00+00:00", GRANT)
    assert not project_sessions._scope_path("fresh").exists()


def test_explicit_marker_project_outside_the_grant_is_refused(home):
    _record_all("first")
    event = {"at": "2026-10-08T10:05:00Z", "attrs": {"project_id": FIXTURE["outside_grant"]["project_id"]}}
    results = attribute("first", [event], GRANT)
    assert results == [{"attribution": "refused"}]
    assert unattributed_session_events_total(results) == 1


def test_unknown_project_is_not_a_grant_claim(home):
    assert record_scope("first", {"cwd": "/scratch"}, "2026-10-08T10:00:00+00:00", GRANT)["project_id"] == ""
    [result] = attribute("first", [{"at": "2026-10-08T10:01:00Z"}], GRANT)
    assert result["attribution"] == "unknown"
    assert result["cwd"] == "/scratch"


def test_an_older_transition_cannot_overwrite_a_newer_one(home):
    _record_all("first")
    with pytest.raises(ScopeRefused):
        record_scope("first", FIXTURE["outside_grant"], "2026-10-08T10:05:00+00:00")
    with pytest.raises(ScopeRefused):
        record_scope("first", {"task": "late"}, "not a time")
    assert len(transitions("first")) == 4


def test_replay_reconstructs_the_same_attribution_without_duplicates(home):
    _record_all("first")
    before = project_sessions._scope_path("first").read_bytes()
    assert _record_all("first") == [None, None, None, None]
    assert project_sessions._scope_path("first").read_bytes() == before
    assert _projected(_attributed("first", home)) == FIXTURE["expected"]


def test_an_interrupted_write_leaves_accepted_transitions_readable(home):
    first, second = FIXTURE["transitions"][:2]
    record_scope("first", first["scope"], first["at"])
    path = project_sessions._scope_path("first")
    with path.open("a") as stream:
        stream.write('{"session_id": "first", "sequ')
    assert len(transitions("first")) == 1
    assert record_scope("first", second["scope"], second["at"])["sequence"] == 1
    assert [row["worktree"] for row in transitions("first")] == ["one", "two"]


def test_unsafe_session_ids_are_never_stored(home):
    first = FIXTURE["transitions"][0]
    assert record_scope("../escape", first["scope"], first["at"]) is None
    assert transitions("../escape") == []
    assert not (home / "state").exists()


def test_transcript_observation_records_the_worktree_switch(home, monkeypatch):
    identities = {
        "/work/alpha/one": ProjectIdentity(
            "alpha", "fixture/alpha", "one", "/work/alpha/one", "", "github.com/fixture/alpha"
        ),
        "/work/alpha/two": ProjectIdentity(
            "alpha", "fixture/alpha", "two", "/work/alpha/two", "", "github.com/fixture/alpha"
        ),
        "/work/beta": ProjectIdentity("beta", "fixture/beta", "beta", "/work/beta", "", "github.com/fixture/beta"),
    }
    calls = []

    def resolve(cwd, env=None):
        calls.append(cwd)
        return identities.get(cwd)

    monkeypatch.setattr(project_sessions, "resolve_project", resolve)
    env = {"AGENTIHOOKS_SWARM": "fixture", "AGENTIHOOKS_SWARM_TASK": "first", "AGENTIHOOKS_SWARM_LANE": "eng"}
    path = _transcript(home)
    assert observe_transcript("live", str(path), env) == 4
    rows = transitions("live")
    assert [(row["worktree"], row["branch"]) for row in rows] == [
        ("", ""),
        ("one", "one"),
        ("two", "two"),
        ("beta", "main"),
    ]
    assert {row["task"] for row in rows} == {"first"}
    assert calls == ["/scratch", "/work/alpha/one", "/work/alpha/two", "/work/beta"]
    assert observe_transcript("live", str(path), env) == 0
    assert len(transitions("live")) == 4
    assert observe_transcript("live", str(home / "missing.jsonl"), env) == 0


def test_session_start_records_the_launch_scope(home, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "fixture")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "first")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", "eng")
    monkeypatch.setattr(project_sessions, "_branch", lambda cwd: "one")
    identity = ProjectIdentity("alpha", "fixture/alpha", "one", "/work/alpha/one", "", "github.com/fixture/alpha")
    record_session("live", identity)
    record_session("live", identity)
    [row] = transitions("live")
    assert (row["project_id"], row["task"], row["lane"], row["branch"]) == (
        "github.com/fixture/alpha",
        "first",
        "eng",
        "one",
    )
    record_session("live", None)
    assert transitions("live")[-1]["project_id"] == ""


def test_transitions_and_attributions_match_the_schema(home):
    from jsonschema import Draft202012Validator, ValidationError

    schema = json.loads((Path(__file__).parents[2] / "docs/swarm-v2/schemas/session-scope.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    rows = _record_all("first")
    for row in rows:
        validator.validate(row)
    for result in _attributed("first", home) + [{"attribution": "refused"}]:
        validator.validate(result)
    with pytest.raises(ValidationError):
        validator.validate({"attribution": "fleet", "project_id": "github.com/fixture/alpha"})
    with pytest.raises(ValidationError):
        validator.validate({"attribution": "scoped", "task": "first"})
    with pytest.raises(ValidationError):
        validator.validate({**rows[0], "grant_ref": "forged-display-grant"})
