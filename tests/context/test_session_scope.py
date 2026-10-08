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


IDENTITIES = {
    "/work/alpha/one": ProjectIdentity(
        "alpha", "fixture/alpha", "one", "/work/alpha/one", "", "github.com/fixture/alpha"
    ),
    "/work/alpha/two": ProjectIdentity(
        "alpha", "fixture/alpha", "two", "/work/alpha/two", "", "github.com/fixture/alpha"
    ),
    "/work/beta": ProjectIdentity("beta", "fixture/beta", "beta", "/work/beta", "", "github.com/fixture/beta"),
}


def test_transcript_observation_records_the_worktree_switch(home, monkeypatch):
    calls = []

    def resolve(cwd, env=None):
        calls.append(cwd)
        return IDENTITIES.get(cwd)

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


def test_branch_is_read_from_git_only_for_a_folder(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "feature", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    assert project_sessions._branch(str(tmp_path)) == "feature"
    assert project_sessions._branch("") == ""


def test_scope_carries_the_identity_and_swarm_variables():
    env = {"AGENTIHOOKS_SWARM": "s", "AGENTIHOOKS_SWARM_TASK": "t", "AGENTIHOOKS_SWARM_LANE": "l"}
    identity = ProjectIdentity("alpha", "fixture/alpha", "one", "/work/alpha/one", "r", "github.com/fixture/alpha")
    assert project_sessions._scope(identity, "/ignored", "b", env) == {
        **identity.attributes(),
        "branch": "b",
        "swarm": "s",
        "task": "t",
        "lane": "l",
    }
    assert project_sessions._scope(None, "/scratch", "", {}) == {
        "cwd": "/scratch",
        "branch": "",
        "swarm": "",
        "task": "",
        "lane": "",
    }


def test_scope_log_location_time_zones_and_transition_identity(home):
    assert project_sessions._scope_path("a.b_c-1") == home / "state" / "brain" / "session-scopes" / "a.b_c-1.jsonl"
    assert project_sessions._scope_path("x" * 129) is None
    assert project_sessions._scope_path("-lead") is None
    record_scope("tz", {"task": "naive"}, "2026-10-08T10:00:00")
    assert scope_at("tz", "2026-10-08T10:00:00+00:00")["task"] == "naive"
    assert scope_at("tz", "2026-10-08T11:59:59+02:00") is None
    scope = FIXTURE["transitions"][0]["scope"]
    row = record_scope("order", dict(reversed(list(scope.items()))), FIXTURE["transitions"][0]["at"])
    values = {name: str(scope.get(name) or "") for name in project_sessions.SCOPE_FIELDS}
    expected = json.dumps(["order", FIXTURE["transitions"][0]["at"], values], sort_keys=True).encode()
    import hashlib

    assert row["transition_id"] == hashlib.sha256(expected).hexdigest()
    assert row["session_id"] == "order"
    assert row["at"] == FIXTURE["transitions"][0]["at"]


def _variant():
    text = json.dumps(FIXTURE).replace("2026-10-08", "2026-10-09")
    for old, new in (
        ("alpha", "@swap@"),
        ("beta", "alpha"),
        ("@swap@", "beta"),
        ("first", "@one@"),
        ("third", "first"),
    ):
        text = text.replace(old, new)
    return json.loads(text.replace("@one@", "third"))


def test_a_second_fixture_with_other_times_and_projects_attributes_independently(home, monkeypatch):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", home / "second-state")
    variant = _variant()
    for item in variant["transitions"]:
        record_scope("variant", item["scope"], item["at"], SessionGrant(frozenset(variant["grant"]["project_ids"])))
    path = home / "variant.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in variant["transcript"]) + "\n")
    markers = _parse_transcript_for_markers(str(path), 20)
    results = attribute("variant", [{"at": m.get("at", ""), "attrs": m["attrs"]} for m in markers])
    assert _projected(results) == variant["expected"]
    assert variant["expected"][1]["project_id"] == "github.com/fixture/beta"
    assert variant["expected"][1]["task"] == "third"
    assert not (home / "state").exists()


def test_fleet_markers_shed_every_scope_attribute(home):
    _record_all("first")
    marker = {"type": "signal", "content": "shared", "at": "2026-10-08T10:05:00Z"}
    marker["attrs"] = {"share": "fleet", "project_id": "github.com/fixture/gamma", "task": "first", "cwd": "/work"}
    body, _ = _marker_request(marker, "first")
    assert body["attrs"]["attribution"] == "fleet"
    assert not set(body["attrs"]) & set(project_sessions.SCOPE_FIELDS)


def test_explicit_identity_covers_only_the_project(home):
    _record_all("first")
    attrs = {"project_id": "github.com/fixture/beta", "task": "second", "task_revision": "9", "worktree": "x"}
    [result] = attribute("first", [{"at": "2026-10-08T10:05:00Z", "attrs": attrs}], GRANT)
    assert (result["project_id"], result["task"], result["task_revision"], result["worktree"]) == (
        "github.com/fixture/beta",
        "first",
        "1",
        "one",
    )
    assert "repo" not in result


def test_a_display_label_never_overrides_the_event_time_project(home):
    _record_all("first")
    marker = {"type": "lesson", "content": "label", "at": "2026-10-08T10:05:00Z", "attrs": {"project": "gamma"}}
    body, _ = _marker_request(marker, "first")
    assert (body["attrs"]["project"], body["attrs"]["project_id"]) == ("alpha", "github.com/fixture/alpha")


def test_markers_outside_recorded_scope_stay_unknown_instead_of_taking_the_latest_project(home, monkeypatch):
    monkeypatch.setattr(project_sessions, "_branch", lambda cwd: "")
    latest = ProjectIdentity("beta", "fixture/beta", "beta", "/work/beta", "", "github.com/fixture/beta")
    _record_all("first")
    record_session("first", latest)
    assert project_sessions.lookup("first").project == "beta"
    marker = {"type": "lesson", "content": "early", "at": "2026-10-08T09:59:00Z", "attrs": {"project": "beta"}}
    body, _ = _marker_request(marker, "first")
    assert body["attrs"]["attribution"] == "unknown"
    assert "project_id" not in body["attrs"]
    assert "project" not in body["attrs"]


def test_an_outbox_replay_keeps_the_attributes_computed_when_it_was_written(home):
    _record_all("first")
    written, _ = _marker_request({"type": "lesson", "content": "x", "at": "2026-10-08T10:05:00Z", "attrs": {}}, "first")
    replayed, _ = _marker_request({"type": "lesson", "content": "x", "attrs": dict(written["attrs"])}, "first")
    assert replayed == written
    assert replayed["attrs"]["worktree"] == "one"


def test_an_older_task_revision_cannot_replace_a_newer_one(home):
    _record_all("first")
    current = FIXTURE["transitions"][-1]["scope"]
    with pytest.raises(ScopeRefused):
        record_scope("first", {**current, "task_revision": "1"}, "2026-10-08T11:00:00+00:00")
    assert record_scope("first", {**current, "task_revision": "3"}, "2026-10-08T11:00:00+00:00")["sequence"] == 4
    assert record_scope("first", {**current, "task": "fourth", "task_revision": "1"}, "2026-10-08T11:01:00+00:00")


def test_switching_the_scope_path_off_restores_the_preceding_behavior(home, monkeypatch):
    monkeypatch.setattr(project_sessions, "_branch", lambda cwd: "")
    identity = ProjectIdentity("beta", "fixture/beta", "beta", "/work/beta", "", "github.com/fixture/beta")
    marker = {"type": "lesson", "content": "legacy", "at": "2026-10-08T10:05:00Z", "attrs": {}}
    monkeypatch.setenv("AGENTIHOOKS_SESSION_SCOPE", "0")
    record_session("off", identity)
    assert transitions("off") == []
    assert observe_transcript("off", str(_transcript(home))) == 0
    body, _ = _marker_request(marker, "off")
    legacy = ("project", "repo", "worktree", "cwd")
    assert [body["attrs"][key] for key in legacy] == [identity.attributes()[key] for key in legacy]
    assert "attribution" not in body["attrs"]
    _record_all("off")
    assert _marker_request(marker, "off")[0] == body
    monkeypatch.setenv("AGENTIHOOKS_SESSION_SCOPE", "1")
    assert _marker_request(marker, "off")[0]["attrs"]["project_id"] == "github.com/fixture/alpha"


def test_stop_reports_unattributed_markers(home, monkeypatch):
    from contextlib import contextmanager

    from hooks.context import brain_writer_hook

    recorded = {}

    class Span:
        def set_attrs(self, attrs):
            recorded.update(attrs)

    @contextmanager
    def span_ctx(name, attrs):
        yield Span()

    monkeypatch.setattr("hooks.config.BRAIN_WRITER_ENABLED", True)
    monkeypatch.setattr("hooks.config.BRAIN_WRITER_MAX_MARKERS", 20)
    monkeypatch.setattr("hooks.config.BRAIN_WRITER_OUTBOX", str(home / "outbox"))
    monkeypatch.setattr("hooks.telemetry.span_ctx", span_ctx)
    monkeypatch.setattr(brain_writer_hook, "_drain_outbox", lambda outbox: 0)
    monkeypatch.setattr(brain_writer_hook, "_publish_to_http", lambda markers, sid: (len(markers), []))
    monkeypatch.setattr(project_sessions, "resolve_project", lambda cwd, env=None: IDENTITIES.get(cwd))
    _record_all("stop")
    assert brain_writer_hook.write_markers("stop", str(_transcript(home)))["markers"] == 6
    assert recorded["unattributed_session_events_total"] == 1
    late = "<!-- @lesson -->Late.<!-- @/lesson -->"
    record_scope("late", FIXTURE["transitions"][0]["scope"], "2000-01-01T00:00:00+00:00")
    assert brain_writer_hook.write_markers("late", str(home / "none.jsonl"), late)["markers"] == 1
    assert recorded["unattributed_session_events_total"] == 0


def test_an_unwritable_scope_log_never_stops_session_start_or_stop(home):
    blocked = home / "state" / "brain" / "session-scopes"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("not a folder")
    identity = ProjectIdentity("alpha", "fixture/alpha", "one", "/work/alpha/one", "", "github.com/fixture/alpha")
    record_session("live", identity)
    assert project_sessions.lookup("live").project == "alpha"
    assert observe_transcript("live", str(_transcript(home))) == 0


def test_an_undecodable_scope_log_stays_readable(home):
    first = FIXTURE["transitions"][0]
    record_scope("first", first["scope"], first["at"])
    with project_sessions._scope_path("first").open("ab") as stream:
        stream.write(b"\xff\xfe\n")
    assert [row["worktree"] for row in transitions("first")] == ["one"]


def test_transcript_branch_changes_are_recorded_and_never_read_from_live_git(home, monkeypatch):
    identity = ProjectIdentity("alpha", "fixture/alpha", "one", "/work/alpha/one", "", "github.com/fixture/alpha")
    monkeypatch.setattr(project_sessions, "resolve_project", lambda cwd, env=None: identity)
    monkeypatch.setattr(project_sessions, "_branch", lambda cwd: pytest.fail("live branch read for a past entry"))
    entries = [
        {"type": "assistant", "timestamp": "2026-10-08T10:00:00Z", "cwd": "/work/alpha/one", "gitBranch": "main"},
        {"type": "assistant", "timestamp": "2026-10-08T10:05:00Z", "cwd": "/work/alpha/one", "gitBranch": "feat"},
        {"type": "turn_context", "timestamp": "2026-10-08T10:10:00Z", "payload": {"cwd": "/work/alpha/one"}},
    ]
    path = home / "branches.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    assert observe_transcript("branches", str(path), {}) == 3
    assert [row["branch"] for row in transitions("branches")] == ["main", "feat", ""]


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
