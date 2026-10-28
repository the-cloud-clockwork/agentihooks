import json

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult

from hooks.observability import agent_trace, otel, transcript

CAP = 24_000


class Receiver:
    def __init__(self):
        self.observations = {}
        self.calls = 0

    def export(self, spans):
        self.calls += 1
        self.observations.update({span.context.span_id: span for span in spans})
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


def _stamp(second: int) -> str:
    return f"2026-10-07T{10 + second // 3600:02d}:{second // 60 % 60:02d}:{second % 60:02d}Z"


def _claude(turns: int, calls: int) -> list[dict]:
    records, clock = [], 0
    for turn in range(turns):
        clock += 1
        records.append(
            {"type": "user", "uuid": f"p{turn}", "timestamp": _stamp(clock), "message": {"content": f"ask {turn}"}}
        )
        for call in range(calls):
            name = f"{turn}-{call}"
            usage = {"input_tokens": 10 + call, "output_tokens": 3}
            for block in (
                {"type": "text", "text": f"step {name}"},
                {"type": "tool_use", "id": f"t{name}", "name": "shell", "input": {"cmd": name}},
            ):
                clock += 1
                message = {"id": f"m{name}", "model": f"model-{turn % 2}", "usage": usage, "content": [block]}
                records.append(
                    {
                        "type": "assistant",
                        "uuid": f"a{name}-{block['type']}",
                        "timestamp": _stamp(clock),
                        "message": message,
                    }
                )
            clock += 1
            result = {"type": "tool_result", "tool_use_id": f"t{name}", "content": f"{name} " + "x" * 3000}
            records.append(
                {"type": "user", "uuid": f"r{name}", "timestamp": _stamp(clock), "message": {"content": [result]}}
            )
        clock += 1
        content = [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": f"done {turn}"}]
        message = {"id": f"m{turn}-end", "model": "model-0", "content": content}
        records.append({"type": "assistant", "uuid": f"e{turn}", "timestamp": _stamp(clock), "message": message})
    return records


def _codex(turns: int, calls: int) -> list[dict]:
    records = [{"type": "session_meta", "timestamp": _stamp(0), "payload": {"id": "session"}}]
    clock, total = 0, 0
    for turn in range(turns):
        clock += 1
        records.append({"type": "turn_context", "timestamp": _stamp(clock), "payload": {"model": f"model-{turn % 2}"}})
        user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"ask {turn}"}]}
        records.append({"type": "response_item", "timestamp": _stamp(clock), "payload": user})
        records.append(
            {"type": "response_item", "timestamp": _stamp(clock), "payload": {"type": "reasoning", "id": f"z{turn}"}}
        )
        for call in range(calls):
            name = f"{turn}-{call}"
            clock += 1
            arguments = json.dumps({"cmd": name})
            call_item = {
                "type": "function_call",
                "id": f"g{name}",
                "call_id": f"t{name}",
                "name": "shell",
                "arguments": arguments,
            }
            records.append({"type": "response_item", "timestamp": _stamp(clock), "payload": call_item})
            total += 13
            usage = {
                "last_token_usage": {"input_tokens": 10 + call, "output_tokens": 3},
                "total_token_usage": {"t": total},
            }
            records.append(
                {"type": "event_msg", "timestamp": _stamp(clock), "payload": {"type": "token_count", "info": usage}}
            )
            clock += 1
            output = {"type": "function_call_output", "call_id": f"t{name}", "output": f"{name} " + "x" * 3000}
            records.append({"type": "response_item", "timestamp": _stamp(clock), "payload": output})
        clock += 1
        reply = {
            "type": "message",
            "role": "assistant",
            "id": f"m{turn}-end",
            "content": [{"type": "output_text", "text": f"done {turn}"}],
        }
        records.append({"type": "response_item", "timestamp": _stamp(clock), "payload": reply})
    return records


def _noisy(records: list) -> list:
    middle = len(records) // 2
    return [*records[:2], "not json", *records[2:middle], "[1]", *records[middle:]]


SESSIONS = {
    "claude-many-turns": lambda: _noisy(_claude(12, 5)),
    "claude-one-long-turn": lambda: _noisy(_claude(1, 60)),
    "codex-many-turns": lambda: _noisy(_codex(12, 5)),
    "codex-one-long-turn": lambda: _noisy(_codex(1, 60)),
}


def _line(record) -> str:
    return (record if isinstance(record, str) else json.dumps({**record, "note": "é"}, ensure_ascii=False)) + "\n"


def _observed(receiver: Receiver) -> dict:
    return {
        span_id: (
            span.name,
            span.parent.span_id if span.parent else None,
            dict(span.attributes),
            span.start_time,
            span.end_time,
        )
        for span_id, span in receiver.observations.items()
    }


def _run(tmp_path, monkeypatch, name: str, cap: int, records: list[dict], chunk: int):
    receiver = Receiver()
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / name / "cursor")
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", cap)
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: receiver)
    path = tmp_path / name / "transcript.jsonl"
    path.parent.mkdir()
    path.write_text("")
    cursor_sizes = []

    def flush():
        agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
        cursor_sizes.append(agent_trace._cursor_path("session").stat().st_size)

    for start in range(0, len(records), chunk):
        with path.open("a") as handle:
            handle.write("".join(_line(record) for record in records[start : start + chunk]))
        flush()
    for _ in range(len(records)):
        state = agent_trace._cursor("session")
        if (
            "overflow" not in state
            and not state["pending"]
            and state["source"]["accepted_bytes"] == path.stat().st_size
        ):
            break
        flush()
    return receiver, cursor_sizes, path


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(agent_trace, "_root_attributes", lambda session: {})
    monkeypatch.setattr(agent_trace, "_collector_outcomes", lambda *args: None)
    monkeypatch.setattr("hooks.context.context_usage.session_cost", lambda session: None)


def test_one_long_streamed_generation_keeps_paging_bounded(tmp_path, monkeypatch, quiet):
    monkeypatch.setattr("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 500)
    records = _claude(1, 80)
    for record in records:
        if record["type"] == "assistant" and not record["uuid"].startswith("e") and record["message"]["id"] != "m0-0":
            record["message"]["id"] = "streamed-generation"
    reference, _, _ = _run(tmp_path, monkeypatch, "reference", 10**9, records, len(records))
    paged, sizes, path = _run(tmp_path, monkeypatch, "paged", 16000, records, 3)
    state = agent_trace._cursor("session")
    assert max(sizes) < 4 * 16000
    assert _observed(paged) == _observed(reference)
    assert len(state["accepted"]) + state["paged"]["spans"] == len(paged.observations)
    assert state["source"]["accepted_bytes"] == path.stat().st_size
    generation_id = agent_trace._span_id("session", "streamed-generation")
    assert paged.observations[generation_id].start_time == reference.observations[generation_id].start_time


def test_repeated_streamed_generations_page_without_lost_or_doubled_observations(tmp_path, monkeypatch, quiet):
    monkeypatch.setattr("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 500)
    records = _claude(40, 3)
    for record in records:
        if record["type"] == "assistant" and not record["uuid"].startswith("e"):
            record["message"]["id"] = record["message"]["id"].split("-")[0]
    reference, _, _ = _run(tmp_path, monkeypatch, "reference", 10**9, records, len(records))
    paged, sizes, path = _run(tmp_path, monkeypatch, "paged", CAP, records, 3)
    state = agent_trace._cursor("session")
    assert _observed(paged) == _observed(reference)
    assert len(state["accepted"]) + state["paged"]["spans"] == len(paged.observations)
    assert state["source"]["accepted_bytes"] == path.stat().st_size
    assert max(sizes) < 4 * CAP
    assert len(state["records"]) < len(records) / 4


def test_repeated_metadata_records_resume_paging_past_the_paged_records(tmp_path, monkeypatch, quiet):
    metadata = [{"type": "custom-title", "customTitle": "rig"}, {"type": "agent-name", "agentName": "master"}]

    def call(uuid: str, message: str, tool: str, second: int) -> dict:
        block = {"type": "tool_use", "id": tool, "name": "shell", "input": {"cmd": tool}}
        content = {"id": message, "model": "model-0", "content": [block]}
        return {"type": "assistant", "uuid": uuid, "timestamp": _stamp(second), "message": content}

    def user(uuid: str, content, second: int) -> dict:
        return {"type": "user", "uuid": uuid, "timestamp": _stamp(second), "message": {"content": content}}

    result = [{"type": "tool_result", "tool_use_id": "t2", "content": "two"}]
    first = [
        *metadata,
        user("p0", "ask 0", 1),
        call("a1", "m1", "t1", 2),
        *metadata,
        call("a2", "m2", "t2", 3),
        user("r2", result, 4),
    ]
    later = [*metadata, user("p1", "ask 1", 5)]
    receiver, _, path = _run(tmp_path, monkeypatch, "paged", CAP, first, len(first))
    assert agent_trace._cursor("session")["paged"]["boundary"]
    with path.open("a") as handle:
        handle.write("".join(_line(record) for record in later))
    for _ in range(3):
        agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    state = agent_trace._cursor("session")
    assert len(state["accepted"]) + state["paged"]["spans"] == len(receiver.observations)
    assert state["source"]["records"] == len(first) + len(later)
    assert set(state["records"]) == {"p1"}
    assert set(state["source_points"]) <= set(state["records"])


def test_paged_point_resumes_past_the_dropped_record_read_last():
    state = {"source": {"device": 1, "inode": 2}, "source_points": {"a": [30, 40, 4, 1], "b": [10, 20, 2, 0]}}
    earlier = {"device": 1, "inode": 2, "line": 0, "offset": 5, "records": 1, "unsupported": 0, "boundary": "z"}
    point = {"device": 1, "inode": 2, "line": 30, "offset": 40, "records": 4, "unsupported": 1, "boundary": "a"}
    assert agent_trace._paged_point(state, ["a", "b"], [0, 1], earlier) == ("a", point)


@pytest.mark.parametrize("offset", [20, 40])
def test_paged_point_never_moves_behind_the_earlier_paged_point(offset):
    state = {"source": {"device": 1, "inode": 2}, "source_points": {"a": [0, 10, 1, 0], "b": [10, 20, 2, 0]}}
    earlier = {"device": 1, "inode": 2, "line": 0, "offset": offset, "records": 4, "unsupported": 0, "boundary": "d"}
    assert agent_trace._paged_point(state, ["a", "b", "c"], [0, 1], earlier) == ("d", earlier)


def test_paged_point_without_every_position_keeps_the_earlier_point():
    state = {"source": {}, "source_points": {"a": [0, 10, 1, 0]}}
    assert agent_trace._paged_point(state, ["a", "b"], [0, 1], None) == ("b", None)


def _call(uuid: str, message: str, block: dict, second: int) -> dict:
    content = {"id": message, "model": "model-0", "content": [block]}
    return {"type": "assistant", "uuid": uuid, "timestamp": _stamp(second), "message": content}


def _paged_session(tmp_path, monkeypatch):
    metadata = [{"type": "custom-title", "customTitle": "rig"}, {"type": "agent-name", "agentName": "master"}]

    def tool(name: str) -> dict:
        return {"type": "tool_use", "id": name, "name": "shell", "input": {"cmd": name}}

    def user(uuid: str, content, second: int) -> dict:
        return {"type": "user", "uuid": uuid, "timestamp": _stamp(second), "message": {"content": content}}

    result = [{"type": "tool_result", "tool_use_id": "t2", "content": "two"}]
    first = [
        *metadata,
        user("p0", "ask 0", 1),
        _call("a1", "m1", tool("t1"), 2),
        *metadata,
        _call("a2", "m2", tool("t2"), 3),
        user("r2", result, 4),
    ]
    records = [*first, *metadata, user("p1", "ask 1", 5)]
    receiver, _, path = _run(tmp_path, monkeypatch, "paged", CAP, first, len(first))
    with path.open("a") as handle:
        handle.write("".join(_line(record) for record in records[len(first) :]))
    for _ in range(3):
        agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    assert set(agent_trace._cursor("session")["records"]) == {"p1"}
    return receiver, path, records


def _assert_no_recount(receiver: Receiver, path, dropped: str) -> None:
    for _ in range(3):
        agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    state = agent_trace._cursor("session")
    assert len(state["accepted"]) + state["paged"]["spans"] == len(receiver.observations)
    assert set(state["records"]) == {"p1"}
    exported = json.dumps([dict(span.attributes or {}) for span in receiver.observations.values()], default=str)
    assert dropped not in exported


@pytest.mark.parametrize("rewrite", ["without-boundary", "truncated", "revised"])
def test_transcript_rewritten_in_place_never_recounts_paged_spans(tmp_path, monkeypatch, quiet, rewrite):
    receiver, path, records = _paged_session(tmp_path, monkeypatch)
    boundary = agent_trace._cursor("session")["paged"]["boundary"]
    revised = _call("a1", "m1", {"type": "text", "text": "revised text"}, 2)
    if rewrite == "truncated":
        kept = records[:4]
    elif rewrite == "revised":
        kept = [revised if transcript.record_id(record) == "a1" else record for record in records]
    else:
        kept = [record for record in records if transcript.record_id(record) != boundary]
    inode = path.stat().st_ino
    path.write_text("".join(_line(record) for record in kept))
    assert path.stat().st_ino == inode
    _assert_no_recount(receiver, path, "revised text")


@pytest.mark.parametrize("masked", [False, True])
def test_late_record_for_a_paged_message_never_recounts_its_span(tmp_path, monkeypatch, quiet, masked):
    if masked:
        original = transcript.mask_value
        monkeypatch.setattr(transcript, "mask_value", lambda value: "M1" if value == "m1" else original(value))
    receiver, path, _ = _paged_session(tmp_path, monkeypatch)
    late = _call("a1-late", "m1", {"type": "text", "text": "late text"}, 6)
    with path.open("a") as handle:
        handle.write(_line(late))
    _assert_no_recount(receiver, path, "late text")


def test_message_split_across_the_paging_cut_stays_open_for_a_late_record():
    values = [
        _call("a1", "m1", {"type": "text", "text": "one"}, 1),
        _call("a2", "m1", {"type": "text", "text": "two"}, 2),
    ]
    values.append(_call("a3", "m3", {"type": "text", "text": "three"}, 3))
    paged = agent_trace._paged_ids({}, ["a1", "a2", "a3"], values, [0, 2], [1])
    assert paged["messages"] == [agent_trace._paged_digest("m3")]
    assert paged["keys"] == sorted(agent_trace._paged_digest(key) for key in ("a1", "a3"))
    assert not agent_trace._paged_out(paged, "a4", _call("a4", "m1", {"type": "text", "text": "late"}, 4))
    later = agent_trace._paged_ids(paged, ["a2", "a5"], [values[1], _call("a5", "m5", {}, 5)], [0, 1], [])
    assert later["messages"] == sorted(agent_trace._paged_digest(message) for message in ("m1", "m3", "m5"))


def test_paged_digest_stays_readable_from_state_written_by_earlier_releases():
    assert agent_trace._paged_digest("m3") == "32c2aea8c2e77450"


def _bytes_read_per_pass(monkeypatch, path) -> list[int]:
    original, passes = transcript.iter_complete_records, []

    def counting(transcript_path, start):
        end = start
        try:
            for record, end in original(transcript_path, start):
                yield record, end
        finally:
            passes[-1] += end - start

    monkeypatch.setattr(transcript, "iter_complete_records", counting)
    for _ in range(3):
        passes.append(0)
        agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    return passes


def test_lost_paged_boundary_moves_the_paged_point_past_the_paged_records(tmp_path, monkeypatch, quiet):
    receiver, path, records = _paged_session(tmp_path, monkeypatch)
    boundary = agent_trace._cursor("session")["paged"]["boundary"]
    path.write_text("".join(_line(record) for record in records if transcript.record_id(record) != boundary))
    agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    unpaged = len(_line(records[-1]).encode())
    assert _bytes_read_per_pass(monkeypatch, path) == [unpaged] * 3
    _assert_no_recount(receiver, path, "never exported")


def test_paged_point_holds_past_the_boundary_between_passes(tmp_path, monkeypatch, quiet):
    _, path, _ = _paged_session(tmp_path, monkeypatch)
    offset = agent_trace._cursor("session")["paged"]["source"]["offset"]
    assert offset > 0
    assert _bytes_read_per_pass(monkeypatch, path) == [path.stat().st_size - offset] * 3


def test_lost_boundary_moves_the_point_only_over_paged_records_before_a_staged_one(tmp_path):
    def text(uuid: str, message: str, second: int) -> dict:
        return _call(uuid, message, {"type": "text", "text": uuid}, second)

    lines = ["not json", text("a1", "m1", 1), text("a2", "m2", 2), text("a3", "m3", 3), text("a4", "m4", 4)]
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(_line(record) for record in lines))
    digest = agent_trace._paged_digest
    paged = {
        "boundary": "gone",
        "keys": [digest("a1"), digest("a4")],
        "messages": [digest(transcript.mask_value("m2"))],
    }
    start = {"device": 1, "inode": 2, "offset": 0, "records": 0, "unsupported": 0}
    moved = agent_trace._scan_to(str(path), start, paged, {"a3": {}})
    line, end = (len("".join(_line(record) for record in lines[:count]).encode()) for count in (2, 3))
    assert moved == {
        **start,
        "line": line,
        "offset": end,
        "records": 2,
        "unsupported": 1,
        "record": "a2",
        "boundary": "gone",
    }
    assert agent_trace._scan_to(str(path), start, {"boundary": "gone"}, {}) == {**start, "boundary": "gone"}


@pytest.mark.parametrize("edited", ["codex-user-message", "claude-metadata"])
def test_edited_record_without_native_id_never_changes_span_counts(tmp_path, monkeypatch, quiet, edited):
    if edited == "codex-user-message":
        records = _codex(6, 3)
        first = next(index for index, record in enumerate(records) if record["type"] == "response_item")
        revised = json.loads(json.dumps(records[first]))
        revised["payload"]["content"][0]["text"] = "edited ask"
    else:
        records = _claude(6, 3)
        records.insert(1, {"type": "custom-title", "customTitle": "rig"})
        first, revised = 1, {"type": "custom-title", "customTitle": "edited ask"}
    receiver, _, path = _run(tmp_path, monkeypatch, "paged", CAP, records, 3)
    state = agent_trace._cursor("session")
    assert state["paged"]["boundary"]
    spans = len(state["accepted"]) + state["paged"]["spans"]
    assert spans == len(receiver.observations)
    rewritten = [revised if index == first else record for index, record in enumerate(records)]
    lost = [record for record in rewritten if transcript.record_id(record) != state["paged"]["boundary"]]
    path.write_text("".join(_line(record) for record in lost))
    for _ in range(3):
        agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    state = agent_trace._cursor("session")
    assert len(state["accepted"]) + state["paged"]["spans"] == len(receiver.observations) == spans


@pytest.mark.parametrize("chunk", [1, 3])
def test_streamed_generation_keeps_its_accepted_identity_after_tool_results(tmp_path, monkeypatch, quiet, chunk):
    records = _claude(1, 2)
    records = [records[index] for index in (0, 2, 3, 5, 6)]
    records[3]["message"]["id"] = records[1]["message"]["id"]
    receiver, _, _ = _run(tmp_path, monkeypatch, "streamed", CAP, records, chunk)
    state = agent_trace._cursor("session")
    assert len(receiver.observations) == 5
    assert len(state["accepted"]) + state.get("paged", {}).get("spans", 0) == 5
    generation = receiver.observations[agent_trace._span_id("session", records[1]["message"]["id"])]
    assert generation.attributes["gen_ai.usage.input_tokens"] == 11


@pytest.mark.parametrize("session", sorted(SESSIONS))
def test_export_continues_past_the_pending_cap_without_lost_or_doubled_observations(
    session, tmp_path, monkeypatch, quiet
):
    monkeypatch.setattr("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 500)
    records = SESSIONS[session]()
    reference, _, _ = _run(tmp_path, monkeypatch, "reference", 10**9, records, len(records))
    capped, cursor_sizes, path = _run(tmp_path, monkeypatch, "capped", CAP, records, 9)
    assert path.stat().st_size > 8 * CAP
    assert _observed(capped) == _observed(reference)
    state = agent_trace._cursor("session")
    assert "overflow" not in state and not state["pending"]
    assert state["source"]["accepted_bytes"] == path.stat().st_size
    assert state["source"]["records"] == len(records) - 2 and state["unsupported_records"] == 2
    assert max(cursor_sizes) < 4 * CAP
    assert len(state["records"]) < len(records) / 4
    assert state["paged"]["boundary"] not in state["records"]
    root = next(span for span in capped.observations.values() if span.parent is None)
    assert root.attributes["agentihooks.export.truncated_fields"] > 0
    assert root.attributes["agentihooks.export.unsupported_io"] > 0
    assert _scans(path) == 0


def _scans(path) -> int:
    calls = []
    scan = agent_trace._scan_to

    agent_trace.export_session("session", str(path), agent_trace.Identity("session"))

    def counted(*args):
        calls.append(args)
        return scan(*args)

    agent_trace._scan_to = counted
    try:
        agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    finally:
        agent_trace._scan_to = scan
    return len(calls)


def test_paging_alone_exports_nothing(tmp_path, monkeypatch, quiet):
    records = _claude(4, 3)
    receiver, _, path = _run(tmp_path, monkeypatch, "capped", CAP, records, len(records))
    calls = receiver.calls
    agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    assert receiver.calls == calls
    assert agent_trace._cursor("session")["paged"]["turns"] == 3


def test_rewritten_full_source_replays_no_paged_history(tmp_path, monkeypatch, quiet):
    records = _claude(4, 3)
    receiver, _, path = _run(tmp_path, monkeypatch, "capped", CAP, records, len(records))
    before = _observed(receiver)
    calls = receiver.calls
    path.rename(path.with_suffix(".old"))
    path.write_text("".join(_line(record) for record in records))
    agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    assert receiver.calls == calls
    assert _observed(receiver) == before


def test_open_tool_call_keeps_its_records_until_the_result_lands(tmp_path, monkeypatch, quiet):
    records = _claude(1, 2)
    late = next(index for index, record in enumerate(records) if record["uuid"] == "r0-1")
    pending_result = records.pop(late)
    receiver, _, path = _run(tmp_path, monkeypatch, "capped", CAP, records, len(records))
    assert "a0-1-tool_use" in agent_trace._cursor("session")["records"]
    with path.open("a") as handle:
        handle.write(json.dumps(pending_result) + "\n")
    agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    tool = receiver.observations[agent_trace._span_id("session", "t0-1")]
    assert tool.attributes["langfuse.observation.output"].startswith("0-1 x")
    assert "a0-1-tool_use" not in agent_trace._cursor("session")["records"]


def _without_results(records: list[dict], calls: set[str]) -> list[dict]:
    def answers(record: dict) -> bool:
        payload = record.get("payload") or {}
        if payload.get("type") == "function_call_output":
            return payload.get("call_id") in calls
        content = (record.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        return any(block.get("type") == "tool_result" and block.get("tool_use_id") in calls for block in blocks)

    return [record for record in records if not answers(record)]


@pytest.mark.parametrize("build", [_claude, _codex])
def test_call_without_a_result_closes_when_the_next_turn_arrives(build, tmp_path, monkeypatch, quiet):
    records = _without_results(build(2, 2), {"t0-1"})
    receiver, _, _ = _run(tmp_path, monkeypatch, "capped", CAP, records, len(records))
    tool = receiver.observations[agent_trace._span_id("session", "t0-1")]
    assert tool.attributes["tool.outcome.state"] == "missing"
    assert agent_trace._cursor("session").get("paged", {}).get("turns") == 1


@pytest.mark.parametrize("build", [_claude, _codex])
def test_calls_without_results_keep_stored_state_bounded(build, tmp_path, monkeypatch, quiet):
    turns = 40
    records = _without_results(build(turns, 3), {f"t{turn}-2" for turn in range(turns)})
    receiver, cursor_sizes, path = _run(tmp_path, monkeypatch, "capped", CAP, records, 9)
    state = agent_trace._cursor("session")
    assert path.stat().st_size > 8 * CAP
    assert "overflow" not in state and not state["pending"]
    assert state["source"]["accepted_bytes"] == path.stat().st_size
    assert max(cursor_sizes) < 4 * CAP
    assert len(state["records"]) < len(records) / 4
    assert state["paged"]["turns"] == turns - 1
    missing = [receiver.observations[agent_trace._span_id("session", f"t{turn}-2")] for turn in range(turns)]
    assert {span.attributes["tool.outcome.state"] for span in missing} == {"missing"}


def test_metadata_wider_than_the_window_does_not_stall_export(tmp_path, monkeypatch, quiet):
    metadata = [{"type": "file-history-snapshot", "uuid": f"s{index}", "snapshot": "y" * 4000} for index in range(12)]
    records = metadata + _claude(2, 2)
    receiver, _, path = _run(tmp_path, monkeypatch, "capped", CAP, records, len(records))
    state = agent_trace._cursor("session")
    assert "overflow" not in state and state["source"]["accepted_bytes"] == path.stat().st_size
    assert agent_trace._span_id("session", "t1-1") in receiver.observations


def test_source_rewritten_in_place_is_read_from_its_start(tmp_path, monkeypatch, quiet):
    records = _claude(4, 3)
    receiver, _, path = _run(tmp_path, monkeypatch, "capped", CAP, records, len(records))
    assert agent_trace._cursor("session")["paged"]["source"]["offset"] > 0
    inode = path.stat().st_ino
    later = {"type": "user", "uuid": "p-late", "timestamp": _stamp(9000), "message": {"content": "z" * 60000}}
    path.write_text(json.dumps(later) + "\n")
    assert path.stat().st_ino == inode
    agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    turn = receiver.observations[agent_trace._span_id("session", "p-late")]
    assert turn.name == "turn 5"
    assert agent_trace._cursor("session")["paged"]["source"]["offset"] == 0
    assert _scans(path) == 0


@pytest.mark.parametrize("cut", [1, 0])
def test_resume_point_holds_only_a_whole_boundary_line(cut, tmp_path, monkeypatch, quiet):
    records = _claude(4, 3)
    _, _, path = _run(tmp_path, monkeypatch, "capped", CAP, records, len(records))
    agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    paged = agent_trace._cursor("session")["paged"]
    point = paged["source"]
    assert point["boundary"] == paged["boundary"]
    data = path.read_bytes()
    if cut:
        path.write_bytes(data[: point["offset"] - 1])
    else:
        path.write_bytes(b"#" * (point["offset"] - 1) + b"\n" + data[point["offset"] :])
    agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    moved = agent_trace._cursor("session")["paged"]["source"]
    assert moved["offset"] < point["offset"] and moved.get("record") != paged["boundary"]
    assert agent_trace._still_at(str(path), moved)


def test_kept_records_needs_both_the_cut_entry_and_its_prompt():
    records = [
        {"type": "turn_context", "payload": {"model": "a"}},
        {"type": "turn_context", "payload": {"model": "b"}},
        {"uuid": "p"},
        {"uuid": "x"},
        {"uuid": "y"},
    ]
    turn = [{"uuid": "p"}, {"uuid": "x"}, {"uuid": "y"}]
    assert agent_trace._kept_records(records, [turn], (0, 2)) == [1, 2, 4]
    assert agent_trace._kept_records(records, [turn], (0, 0)) == [1, 2, 3, 4]
    assert agent_trace._kept_records(records, [[{"uuid": "missing"}, *turn[1:]]], (0, 2)) is None
    assert agent_trace._kept_records(records, [[turn[0], {"uuid": "missing"}]], (0, 1)) is None


def test_session_without_a_model_reports_an_empty_model():
    entries = [{"type": "user", "uuid": "p", "timestamp": _stamp(1), "message": {"content": "ask"}}]
    root = agent_trace.session_spans(entries, agent_trace.Identity("session"))[0]
    assert root.attributes["gen_ai.request.model"] == ""


def test_complete_records_of_an_empty_source(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert transcript.complete_records(str(path)) == ([], 0, 0)
    path.write_text('{"a": 1}\nnot json\n[2]\n{"b"')
    assert transcript.complete_records(str(path)) == ([{"a": 1}], len('{"a": 1}\nnot json\n[2]\n'), 2)


def test_staging_beside_waiting_records_reports_the_exact_pending_size(tmp_path, monkeypatch, quiet):
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    records = _claude(1, 1)
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(_line(record) for record in records[:2]))
    state = agent_trace._progress("session")
    agent_trace._stage_source(state, str(path))
    state["pending"] = [{"name": "unsent"}]
    with path.open("a") as handle:
        handle.write(_line(records[2]))
    probe = json.loads(json.dumps(state))
    agent_trace._stage_source(probe, str(path))
    expected = agent_trace._pending_bytes(probe, probe["records"], probe["pending"])
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", expected - 1)
    agent_trace._stage_source(state, str(path))
    assert state["overflow"] == {"bytes": expected, "limit": expected - 1}
    assert len(state["records"]) == 2
