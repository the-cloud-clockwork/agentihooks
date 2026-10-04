import base64
import json
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _identity():
    from hooks.observability import agent_trace

    return agent_trace.Identity(
        session_id="sess-1",
        agent="swarm-buildout-eng-13",
        swarm="swarm-buildout",
        lane="eng",
        task="t14",
        account="nctcc",
    )


def _usage(inp, out, read=0, made=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": made,
    }


def _prompt(uuid, ts, text):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "message": {"role": "user", "content": text}}


def _assistant(uuid, ts, msg_id, blocks, usage, model="claude-opus-5-5"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "message": {"id": msg_id, "model": model, "role": "assistant", "content": blocks, "usage": usage},
    }


def _result(uuid, ts, tool_id, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": "ok", "is_error": is_error}
    return {"type": "user", "uuid": uuid, "timestamp": ts, "message": {"role": "user", "content": [block]}}


ENTRIES = [
    {"type": "attachment", "timestamp": "2026-10-04T10:00:00.000Z"},
    _prompt("u1", "2026-10-04T10:00:01.000Z", "fix the bug"),
    _assistant("a1", "2026-10-04T10:00:03.000Z", "m1", [{"type": "thinking", "thinking": "x"}], _usage(5, 10, 100, 50)),
    _assistant(
        "a2",
        "2026-10-04T10:00:04.000Z",
        "m1",
        [{"type": "tool_use", "id": "tool-a", "name": "Bash", "input": {}}],
        _usage(5, 20, 100, 50),
    ),
    _result("r1", "2026-10-04T10:00:06.000Z", "tool-a", is_error=True),
    _assistant("a3", "2026-10-04T10:00:08.000Z", "m2", [{"type": "text", "text": "done"}], _usage(3, 7, 200)),
    {
        "type": "user",
        "uuid": "meta",
        "isMeta": True,
        "timestamp": "2026-10-04T10:00:09.000Z",
        "message": {"content": "x"},
    },
    _prompt("u2", "2026-10-04T10:01:00.000Z", [{"type": "text", "text": "and the docs"}]),
    _assistant("a4", "2026-10-04T10:01:02.000Z", "m3", [{"type": "text", "text": "ok"}], _usage(1, 2)),
    {"type": "assistant", "uuid": "side", "isSidechain": True, "timestamp": "2026-10-04T10:01:03.000Z"},
]


def _by_kind(spans, kind):
    return [s for s in spans if s.attributes.get("langfuse.observation.type") == kind]


def test_one_root_per_session_with_trace_attributes_and_tags():
    from hooks.observability import agent_trace

    spans = agent_trace.session_spans(ENTRIES, _identity(), cost=1.25)
    roots = [s for s in spans if s.parent_id is None]
    assert len(roots) == 1
    root = roots[0].attributes
    assert root["langfuse.trace.name"] == "swarm-buildout-eng-13"
    assert root["langfuse.session.id"] == "sess-1"
    assert root["langfuse.trace.tags"] == (
        "swarm:swarm-buildout",
        "agent:swarm-buildout-eng-13",
        "lane:eng",
        "task:t14",
        "account:nctcc",
    )
    assert root["gen_ai.agent.name"] == "swarm-buildout-eng-13"
    assert root["gen_ai.conversation.id"] == "sess-1"
    assert root["gen_ai.request.model"] == "claude-opus-5-5"
    assert root["gen_ai.usage.input_tokens"] == 9
    assert root["gen_ai.usage.output_tokens"] == 29
    assert root["gen_ai.usage.cost"] == 1.25
    assert root["agent.turns"] == 2


def test_turns_generations_and_tools_form_the_tree():
    from hooks.observability import agent_trace

    spans = agent_trace.session_spans(ENTRIES, _identity())
    root = next(s for s in spans if s.parent_id is None)
    turns = _by_kind(spans, "span")
    assert [t.name for t in turns] == ["turn 1", "turn 2"]
    assert all(t.parent_id == root.span_id for t in turns)

    generations = _by_kind(spans, "generation")
    assert len(generations) == 3
    first = generations[0]
    assert first.parent_id == turns[0].span_id
    assert first.attributes["gen_ai.system"] == "anthropic"
    assert first.attributes["gen_ai.request.model"] == "claude-opus-5-5"
    assert first.attributes["gen_ai.usage.input_tokens"] == 5
    assert first.attributes["gen_ai.usage.output_tokens"] == 20
    assert first.attributes["gen_ai.usage.cache_read_input_tokens"] == 100
    assert first.attributes["gen_ai.usage.cache_creation_input_tokens"] == 50

    tools = _by_kind(spans, "tool")
    assert len(tools) == 1
    tool = tools[0]
    assert tool.parent_id == turns[0].span_id
    assert tool.attributes["gen_ai.tool.name"] == "Bash"
    assert tool.attributes["gen_ai.tool.call.id"] == "tool-a"
    assert tool.attributes["error"] is True
    assert tool.end_ns - tool.start_ns == 2_000_000_000


def test_span_ids_are_stable_and_first_turn_skips_exported_turns():
    from hooks.observability import agent_trace

    first = agent_trace.session_spans(ENTRIES, _identity())
    again = agent_trace.session_spans(ENTRIES, _identity())
    assert [s.span_id for s in first] == [s.span_id for s in again]
    later = agent_trace.session_spans(ENTRIES, _identity(), first_turn=1)
    assert [s.name for s in _by_kind(later, "span")] == ["turn 2"]
    assert sum(1 for s in later if s.parent_id is None) == 1


def test_trace_id_derives_from_the_session_id():
    from hooks.observability import agent_trace

    assert agent_trace.trace_id("sess-1") == agent_trace.trace_id("sess-1")
    assert agent_trace.trace_id("sess-1") != agent_trace.trace_id("sess-2")
    assert 0 < agent_trace.trace_id("sess-1") < 2**128


def test_identity_from_env_reads_swarm_launch_variables():
    from hooks.observability import agent_trace

    env = {
        "AGENTIHOOKS_AGENT_NAME": "swarm-buildout-eng-13",
        "AGENTIHOOKS_SWARM": "swarm-buildout",
        "AGENTIHOOKS_SWARM_LANE": "eng",
        "AGENTIHOOKS_SWARM_TASK": "t14",
        "AH_CC_TOKEN_nctcc": "set",
    }
    assert agent_trace.identity_from_env("sess-1", env) == _identity()


def _config(**overrides):
    values = {
        "OTEL_LANGFUSE_ENABLED": True,
        "OTEL_LANGFUSE_ENDPOINT": "http://10.10.30.200/api/public/otel",
        "OTEL_LANGFUSE_HOST_HEADER": "langfuse.homeofanton.com",
        "OTEL_LANGFUSE_PUBLIC_KEY": "pk-fake",
        "OTEL_LANGFUSE_SECRET_KEY": "sk-fake",
        **overrides,
    }
    return patch.multiple("hooks.config", **values)


def test_exporter_config_sends_basic_auth_host_and_ingestion_version():
    from hooks.observability import otel

    with _config():
        config = otel.langfuse_exporter_config()
    assert config["endpoint"] == "http://10.10.30.200/api/public/otel/v1/traces"
    headers = config["headers"]
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"pk-fake:sk-fake").decode()
    assert headers["Host"] == "langfuse.homeofanton.com"
    assert headers["x-langfuse-ingestion-version"] == "4"


def test_exporter_config_without_host_header_sends_none():
    from hooks.observability import otel

    with _config(OTEL_LANGFUSE_HOST_HEADER=""):
        assert "Host" not in otel.langfuse_exporter_config()["headers"]


@pytest.mark.parametrize(
    "override",
    [{"OTEL_LANGFUSE_ENABLED": False}, {"OTEL_LANGFUSE_PUBLIC_KEY": ""}, {"OTEL_LANGFUSE_SECRET_KEY": ""}],
)
def test_exporter_config_is_none_when_disabled_or_keys_missing(override):
    from hooks.observability import otel

    with _config(**override):
        assert otel.langfuse_exporter_config() is None


def test_route_defaults_to_the_lan_ingress_with_its_host_header():

    from hooks.config import langfuse_route

    assert langfuse_route({}) == ("http://10.10.30.200/api/public/otel", "langfuse.homeofanton.com")
    custom = {"OTEL_LANGFUSE_ENDPOINT": "https://langfuse.example.com/api/public/otel"}
    assert langfuse_route(custom) == ("https://langfuse.example.com/api/public/otel", "")
    custom["OTEL_LANGFUSE_HOST_HEADER"] = "langfuse.example.com"
    assert langfuse_route(custom)[1] == "langfuse.example.com"


class _Exporter:
    def __init__(self, result=True):
        self.batches = []
        self.result = result

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        self.batches.append(list(spans))
        return SpanExportResult.SUCCESS if self.result else SpanExportResult.FAILURE

    def shutdown(self):
        pass


def _transcript(tmp_path, entries):
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return str(path)


def test_export_sends_new_turns_once_under_the_session_trace(monkeypatch, tmp_path):
    from hooks.observability import agent_trace, otel

    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    exporter = _Exporter()
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: exporter)
    path = _transcript(tmp_path, ENTRIES)

    agent_trace.export_session("sess-1", path, _identity())
    agent_trace.export_session("sess-1", path, _identity())

    first, second = exporter.batches
    assert {s.context.trace_id for s in first} == {agent_trace.trace_id("sess-1")}
    assert sorted(s.name for s in first if s.name.startswith("turn")) == ["turn 1", "turn 2"]
    assert [s.name for s in second] == ["swarm-buildout-eng-13"]
    root = second[0]
    assert root.parent is None
    child = next(s for s in first if s.name == "turn 1")
    assert child.parent.span_id == root.context.span_id


def test_failed_export_keeps_the_cursor(monkeypatch, tmp_path):
    from hooks.observability import agent_trace, otel

    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    exporter = _Exporter(result=False)
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: exporter)
    path = _transcript(tmp_path, ENTRIES)

    agent_trace.export_session("sess-1", path, _identity())
    agent_trace.export_session("sess-1", path, _identity())

    assert [sum(1 for s in b if s.name.startswith("turn")) for b in exporter.batches] == [2, 2]


def test_export_is_a_noop_when_langfuse_is_off(monkeypatch, tmp_path):
    from hooks.observability import agent_trace, otel

    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "cursor")
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: None)
    agent_trace.export_session("sess-1", _transcript(tmp_path, ENTRIES), _identity())
    assert not (tmp_path / "cursor").exists()
