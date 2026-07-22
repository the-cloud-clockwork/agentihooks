"""Agent-pool MCP tools — directed agent-to-agent (A2A) messaging.

- ``call_agent``: message a specific peer by session id. Routed by liveness —
  a live peer is notified via directed inbox AND read via a throwaway fork; a
  dormant peer is resumed and answers directly. Never corrupts a live peer's
  transcript.
- ``pool_list``: who is live in the fleet + what they're working on (scan this
  before deciding whom to call).
- ``pool_status``: self-declare what THIS session is working on.
"""

import json
import os

from hooks.common import log


def register(mcp):
    @mcp.tool()
    def call_agent(target_session_id: str, message: str) -> str:
        """Send a message to a specific fleet agent by its session id.

        Use this to coordinate directly with a peer — e.g. you find (via
        ``pool_list``) that another agent is working the same pipeline and you
        need to warn it off or ask what it's doing.

        Delivery is routed by the peer's liveness, and the SAME call has a
        different effect depending on that (the return value always tells you
        which happened):

        - **Live peer** → the peer is NOT interrupted (resuming a running
          session would corrupt its transcript). Your message is placed in the
          peer's directed inbox — it sees it on its next turn — AND a throwaway
          fork of the peer's context answers you now with what it's working on.
          Returns ``mode="forked+notified"``, ``delivered=true``, ``their_state``.
        - **Dormant peer** (stopped, transcript idle) → the peer's real session
          is resumed, your message delivered, and its reply returned.
          Returns ``mode="resumed"``, ``delivered=true``, ``reply``.

        The peer answers from its loaded context only — call_agent cannot make
        another agent run tools or change anything. It is communication, not
        remote execution.

        Args:
            target_session_id: The peer's session id (from ``pool_list``).
            message: What to say/ask. Coordination and status questions.

        Returns:
            JSON: success, mode, delivered, and their_state (live) or reply (dormant).
        """
        try:
            from hooks.context.agent_pool import call_agent as _call_agent

            caller = os.getenv("CLAUDE_SESSION_ID", "")
            result = _call_agent(target_session_id, message, caller_session_id=caller)
            return json.dumps(result)
        except Exception as e:
            log("MCP call_agent failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def pool_list() -> str:
        """List the live agents in the fleet pool and what each is working on.

        Read this before ``call_agent`` to find who is active and whether anyone
        is touching the same work you are. Excludes your own session.

        Returns:
            JSON with the peer list: session_id, cwd, model, status, summary, last_seen.
        """
        try:
            from hooks.context.agent_pool import list_pool

            caller = os.getenv("CLAUDE_SESSION_ID", "")
            peers = list_pool(include_self=caller)
            return json.dumps({"success": True, "count": len(peers), "agents": peers})
        except Exception as e:
            log("MCP pool_list failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def pool_status(summary: str) -> str:
        """Declare what THIS session is currently working on, for the pool.

        A peer scanning ``pool_list`` sees this line. Set it when you start a
        distinctive piece of work (e.g. "rolling out litellm :dev") so others
        can tell whether they'd collide with you. Otherwise a summary is derived
        automatically from your transcript.

        Args:
            summary: Short description of your current task (one line).

        Returns:
            JSON with success status.
        """
        try:
            from hooks.context.agent_pool import set_summary

            caller = os.getenv("CLAUDE_SESSION_ID", "")
            if not caller:
                return json.dumps({"success": False, "error": "no session id in environment"})
            ok = set_summary(caller, summary)
            if not ok:
                return json.dumps({"success": False, "error": "this session is not registered in the pool yet"})
            return json.dumps({"success": True, "session_id": caller, "summary": summary})
        except Exception as e:
            log("MCP pool_status failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
