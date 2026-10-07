import json

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult

from hooks.observability import agent_trace, otel

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
        message = {"id": f"m{turn}-end", "model": "model-0", "content": [{"type": "text", "text": f"done {turn}"}]}
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


SESSIONS = {
    "claude-many-turns": lambda: _claude(12, 5),
    "claude-one-long-turn": lambda: _claude(1, 60),
    "codex-many-turns": lambda: _codex(12, 5),
    "codex-one-long-turn": lambda: _codex(1, 60),
}


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
            handle.write("".join(json.dumps(record) + "\n" for record in records[start : start + chunk]))
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


@pytest.mark.parametrize("session", sorted(SESSIONS))
def test_export_continues_past_the_pending_cap_without_lost_or_doubled_observations(
    session, tmp_path, monkeypatch, quiet
):
    records = SESSIONS[session]()
    reference, _, _ = _run(tmp_path, monkeypatch, "reference", 10**9, records, len(records))
    capped, cursor_sizes, path = _run(tmp_path, monkeypatch, "capped", CAP, records, 9)
    assert path.stat().st_size > 8 * CAP
    assert _observed(capped) == _observed(reference)
    state = agent_trace._cursor("session")
    assert "overflow" not in state and not state["pending"]
    assert state["source"]["accepted_bytes"] == path.stat().st_size
    assert max(cursor_sizes) < 4 * CAP
    assert len(state["records"]) < len(records) / 4


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
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    agent_trace.export_session("session", str(path), agent_trace.Identity("session"))
    assert receiver.calls == calls
    assert _observed(receiver) == before


def test_open_tool_call_keeps_its_records_until_the_result_lands(tmp_path, monkeypatch, quiet):
    records = _claude(3, 2)
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
