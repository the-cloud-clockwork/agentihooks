import json
import multiprocessing
import time

from opentelemetry.sdk.trace.export import SpanExportResult

from hooks.observability import agent_trace, otel, trace_flush, transcript


class Receiver:
    def export(self, spans):
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
    path.write_text("".join(json.dumps(record) + "\n" for record in source[:51]))
    assert trace_flush.flush_once("session", str(path)) == 0
    before = agent_trace._cursor("session")["source"]["accepted_bytes"]
    with path.open("a") as handle:
        handle.write("".join(json.dumps(record) + "\n" for record in source[51:]))
    original = transcript.mask_value

    def slow_mask(value):
        if isinstance(value, str) and len(value) > 10000:
            time.sleep(0.1)
        return original(value)

    monkeypatch.setattr(transcript, "mask_value", slow_mask)
    monkeypatch.setenv("AGENTIHOOKS_TRACE_ATTEMPT_TIMEOUT_SEC", "5")
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
