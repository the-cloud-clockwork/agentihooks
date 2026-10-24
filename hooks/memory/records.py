"""Per-entry transcript record normalization for Claude, Codex and Copilot.

Each provider decoder turns one raw transcript entry into unified records and
keeps the only cross-entry state, whether the current turn already produced
assistant text, in a ``TurnState`` the caller owns. A whole-file reader and an
incremental consumer share the same decoders; an incremental consumer saves
``TurnState.to_dict()`` with its cursor so a restart does not recount a
completion.
"""

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from typing import TypedDict


class TranscriptRecord(TypedDict, total=False):
    kind: str  # user_text | assistant_text | system_text | tool_call | tool_result | token_usage | turn_complete | meta
    # turn_complete is a fallback only: emitted for a codex turn when no
    # assistant_text record was produced for it (e.g. a rollout truncated
    # before the response_item message landed). A turn that already yielded
    # assistant_text never also yields turn_complete, so consumers that
    # treat the two kinds as equivalent digest text don't double-count.
    text: str
    tool_name: str
    tool_input: object
    tool_result: object
    tool_use_id: str
    is_error: bool
    raw: dict


@dataclass
class TurnState:
    assistant_seen: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "TurnState":
        return cls(assistant_seen=bool(data.get("assistant_seen", False)))


Decoder = Callable[[dict, TurnState], Iterator[TranscriptRecord]]

CODEX_TYPES = ("session_meta", "response_item", "event_msg", "turn_context", "world_state")

# Copilot session events are ``{id, timestamp, parentId, type, data}`` where
# ``type`` is a dotted namespace string ("user.message", "tool.execution_start").
# Neither claude nor codex ever writes a dotted type, so the prefix alone is a
# sound discriminator.
_COPILOT_TYPE_PREFIXES = (
    "session.",
    "user.",
    "assistant.",
    "tool.",
    "system.",
    "hook.",
    "subagent.",
    "mcp.",
    "permission.",
)


def is_copilot_type(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_COPILOT_TYPE_PREFIXES)


def is_inbox_delivery(entry: dict) -> bool:
    origin = entry.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "channel" and origin.get("server") == "inbox"


def decode_claude(entry: dict, state: TurnState) -> Iterator[TranscriptRecord]:
    etype = entry.get("type")
    if etype not in ("user", "assistant"):
        return
    message = entry.get("message")
    kind = "assistant_text" if etype == "assistant" else "user_text"
    # isMeta user entries are harness-injected (reminders, caveats, skill
    # bodies); an inbox channel delivery is isMeta too but stays a prompt.
    if kind == "user_text" and entry.get("isMeta") and not is_inbox_delivery(entry):
        kind = "system_text"
    if isinstance(message, str):
        yield {"kind": kind, "text": message, "raw": entry}
        return
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        yield {"kind": kind, "text": content, "raw": entry}
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            yield {"kind": kind, "text": block.get("text", ""), "raw": entry}
        elif btype == "tool_use":
            yield {
                "kind": "tool_call",
                "tool_name": block.get("name", ""),
                "tool_input": block.get("input"),
                "tool_use_id": block.get("id", ""),
                "raw": entry,
            }
        elif btype == "tool_result":
            yield {
                "kind": "tool_result",
                "tool_result": block.get("content"),
                "tool_use_id": block.get("tool_use_id", ""),
                "is_error": bool(block.get("is_error")),
                "raw": entry,
            }


def _codex_response_item(payload: dict, entry: dict, state: TurnState) -> Iterator[TranscriptRecord]:
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        kind = {
            "assistant": "assistant_text",
            "user": "user_text",
        }.get(role, "system_text")
        if role == "user":
            state.assistant_seen = False
        texts = [
            b.get("text", "")
            for b in payload.get("content", [])
            if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text")
        ]
        if texts:
            if kind == "assistant_text":
                state.assistant_seen = True
            yield {"kind": kind, "text": "\n".join(texts), "raw": entry}
    elif ptype in ("function_call", "local_shell_call", "custom_tool_call"):
        yield {
            "kind": "tool_call",
            "tool_name": payload.get("name", ptype),
            "tool_input": payload.get("arguments") or payload.get("action"),
            "tool_use_id": payload.get("call_id", ""),
            "raw": entry,
        }
    elif ptype in ("function_call_output", "custom_tool_call_output"):
        yield {
            "kind": "tool_result",
            "tool_result": payload.get("output"),
            "tool_use_id": payload.get("call_id", ""),
            # Codex does not carry an is_error flag on outputs; leave
            # False and let consumers pattern-match the content.
            "is_error": False,
            "raw": entry,
        }


def decode_codex(entry: dict, state: TurnState) -> Iterator[TranscriptRecord]:
    # task_complete repeats the turn's reply; it is only a fallback for a turn with no assistant_text.
    etype = entry.get("type")
    payload = entry.get("payload", {})
    if not isinstance(payload, dict):
        return
    if etype == "response_item":
        yield from _codex_response_item(payload, entry, state)
    elif etype == "event_msg":
        ptype = payload.get("type")
        if ptype == "token_count":
            yield {"kind": "token_usage", "raw": entry}
        elif ptype == "task_complete":
            if not state.assistant_seen:
                yield {
                    "kind": "turn_complete",
                    "text": payload.get("last_agent_message") or "",
                    "raw": entry,
                }
            state.assistant_seen = False
    elif etype == "session_meta":
        yield {"kind": "meta", "raw": entry}


def _copilot_result_text(result: object) -> object:
    """Pull the model-facing text out of a copilot tool result, else pass through."""
    if not isinstance(result, dict):
        return result
    for key in ("content", "detailedContent"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    blocks = result.get("contents")
    if isinstance(blocks, list):
        texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("text")]
        if texts:
            return "\n".join(texts)
    return result


def _copilot_message(etype: str, data: dict, entry: dict, state: TurnState) -> Iterator[TranscriptRecord]:
    text = data.get("content")
    if etype == "user.message":
        # Hook-injected context rides in as a user.message with
        # source="system" (observed live, v1.0.80) — counting it as a
        # user turn would charge every additionalContext injection as a
        # turn of its own.
        if data.get("source") == "system":
            if text:
                yield {"kind": "system_text", "text": text, "raw": entry}
            return
        state.assistant_seen = False
        if text:
            yield {"kind": "user_text", "text": text, "raw": entry}
    elif etype == "assistant.message":
        if text:
            state.assistant_seen = True
            yield {"kind": "assistant_text", "text": text, "raw": entry}
    elif text:
        yield {"kind": "system_text", "text": text, "raw": entry}


def _copilot_tool(etype: str, data: dict, entry: dict) -> TranscriptRecord:
    if etype == "tool.execution_start":
        return {
            "kind": "tool_call",
            "tool_name": data.get("toolName", ""),
            "tool_input": data.get("arguments"),
            "tool_use_id": data.get("toolCallId", ""),
            "raw": entry,
        }
    # Unlike codex, copilot states success explicitly, so error
    # scanning does not have to pattern-match the result content.
    failed = not data.get("success", True)
    error = data.get("error")
    return {
        "kind": "tool_result",
        "tool_result": (
            error.get("message", "") if failed and isinstance(error, dict) else _copilot_result_text(data.get("result"))
        ),
        "tool_use_id": data.get("toolCallId", ""),
        "is_error": failed,
        "raw": entry,
    }


def decode_copilot(entry: dict, state: TurnState) -> Iterator[TranscriptRecord]:
    # Same turn-boundary rule as the codex decoder: session.task_complete
    # repeats the turn's summary. Ephemeral events are streaming deltas and
    # re-renders, never durable content — including them would multiply-count
    # every reply.
    etype = entry.get("type")
    if not is_copilot_type(etype):
        return
    if entry.get("ephemeral") and etype != "assistant.usage":
        return
    data = entry.get("data")
    if not isinstance(data, dict):
        return
    if etype in ("user.message", "assistant.message", "system.message"):
        yield from _copilot_message(etype, data, entry, state)
    elif etype in ("tool.execution_start", "tool.execution_complete"):
        yield _copilot_tool(etype, data, entry)
    elif etype == "assistant.usage":
        yield {"kind": "token_usage", "raw": entry}
    elif etype == "session.task_complete":
        if not state.assistant_seen:
            yield {"kind": "turn_complete", "text": data.get("summary") or "", "raw": entry}
        state.assistant_seen = False
    elif etype == "session.start":
        yield {"kind": "meta", "raw": entry}


DECODERS: dict[str, Decoder] = {
    "claude": decode_claude,
    "codex": decode_codex,
    "copilot": decode_copilot,
}


def normalize_entries(
    fmt: str, entries: Iterable[object], state: TurnState | None = None
) -> Iterator[TranscriptRecord]:
    """Yield unified records for ``entries``, advancing ``state`` in place across calls."""
    decode = DECODERS[fmt]
    turn = TurnState() if state is None else state
    for entry in entries:
        if isinstance(entry, dict):
            yield from decode(entry, turn)
