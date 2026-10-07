import json
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

FAKE_TOKEN = "ghp_" + "Q7" * 18


def _identity(account="nctcc"):
    from hooks.observability import agent_trace

    return agent_trace.Identity(session_id="sess-1", agent="eng-11", account=account)


def _prompt(uuid, ts, content):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "message": {"role": "user", "content": content}}


def _assistant(uuid, ts, msg_id, blocks):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "message": {"id": msg_id, "model": "claude-opus-5-5", "role": "assistant", "content": blocks, "usage": {}},
    }


def _result(uuid, ts, tool_id, content):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    return {"type": "user", "uuid": uuid, "timestamp": ts, "message": {"role": "user", "content": [block]}}


def _entries(prompt="fix the bug", tool_output="file-a\nfile-b"):
    return [
        _prompt("u1", "2026-10-04T10:00:01.000Z", prompt),
        _assistant("a1", "2026-10-04T10:00:02.000Z", "m1", [{"type": "text", "text": "Looking."}]),
        _assistant(
            "a2",
            "2026-10-04T10:00:03.000Z",
            "m1",
            [{"type": "tool_use", "id": "tool-a", "name": "Bash", "input": {"command": "ls"}}],
        ),
        _result("r1", "2026-10-04T10:00:04.000Z", "tool-a", tool_output),
        _assistant(
            "a3",
            "2026-10-04T10:00:05.000Z",
            "m2",
            [{"type": "tool_use", "id": "tool-b", "name": "Read", "input": {"file_path": "/x"}}],
        ),
        _result("r2", "2026-10-04T10:00:06.000Z", "tool-b", [{"type": "text", "text": "line one"}, {"type": "image"}]),
        _assistant("a4", "2026-10-04T10:00:07.000Z", "m3", [{"type": "text", "text": "Fixed it."}]),
        _prompt("u2", "2026-10-04T10:01:00.000Z", [{"type": "text", "text": "and the docs"}]),
        _assistant("a5", "2026-10-04T10:01:02.000Z", "m4", [{"type": "text", "text": "Docs done."}]),
    ]


def _spans(entries=None, identity=None):
    from hooks.observability import agent_trace

    return agent_trace.session_spans(entries or _entries(), identity or _identity())


def _kind(spans, kind):
    return [s for s in spans if s.attributes.get("langfuse.observation.type") == kind]


def _tool(spans, name):
    return next(s for s in _kind(spans, "tool") if s.attributes["gen_ai.tool.name"] == name)


def test_turn_spans_carry_the_prompt_and_the_assistant_text():
    first, second = _kind(_spans(), "span")
    assert first.attributes["langfuse.observation.input"] == "fix the bug"
    assert first.attributes["langfuse.observation.output"] == "Looking.\n\nFixed it."
    assert second.attributes["langfuse.observation.input"] == "and the docs"
    assert second.attributes["langfuse.observation.output"] == "Docs done."


def test_generation_spans_carry_their_message_text():
    outputs = {
        s.attributes["gen_ai.response.id"]: s.attributes.get("langfuse.observation.output")
        for s in _kind(_spans(), "generation")
    }
    assert outputs == {"m1": "Looking.", "m2": None, "m3": "Fixed it.", "m4": "Docs done."}


def test_tool_spans_carry_input_and_result_text():
    spans = _spans()
    bash = _tool(spans, "Bash")
    assert json.loads(bash.attributes["langfuse.observation.input"]) == {"command": "ls"}
    assert bash.attributes["langfuse.observation.output"] == "file-a\nfile-b"
    read = _tool(spans, "Read")
    assert read.attributes["langfuse.observation.output"] == "line one\n[image]"


def test_root_carries_trace_input_output_and_every_span_session_and_user():
    spans = _spans()
    root = next(s for s in spans if s.parent_id is None)
    assert root.attributes["langfuse.observation.type"] == "agent"
    assert root.attributes["langfuse.trace.input"] == "fix the bug"
    assert root.attributes["langfuse.trace.output"] == "Docs done."
    assert all(s.attributes["langfuse.session.id"] == "sess-1" for s in spans)
    assert all(s.attributes["langfuse.user.id"] == "nctcc" for s in spans)


def test_user_falls_back_to_the_login_user_without_a_routed_account(monkeypatch):
    monkeypatch.setenv("USER", "iamroot")
    spans = _spans(identity=_identity(account=""))
    assert {s.attributes["langfuse.user.id"] for s in spans} == {"iamroot"}


def _values(spans):
    return [v for s in spans for v in s.attributes.values() if isinstance(v, str)]


@pytest.mark.parametrize("mode", ["off", "standard"])
def test_planted_credential_is_masked_in_every_exported_field(mode):
    entries = _entries(prompt=f"use {FAKE_TOKEN} please", tool_output=f"token={FAKE_TOKEN}")
    with patch("hooks.config.SECRETS_MODE", mode):
        values = _values(_spans(entries))
    assert not any(FAKE_TOKEN in v for v in values)
    assert sum("[REDACTED:github_token]" in v for v in values) >= 3


def test_fields_over_the_cap_are_cut_with_a_marker():
    entries = _entries(prompt="p" * 200)
    with patch("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 50):
        turn = _kind(_spans(entries), "span")[0]
    assert turn.attributes["langfuse.observation.input"] == "p" * 50 + "…[truncated 150 chars]"


def test_masking_runs_before_the_cap_so_no_credential_fragment_survives():
    entries = _entries(prompt="x" * 40 + FAKE_TOKEN)
    with patch("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 50):
        values = _values(_spans(entries))
    assert not any("ghp_" in v for v in values)


class _Exporter:
    def __init__(self):
        self.batches = []

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        self.batches.append(list(spans))
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


def test_export_splits_spans_into_batches_under_the_text_budget(monkeypatch, tmp_path):
    from hooks.observability import agent_trace, otel

    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    monkeypatch.setattr(agent_trace, "BATCH_CHARS", 30)
    exporter = _Exporter()
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: exporter)
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in _entries()))

    agent_trace.export_session("sess-1", str(path), _identity())

    assert len(exporter.batches) > 1
    exported = [s.name for batch in exporter.batches for s in batch]
    assert sorted(exported) == sorted(s.name for s in _spans())
    assert (tmp_path / "cursor" / "sess-1.json").exists()


def _persisted(tmp_path, text, folder="tool-results", size=None):
    side = tmp_path / folder / "b1.txt"
    side.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        side.write_text(text)
    preview = f"<persisted-output>\nOutput too large. Full output saved to: {side}\n\nPreview (first 2KB):\nhead"
    entries = _entries(tool_output=preview)
    entries[3]["toolUseResult"] = {
        "stdout": "head",
        "persistedOutputPath": str(side),
        "persistedOutputSize": len(text) if size is None else size,
    }
    return entries, preview


def test_a_persisted_claude_tool_output_exports_its_side_file_masked(tmp_path):
    entries, _ = _persisted(tmp_path, "row\n" * 5_000 + f"token={FAKE_TOKEN}")
    bash = _tool(_spans(entries), "Bash")
    assert bash.attributes["langfuse.observation.output"] == "row\n" * 5_000 + "token=[REDACTED:github_token]"
    assert not any(key.startswith("agentihooks.truncation.") for key in bash.attributes)


def test_a_persisted_tool_output_over_the_cap_counts_its_truncation(tmp_path):
    entries, _ = _persisted(tmp_path, "x" * 120)
    with patch("hooks.config.LANGFUSE_FIELD_MAX_CHARS", 50):
        bash = _tool(_spans(entries), "Bash")
    assert bash.attributes["langfuse.observation.output"] == "x" * 50 + "…[truncated 70 chars]"
    assert bash.attributes["agentihooks.truncation.langfuse.observation.output.chars"] == 70


@pytest.mark.parametrize("folder, text", [("tool-results", None), ("elsewhere", "secret file")])
def test_an_unread_side_file_keeps_the_preview_and_counts_what_it_omits(tmp_path, folder, text):
    entries, preview = _persisted(tmp_path, text, folder, size=40_000)
    bash = _tool(_spans(entries), "Bash")
    assert bash.attributes["langfuse.observation.output"] == preview
    assert bash.attributes["agentihooks.truncation.langfuse.observation.output.chars"] == 40_000 - len(preview)


def test_a_persisted_output_without_a_size_counts_nothing_it_cannot_measure(tmp_path):
    entries, preview = _persisted(tmp_path, None, size="large")
    bash = _tool(_spans(entries), "Bash")
    assert bash.attributes["langfuse.observation.output"] == preview
    assert not any(key.startswith("agentihooks.truncation.") for key in bash.attributes)


def test_a_persisted_output_counts_toward_the_root_truncated_fields(tmp_path):
    from hooks.observability import agent_trace

    entries, _ = _persisted(tmp_path, None, size=40_000)
    assert agent_trace._truncated_fields(_spans(entries)) == 1
