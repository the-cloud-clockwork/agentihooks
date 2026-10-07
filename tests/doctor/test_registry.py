import json
import os

import pytest

from hooks.observability import agent_trace, trace_flush
from scripts.doctor import registry

T0 = 1_791_400_000_000
AGENT = {
    "name": "engineer@1-0001",
    "lane": "eng",
    "task": "t1",
    "state": "working",
    "harness": "claude",
    "seat": "eng-1@s",
    "profile": "engineer",
    "started_at": T0,
    "conversation_id": "",
}


@pytest.fixture
def cursors(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "agent_trace")
    (tmp_path / "agent_trace").mkdir()
    return tmp_path


def _transcript(path, stamps):
    lines = [json.dumps({"type": "user", "timestamp": stamp}) + "\n" for stamp in stamps]
    path.write_text("".join(lines))
    return [len(line.encode()) for line in lines]


def _session(home, session_id, stamps, accepted_lines, **cursor):
    transcript = home / f"{session_id}.jsonl"
    sizes = _transcript(transcript, stamps)
    source = {"accepted_bytes": sum(sizes[:accepted_lines])}
    agent_trace._cursor_path(session_id).write_text(json.dumps({"version": 2, "source": source, **cursor}))
    request = {"transcript": str(transcript), "target": "claude", "at": (T0 + 3_000) * 1_000_000}
    trace_flush.request_path(session_id).write_text(json.dumps(request))
    return transcript


def test_only_working_agents_are_expected_bindings():
    agents = [AGENT, {**AGENT, "name": "engineer@1-0002", "state": "finished"}, {**AGENT, "state": "starting"}]
    assert registry.bindings(agents) == [
        {
            "agent": "engineer@1-0001",
            "life": f"engineer@1-0001#{T0}",
            "seat": "eng-1@s",
            "task": "t1",
            "harness": "claude",
            "profile": "engineer",
            "started_at": T0,
            "session_id": "",
        }
    ]


def test_progress_reads_generated_and_accepted_progress_and_the_oldest_unaccepted_event(cursors):
    stamps = ["2026-10-07T08:00:00Z", "2026-10-07T08:00:30.500Z", "2026-10-07T08:01:00Z"]
    accepted = {"a": "rev", "b": "rev", "c": "legacy"}
    _session(
        cursors,
        "sid",
        stamps,
        1,
        pending=[{}, {}],
        overflow={"bytes": 9},
        accepted=accepted,
        accepted_at="2026-10-07T08:00:10+00:00",
    )
    found = registry.progress("sid", "claude")
    size = os.path.getsize(cursors / "sid.jsonl")
    assert found == {
        "generated_bytes": size,
        "accepted_bytes": found["accepted_bytes"],
        "oldest_unaccepted": 1791360030500,
        "pending": 2,
        "overflow": 9,
        "accepted": 2,
        "accepted_at": 1791360010000,
        "exporter_alive": False,
        "requested_at": T0 + 3_000,
    }
    assert 0 < found["accepted_bytes"] < size


def test_fully_accepted_progress_has_no_unaccepted_event(cursors):
    _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1)
    found = registry.progress("sid", "claude")
    assert found["generated_bytes"] == found["accepted_bytes"]
    assert found["oldest_unaccepted"] == 0


def test_unaccepted_lines_without_a_timestamp_fall_back_to_the_transcript_time(cursors):
    transcript = _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1)
    with transcript.open("a") as out:
        out.write('{"type": "summary"}\n["not a record"]\nnot json\n')
    os.utime(transcript, (1_791_360_100, 1_791_360_100))
    assert registry.progress("sid", "claude")["oldest_unaccepted"] == 1_791_360_100_000


def test_a_live_exporter_is_reported(cursors):
    _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1)
    me = {"supervisor_pid": os.getpid(), "supervisor_start": trace_flush.start_time(os.getpid())}
    trace_flush.owner_path("sid").write_text(json.dumps(me))
    assert registry.progress("sid", "claude")["exporter_alive"] is True


def test_a_session_with_no_exporter_files_has_no_local_progress(cursors):
    assert registry.progress("nothing", "claude") is None


def test_without_a_known_transcript_the_cursor_staged_bytes_are_the_generated_progress(cursors):
    cursor = {"version": 2, "source": {"buffered_bytes": 900, "accepted_bytes": 0}, "pending": [{}]}
    path = agent_trace._cursor_path("lost")
    path.write_text(json.dumps(cursor))
    os.utime(path, (1_791_360_100, 1_791_360_100))
    found = registry.progress("lost", "claude")
    assert (found["generated_bytes"], found["accepted_bytes"]) == (900, 0)
    assert found["oldest_unaccepted"] == 1_791_360_100_000
    cursor["pending"] = [{"start_ns": 1_791_360_050_000_000_000}, {"start_ns": 1_791_360_020_000_000_000}, {}]
    path.write_text(json.dumps(cursor))
    assert registry.progress("lost", "claude")["oldest_unaccepted"] == 1_791_360_020_000


def test_a_codex_session_without_a_request_reads_its_rollout(cursors, monkeypatch):
    rollout = cursors / "rollout.jsonl"
    _transcript(rollout, ["2026-10-07T08:00:00Z"])
    monkeypatch.setattr(
        "hooks.targets.normalizer.codex_rollout_path", lambda session: str(rollout) if session == "cx" else ""
    )
    agent_trace._cursor_path("cx").write_text(json.dumps({"version": 2, "source": {"accepted_bytes": 0}}))
    found = registry.progress("cx", "codex")
    assert found["generated_bytes"] == os.path.getsize(rollout)
    assert found["oldest_unaccepted"] == 1791360000000
    assert found["requested_at"] == 0


def _trace(trace_id, session_id, life=f"engineer@1-0001#{T0}", **extra):
    attributes = {
        "agentihooks.correlation.agent.life": life,
        "agentihooks.correlation.seat": "eng-1@s",
        "agentihooks.correlation.task": "t1",
        "agentihooks.correlation.harness": "claude",
        "agentihooks.correlation.profile.resolved": "engineer",
        **extra,
    }
    return {"id": trace_id, "sessionId": session_id, "metadata": {"attributes": attributes}, "input": "text"}


class Langfuse:
    def __init__(self, traces, observations, total=40):
        self.traces, self.observations, self.total, self.calls = traces, observations, total, []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "traces":
            return {"data": self.traces, "meta": {"page": 1, "totalPages": 1}}
        if params["limit"] == 1:
            return {"data": self.observations[:1], "meta": {"totalItems": self.total}}
        since = params.get("fromStartTime")
        rows = [o for o in self.observations if not since or o["startTime"] >= since]
        return {"data": rows, "meta": {"totalItems": len(rows)}}


def _clock(*times):
    values = iter(times)
    return lambda: next(values)


def test_read_joins_the_newest_trace_and_reads_freshness_from_a_watermark(cursors):
    _session(cursors, "sid-new", ["2026-10-07T08:00:00Z"], 1)
    observations = [
        {"startTime": "2026-10-07T08:00:20.000Z", "endTime": "2026-10-07T08:00:25.000Z"},
        {"startTime": "2026-10-07T08:00:10.000Z", "endTime": "2026-10-07T08:00:40.000Z"},
    ]
    langfuse = Langfuse([_trace("tr-new", "sid-new"), _trace("tr-old", "sid-old")], observations)
    state = {}
    [found], failures = registry.read("s", [AGENT], langfuse, state, 15, 50)
    assert failures == []
    assert langfuse.calls[0] == (
        "traces",
        {
            "tags": ["swarm:s", "agent:engineer@1-0001"],
            "fields": "core,io",
            "orderBy": "timestamp.desc",
            "limit": registry.TRACES_LIMIT,
            "page": 1,
        },
    )
    assert langfuse.calls[1] == ("observations", {"traceId": "tr-new", "limit": 50, "page": 1})
    assert found["read"] is True and found["session_id"] == "sid-new"
    assert found["remote"] == {"trace": "tr-new", "fresh_ms": 1791360040000, "observations": 40}
    assert found["traces"][0] == {
        "id": "tr-new",
        "session_id": "sid-new",
        "life": f"engineer@1-0001#{T0}",
        "seat": "eng-1@s",
        "task": "t1",
        "harness": "claude",
        "profile": "engineer",
    }
    assert found["local"]["generated_bytes"] == found["local"]["accepted_bytes"]
    assert state["marks"] == {"tr-new": {"start_ms": 1791360020000, "fresh_ms": 1791360040000}}
    langfuse.observations = [{"startTime": "2026-10-07T08:01:00.000Z", "endTime": None}, *observations]
    [found], _ = registry.read("s", [AGENT], langfuse, state, 15, 50)
    assert langfuse.calls[-2][1]["fromStartTime"] == "2026-10-07T08:00:20.000Z"
    assert found["remote"]["fresh_ms"] == 1791360060000
    assert state["marks"]["tr-new"] == {"start_ms": 1791360060000, "fresh_ms": 1791360060000}


def test_a_known_session_is_joined_to_its_own_trace(cursors):
    codex = {**AGENT, "harness": "codex", "conversation_id": "sid-old"}
    langfuse = Langfuse([_trace("tr-new", "sid-new"), _trace("tr-old", "sid-old")], [])
    [found], _ = registry.read("s", [codex], langfuse, {}, 15, 50)
    assert found["session_id"] == "sid-old"
    assert found["remote"] == {"trace": "tr-old", "fresh_ms": 0, "observations": 40}


def test_a_known_session_absent_from_the_agent_traces_has_no_remote(cursors):
    codex = {**AGENT, "harness": "codex", "conversation_id": "sid-x"}
    [found], _ = registry.read("s", [codex], Langfuse([_trace("tr-new", "sid-new")], []), {}, 15, 50)
    assert found["session_id"] == "sid-x" and found["remote"] is None
    assert [t["id"] for t in found["traces"]] == ["tr-new"]


def test_an_agent_with_no_trace_is_read_with_no_remote_and_no_local(cursors):
    [found], failures = registry.read("s", [AGENT], Langfuse([], []), {}, 15, 50)
    assert (found["read"], found["traces"], found["remote"], found["local"]) == (True, [], None, None)
    assert failures == []


def test_a_failed_read_is_returned_as_a_line_and_keeps_local_progress(cursors):
    _session(cursors, "sid", ["2026-10-07T08:00:00Z", "2026-10-07T08:00:30Z"], 1, pending=[{}])

    def down(path, params):
        raise ConnectionError("refused")

    codex = {**AGENT, "conversation_id": "sid"}
    state = {"marks": {"tr-1": {"start_ms": 5, "fresh_ms": 6}}}
    [found], failures = registry.read("s", [codex], down, state, 15, 50)
    assert failures == ["active read of engineer@1-0001 failed: ConnectionError: refused"]
    assert (found["read"], found["traces"], found["remote"]) == (False, [], None)
    assert found["local"]["pending"] == 1
    assert state["marks"] == {"tr-1": {"start_ms": 5, "fresh_ms": 6}}


def test_one_slow_read_is_retried_once_inside_the_budget(cursors):
    langfuse = Langfuse([], [])
    tries = []

    def flaky(path, params):
        tries.append(path)
        if len(tries) == 1:
            raise TimeoutError("read timed out")
        return langfuse(path, params)

    [found], failures = registry.read("s", [AGENT], flaky, {}, 15, 50)
    assert failures == [] and found["read"] is True
    assert tries == ["traces", "traces"]
    assert registry.ATTEMPTS == 2


def test_the_read_budget_leaves_later_bindings_unread_and_says_so(cursors):
    second = {**AGENT, "name": "engineer@1-0002"}
    langfuse = Langfuse([], [])
    found, failures = registry.read("s", [AGENT, second], langfuse, {}, 15, 50, clock=_clock(0, 1, 16))
    assert [b["read"] for b in found] == [True, False]
    assert failures == ["active read budget of 15 seconds spent before engineer@1-0002"]
    assert len(langfuse.calls) == 1


def test_a_full_read_drops_watermarks_of_traces_no_longer_active(cursors):
    state = {"marks": {"gone": {"start_ms": 1, "fresh_ms": 1}}}
    registry.read("s", [AGENT], Langfuse([_trace("tr-1", "sid")], []), state, 15, 50)
    assert state["marks"] == {"tr-1": {"start_ms": 0, "fresh_ms": 0}}


def test_state_round_trips_and_a_missing_or_broken_file_reads_empty(tmp_path):
    path = tmp_path / "s" / "doctor-telemetry.json"
    assert registry.load(path) == {}
    registry.save(path, {"marks": {}, "down_since": 7})
    assert registry.load(path) == {"marks": {}, "down_since": 7}
    path.write_text("[1]")
    assert registry.load(path) == {}


def test_state_saves_into_missing_parents_and_over_itself(tmp_path):
    path = tmp_path / "a" / "b" / "state.json"
    registry.save(path, {"marks": {}})
    registry.save(path, {"marks": {"t": {}}})
    assert registry.load(path) == {"marks": {"t": {}}}


def test_times_that_do_not_parse_are_zero():
    assert registry._ms("not a time") == 0
    assert registry._ms(None) == 0
    assert registry._ms("2026-10-07T08:00:00Z") == 1791360000000


def test_safe_names_keep_word_characters_and_dashes_only():
    assert registry.safe_name("Ab-9_z/..:x y") == "Ab-9_z____x_y"


def test_an_agent_record_missing_optional_fields_binds_with_empty_values():
    [found] = registry.bindings([{"name": "engineer@1-0003", "state": "working"}])
    assert found == {
        "agent": "engineer@1-0003",
        "life": "engineer@1-0003#0",
        "seat": "",
        "task": "",
        "harness": "",
        "profile": "",
        "started_at": 0,
        "session_id": "",
    }


def test_the_oldest_unaccepted_event_skips_undecodable_and_unparsable_records(cursors):
    transcript = _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1)
    with transcript.open("ab") as out:
        out.write(b'{"type": "user", "text": "\xff\xfe"}\n')
        out.write(b'{"type": "user", "timestamp": "yesterday"}\n')
        out.write(b'{"type": "user", "timestamp": "2026-10-07T08:00:20Z"}\n')
    assert registry.progress("sid", "claude")["oldest_unaccepted"] == 1791360020000


def test_the_oldest_unaccepted_event_is_read_only_inside_its_window(cursors, monkeypatch):
    transcript = _session(cursors, "sid", ["2026-10-07T08:00:00Z", "2026-10-07T08:00:20Z"], 1)
    os.utime(transcript, (1_791_360_100, 1_791_360_100))
    monkeypatch.setattr(registry, "WINDOW_BYTES", 10)
    assert registry.progress("sid", "claude")["oldest_unaccepted"] == 1_791_360_100_000


def test_a_cursor_with_nothing_staged_has_no_generated_progress(cursors):
    agent_trace._cursor_path("empty").write_text(json.dumps({"version": 2}))
    found = registry.progress("empty", "claude")
    assert found == {
        "generated_bytes": 0,
        "accepted_bytes": 0,
        "oldest_unaccepted": 0,
        "pending": 0,
        "overflow": 0,
        "accepted": 0,
        "accepted_at": 0,
        "exporter_alive": False,
        "requested_at": 0,
    }


def test_sub_millisecond_times_round_down(cursors):
    _session(cursors, "sid", ["2026-10-07T08:00:00Z"], 1)
    request = json.loads(trace_flush.request_path("sid").read_text())
    trace_flush.request_path("sid").write_text(json.dumps({**request, "at": (T0 + 3_000) * 1_000_000 + 500_000}))
    assert registry.progress("sid", "claude")["requested_at"] == T0 + 3_000
    cursor = {"version": 2, "source": {"buffered_bytes": 9}, "pending": [{"start_ns": 1_791_360_020_000_500_000}]}
    agent_trace._cursor_path("lost").write_text(json.dumps(cursor))
    assert registry.progress("lost", "claude")["oldest_unaccepted"] == 1_791_360_020_000


def test_a_trace_row_missing_fields_reads_as_empty(cursors):
    row = {"id": "tr", "metadata": {"attributes": {"agentihooks.correlation.task": "t1"}}}
    assert registry._remote_trace(row) == {
        "id": "tr",
        "session_id": "",
        "life": "",
        "seat": "",
        "task": "t1",
        "harness": "",
        "profile": "",
    }


def test_a_scan_with_no_new_observations_keeps_its_watermark_and_counts_a_missing_total_as_zero(cursors):
    observations = [{"startTime": "2026-10-07T08:00:20.000Z", "endTime": "2026-10-07T08:00:25.000Z"}]
    langfuse = Langfuse([_trace("tr", "sid")], observations)
    state = {}
    registry.read("s", [AGENT], langfuse, state, 15, 50)
    assert langfuse.calls[2] == ("observations", {"traceId": "tr", "limit": 1, "page": 1})
    langfuse.observations = []
    langfuse.total = None
    [found], _ = registry.read("s", [AGENT], langfuse, state, 15, 50)
    assert state["marks"]["tr"] == {"start_ms": 1791360020000, "fresh_ms": 1791360025000}
    assert found["remote"] == {"trace": "tr", "fresh_ms": 1791360025000, "observations": 0}


def test_every_failed_binding_is_reported(cursors):
    def down(path, params):
        raise ConnectionError("refused")

    second = {**AGENT, "name": "engineer@1-0002"}
    _, failures = registry.read("s", [AGENT, second], down, {}, 15, 50)
    assert failures == [
        "active read of engineer@1-0001 failed: ConnectionError: refused",
        "active read of engineer@1-0002 failed: ConnectionError: refused",
    ]


def test_a_budget_spent_exactly_leaves_the_binding_unread(cursors):
    second = {**AGENT, "name": "engineer@1-0002"}
    found, failures = registry.read("s", [AGENT, second], Langfuse([], []), {}, 15, 50, clock=_clock(0, 1, 15))
    assert [b["read"] for b in found] == [True, False]


def test_a_codex_binding_reads_its_rollout_through_its_harness(cursors, monkeypatch):
    rollout = cursors / "rollout.jsonl"
    _transcript(rollout, ["2026-10-07T08:00:00Z"])
    monkeypatch.setattr(
        "hooks.targets.normalizer.codex_rollout_path", lambda session: str(rollout) if session == "cx" else ""
    )
    agent_trace._cursor_path("cx").write_text(json.dumps({"version": 2, "source": {"accepted_bytes": 0}}))
    codex = {**AGENT, "harness": "codex", "conversation_id": "cx"}
    [found], _ = registry.read("s", [codex], Langfuse([], []), {}, 15, 50)
    assert found["local"]["generated_bytes"] == os.path.getsize(rollout)


def test_unreadable_exporter_progress_is_a_failure_line_not_a_crash(cursors):
    agent_trace._cursor_path("bad").write_text(json.dumps({"version": 2, "source": {"accepted_bytes": "x"}}))
    bad = {**AGENT, "conversation_id": "bad"}
    second = {**AGENT, "name": "engineer@1-0002"}
    found, failures = registry.read("s", [bad, second], Langfuse([], []), {}, 15, 50)
    assert [b["local"] for b in found] == [None, None]
    assert failures == [
        "exporter progress of engineer@1-0001 unreadable: ValueError: invalid literal for int() with base 10: 'x'"
    ]
