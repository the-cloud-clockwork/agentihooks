"""Caller-session resolution for the session-scoped agentihooks MCP tools.

Under stdio, Claude Code spawns one MCP subprocess per session, so the process
environment *is* the identity boundary and an env lookup answers "who is
calling". Under a network transport the server is one long-lived process shared
by every session, and no environment read can distinguish callers — the answer
has to arrive with the call.

Hence the precedence below: an explicit argument is the only mechanism correct
under both transports, so the tool bodies never branch on transport.
"""

import os

# Whether an environment lookup is allowed to answer "who is calling".
#
# True only under stdio, where the process *is* one session. A daemon inherits
# whatever CLAUDE_CODE_SESSION_ID its launching shell happened to carry, so an
# env read there does not return "no idea" — it returns a real, wrong session id
# and silently writes another agent's state. Observed in testing: a daemon
# started from a live Claude Code shell attributed every caller's ack to that
# shell's session.
#
# Set once at startup by ``hooks.mcp.build_server``; the tool bodies never
# branch on transport themselves.
_ENV_FALLBACK_ALLOWED = True


def set_env_fallback_allowed(allowed: bool) -> None:
    """Enable or disable environment-based caller resolution process-wide."""
    global _ENV_FALLBACK_ALLOWED
    _ENV_FALLBACK_ALLOWED = allowed


def resolve_session_id(explicit: str = "") -> str:
    """Resolve the calling session's id.

    Precedence:
        1. ``explicit`` — the ``session_id`` argument the agent passed. Correct
           under every transport; the only thing that works under a shared
           network daemon.
        2. ``CLAUDE_CODE_SESSION_ID`` — what Claude Code actually injects into a
           stdio MCP subprocess. Consulted only when the env fallback is
           allowed, i.e. one process serves one session.
        3. ``CLAUDE_SESSION_ID`` — legacy name this code read for a long time.
           Claude Code never sets it; kept only for anything setting it
           out-of-band.

    Returns:
        The resolved session id, or ``""`` when nothing resolves. Callers decide
        whether that is fatal. Under a network transport an omitted argument
        always yields ``""`` — refusing to act beats acting as the wrong agent.
    """
    if explicit:
        return explicit
    if not _ENV_FALLBACK_ALLOWED:
        return ""
    return os.getenv("CLAUDE_CODE_SESSION_ID", "") or os.getenv("CLAUDE_SESSION_ID", "")
