"""The swarm session gates PreToolUse runs in order; the first that answers denies the call, and a gate that fails is
logged and passes."""

from hooks.common import log


def _recycle(tool_name: str, tool_input: dict, session_id: str) -> str | None:
    from hooks.context.context_recycle import gate

    return gate(tool_name, tool_input, session_id)


def _claim(tool_name: str, tool_input: dict, session_id: str) -> str | None:
    from hooks.context.claim_watch import check

    return check()


GATES = (("claim watch", _claim), ("context recycle gate", _recycle))


def pretool(tool_name: str, tool_input: dict, session_id: str) -> str | None:
    for label, gate in GATES:
        try:
            block = gate(tool_name, tool_input, session_id)
        except (OSError, RuntimeError, ValueError) as e:
            log(f"{label} failed", {"error": str(e)})
            continue
        if block:
            return block
    return None
