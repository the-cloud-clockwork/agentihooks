import json
import multiprocessing
import time

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult

from hooks.observability import agent_trace, otel, trace_flush, transcript


class Receiver:
    def __init__(self):
        self.observations = {}

    def export(self, spans):
        self.observations.update({span.context.span_id: span for span in spans})
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


def records(count):
    stamp = "2026-10-12T10:00:00Z"
    yield {"type": "user", "uuid": "prompt", "timestamp": stamp, "message": {"content": "probe"}}
    for index in range(count):
        yield {
            "type": "assistant",
            "uuid": f"call-{index}",
            "timestamp": stamp,
            "message": {
                "id": "generation",
                "content": [{"type": "tool_use", "id": f"tool-{index}", "name": "shell", "input": {"cmd": "probe"}}],
            },
        }
        yield {
            "type": "user",
            "uuid": f"result-{index}",
            "timestamp": stamp,
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": f"tool-{index}", "content": "public output " * 4000}]
            },
        }


def timed_export(path, phases):
    for name in ("_stage_source", "_prepare_pending", "_save_progress", "_send_pending"):
        original = getattr(agent_trace, name)

        def timed(*args, phase=name, call=original):
            with phases.open("a") as handle:
                handle.write(f"{phase} start {time.monotonic()}\n")
            result = call(*args)
            with phases.open("a") as handle:
                handle.write(f"{phase} end {time.monotonic()}\n")
            return result

        setattr(agent_trace, name, timed)
    agent_trace.export_session("session", str(path))


def test_deadline_checkpoints_a_growing_open_turn(tmp_path, monkeypatch):
    path = tmp_path / "transcript.jsonl"
    phases = tmp_path / "phases.txt"
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    monkeypatch.setattr(agent_trace, "_root_attributes", lambda session: {})
    monkeypatch.setattr(agent_trace, "_collector_outcomes", lambda *args: None)
    monkeypatch.setattr(otel, "langfuse_exporter", Receiver)
    source = list(records(80))
    path.write_text("".join(json.dumps(record) + "\n" for record in source[:2] + source[3:51]))
    for _ in range(10):
        if trace_flush.flush_once("session", str(path)) == 0:
            break
    before = agent_trace._cursor("session")["source"]["accepted_bytes"]
    assert before == path.stat().st_size
    with path.open("a") as handle:
        handle.write("".join(json.dumps(record) + "\n" for record in source[51:]))
    original = transcript.mask_value

    def slow_mask(value):
        if isinstance(value, dict) and str(value.get("uuid", "")).startswith("result-"):
            time.sleep(0.2)
        return original(value)

    monkeypatch.setattr(transcript, "mask_value", slow_mask)
    child = multiprocessing.get_context("fork").Process(target=timed_export, args=(path, phases))
    child.start()
    child.join(5)
    if child.is_alive():
        child.kill()
        child.join()
    progress = agent_trace._cursor("session")
    print(phases.read_text())
    print(
        json.dumps({"generated": path.stat().st_size, "before": before, "after": progress["source"]["accepted_bytes"]})
    )
    assert progress["source"]["accepted_bytes"] > before
    assert child.exitcode == 0


@pytest.mark.parametrize(("page_seconds", "timeout", "accepted"), [(1, 5, 3), (3, 5, 1), (0.1, 0.5, 3)])
def test_retry_returns_with_checkpoint_before_its_deadline(monkeypatch, page_seconds, timeout, accepted):
    elapsed = [0.0]
    state = {"source": {"accepted_bytes": 0}, "overflow": {"bytes": 1}}

    def advance(*args):
        elapsed[0] += page_seconds
        state["source"]["accepted_bytes"] += 1
        if state["source"]["accepted_bytes"] == 10:
            state.pop("overflow")

    monkeypatch.setattr(trace_flush.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(agent_trace, "export_session", advance)
    monkeypatch.setattr(agent_trace, "_cursor", lambda session: state)
    monkeypatch.setenv("AGENTIHOOKS_TRACE_FLUSH_ATTEMPT_TIMEOUT_SEC", str(timeout))
    assert trace_flush.flush_once("session", "/transcript") == 1
    assert state["source"]["accepted_bytes"] == accepted
    assert elapsed[0] < timeout


@pytest.fixture
def exporter(tmp_path, monkeypatch):
    receiver = Receiver()
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    monkeypatch.setattr(agent_trace, "_root_attributes", lambda session: {})
    monkeypatch.setattr(agent_trace, "_collector_outcomes", lambda *args: None)
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: receiver)
    return receiver


def test_revised_record_updates_its_cached_observation(tmp_path, exporter):
    source = list(records(1))
    path = tmp_path / "source.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in source))
    agent_trace.export_session("session", str(path))
    source[-1]["message"]["content"][0]["content"] = "updated output"
    path.write_text("".join(json.dumps(record) + "\n" for record in source))
    agent_trace.export_session("session", str(path))
    tool = exporter.observations[agent_trace._span_id("session", "tool-0")]
    assert tool.attributes["langfuse.observation.output"] == "updated output"


def test_paging_bounds_the_durable_source_cache(tmp_path, exporter):
    source = list(records(1))
    source.extend(
        [
            {
                "type": "user",
                "uuid": "prompt-2",
                "timestamp": "2026-10-12T10:01:00Z",
                "message": {"content": "next prompt"},
            },
            {
                "type": "assistant",
                "uuid": "assistant-2",
                "timestamp": "2026-10-12T10:01:00Z",
                "message": {"id": "generation-2", "content": "next answer"},
            },
        ]
    )
    path = tmp_path / "source.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in source))
    assert trace_flush.flush_once("session", str(path)) == 0
    state = agent_trace._cursor("session")
    assert set(state["source_revisions"]) == {"prompt-2", "assistant-2"}
    assert state["source"]["accepted_bytes"] == path.stat().st_size


@pytest.mark.parametrize(("first_bytes", "accepted"), [(262143, "all"), (262144, "first")])
def test_source_page_checkpoint_at_the_byte_boundary(tmp_path, exporter, first_bytes, accepted):
    source = list(records(1))[0]
    source["message"]["content"] = ""
    size = len((json.dumps(source) + "\n").encode())
    source["message"]["content"] = "p" * (first_bytes - size)
    line = json.dumps(source) + "\n"
    next_line = (
        json.dumps(
            {
                "type": "user",
                "uuid": "prompt-2",
                "timestamp": "2026-10-12T10:01:00Z",
                "message": {"content": "next prompt"},
            }
        )
        + "\n"
    )
    path = tmp_path / "source.jsonl"
    path.write_text(line + next_line)
    agent_trace.export_session("session", str(path))
    expected = first_bytes if accepted == "first" else path.stat().st_size
    assert agent_trace._cursor("session")["source"]["accepted_bytes"] == expected
    assert trace_flush.flush_once("session", str(path)) == 0
    assert agent_trace._cursor("session")["source"]["accepted_bytes"] == path.stat().st_size


def test_source_page_counts_only_new_record_bytes(tmp_path, exporter):
    prompt = list(records(1))[0]
    assistant = {
        "type": "assistant",
        "uuid": "answer",
        "timestamp": "2026-10-12T10:00:00Z",
        "message": {"id": "generation", "content": ""},
    }
    lines = []
    for record in (prompt, assistant):
        record["message"]["content"] = ""
        size = len((json.dumps(record) + "\n").encode())
        record["message"]["content"] = "p" * (131071 - size)
        lines.append(json.dumps(record) + "\n")
    lines.append(
        json.dumps(
            {"type": "user", "uuid": "next", "timestamp": "2026-10-12T10:01:00Z", "message": {"content": "next"}}
        )
        + "\n"
    )
    path = tmp_path / "source.jsonl"
    path.write_text("".join(lines))
    agent_trace.export_session("session", str(path))
    assert agent_trace._cursor("session")["source"]["accepted_bytes"] == path.stat().st_size


def test_attempt_passes_its_custom_budget_to_the_child(monkeypatch):
    received = []

    def run(command, env, timeout):
        received.append((trace_flush.budget(env).attempt_timeout, timeout))
        return trace_flush.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(trace_flush.subprocess, "run", run)
    assert trace_flush.attempt("session", "/source", 0.5, "interval")
    assert received == [(0.5, 0.5)]
