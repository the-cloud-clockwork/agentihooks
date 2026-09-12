"""Single choke point for context output back to the host CLI.

Claude Code concatenates every raw stdout line into injected context, so
handlers can print as many times as they like. Codex and Copilot parse hook
stdout as ONE JSON object — multiple prints would corrupt it. Under those
targets, context is buffered here across the whole hook process and flushed
exactly once, as a single ``hookSpecificOutput.additionalContext`` envelope,
by ``main()`` right before exit. Events with no context channel on the target
(codex PreToolUse) drop the buffer with a log line instead.

Invariant: the module-global buffer never survives past the request it was
built for. ``flush()`` clears it on the success path; ``drain()`` clears it
on every other path (a ``BlockAction``, or any other exception) so content
buffered for one event can never leak into the next event's envelope.
"""

from __future__ import annotations

import json

from hooks.targets import buffers_single_envelope
from hooks.targets.capabilities import can_inject_context
from hooks.targets.capabilities import context_cap_bytes as capability_cap

_buffer: list[str] = []
_forced = False


def force_buffer() -> None:
    """Buffer on a target that normally prints raw (claude PreToolUse).

    A PreToolUse decision envelope is only parsed when it is the whole of
    stdout, so every context line printed ahead of it would discard it.
    """
    global _forced
    _forced = True


def forced() -> bool:
    return _forced


def buffer_context(content: str) -> None:
    """Queue *content* for the single end-of-process flush (envelope targets)."""
    _buffer.append(content)


def has_buffered() -> bool:
    return bool(_buffer)


def drain() -> str:
    """Return the buffered content joined, and clear the buffer.

    The exception-path counterpart to ``flush()``: where ``flush()`` emits
    the buffer as the terminal stdout envelope on success, ``drain()`` pulls
    the content out (for a caller to fold into a stderr message, a log line,
    etc.) without emitting anything itself, and always leaves the buffer
    empty. Safe to call when nothing is buffered — returns ``""``.
    """
    if not _buffer:
        return ""
    content = "\n".join(_buffer)
    _buffer.clear()
    return content


def flush(event_name: str) -> None:
    """Emit the buffered context as one JSON envelope. No-op when empty.

    Clears the buffer unconditionally once there is content to flush — see
    the module invariant above.
    """
    if not _buffer:
        return
    content = "\n".join(_buffer)
    _buffer.clear()
    if not buffers_single_envelope() and not _forced:
        # Claude path never buffers; guard against misuse.
        print(content)
        return
    if not can_inject_context(event_name):
        from hooks.common import log
        from hooks.targets import current_target

        log(
            "emitter: context dropped — no context channel for event on target",
            {"event": event_name, "target": current_target(), "chars": len(content)},
        )
        return
    cap = capability_cap(event_name)
    if cap is not None and len(content.encode()) > cap:
        marker = "\n[truncated at the host's additionalContext limit]"
        room = max(0, cap - len(marker.encode()))
        content = content.encode()[:room].decode("utf-8", errors="ignore") + marker

    from hooks.targets import current_target

    # Copilot reads TOP-LEVEL ``additionalContext`` only — the nested claude
    # shape is silently ignored (proven live on v1.0.80: a top-level canary
    # reached the model, a hookSpecificOutput-nested one did not). Codex
    # clones claude's nested contract.
    if current_target() == "copilot":
        print(json.dumps({"additionalContext": content}))
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": content,
                }
            }
        )
    )
