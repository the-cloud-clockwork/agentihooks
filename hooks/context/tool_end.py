"""A call the PreToolUse hook denies never runs, so no PostToolUse closes it: end it here for the stall clock."""


def closing_denied(handler, payload, blocked, end):
    try:
        handler(payload)
    except blocked:
        end(payload)
        raise
