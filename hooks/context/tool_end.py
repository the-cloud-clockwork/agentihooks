"""A call the PreToolUse hook denies never runs, so no PostToolUse closes it: end it here for the stall clock."""


def closing_denied(handler, payload, blocked):
    try:
        handler(payload)
    except blocked:
        from hooks.hook_manager import _swarm_tool_end

        _swarm_tool_end(payload)
        raise
