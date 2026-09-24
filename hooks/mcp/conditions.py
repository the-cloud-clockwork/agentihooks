"""Condition MCP tools — create, inspect and remove condition scripts from a session.

condition_set and condition_clear work only in a turn whose operator prompt
asked for it ("set a condition ...", "remove the condition ..."); the hook
arms that gate from the typed prompt alone, never from tool output or files.
"""

import json
from pathlib import Path

from hooks.common import log


def register(mcp):
    @mcp.tool()
    def condition_set(
        step: str,
        matcher: str,
        name: str,
        script: str,
        session_id: str,
        language: str = "bash",
        scope: str = "global",
        profile: str = "",
        cwd: str = "",
        run_async: bool = False,
        replace: bool = False,
    ) -> str:
        """Create a condition: a script agentihooks runs on every matching tool call.

        Use ONLY when the operator's own message this turn asks to set/add/create a
        condition. Never create one on your own initiative; the call is refused
        otherwise. The condition is live from the next matching tool call.

        The script receives the hook payload as JSON on stdin (tool_name, tool_input,
        tool_response on post, session_id, cwd, transcript_path) plus AH_* env vars.
        stdout: nothing, plain text (context for the agent), or JSON with context,
        tool_input (pre: rewrite), tool_output (post: replace), decision allow|ask|deny,
        reason. Exit 2 denies the call (pre) with stderr as the reason.

        Args:
            step: "pre" (before the tool runs) or "post" (after it succeeds).
            matcher: any, a tool name (bash, edit, write, read, webfetch), mcp,
                mcp__<server>, mcp__<server>__<tool>, bash.<cli> (e.g. bash.kubectl);
                join alternatives with "+".
            name: letters, digits and "_" only.
            script: the script body.
            session_id: your session id (from the SessionStart banner).
            language: "bash" or "python".
            scope: "global" (bundle-wide; ~/.agentihooks when no bundle is linked),
                "profile" (one profile of the chain), or "directory"
                (<git-root>/.agentihooks/conditions of the current repository).
            profile: profile name for scope="profile"; defaults to the chain's first.
            cwd: directory inside the target repository for scope="directory".
            run_async: run detached; its output is ignored.
            replace: overwrite an existing condition with the same file name.
        """
        try:
            from hooks.context.conditions import ConditionError, create_condition

            try:
                result = create_condition(
                    step=step,
                    matcher=matcher,
                    name=name,
                    script=script,
                    session_id=session_id,
                    language=language,
                    scope=scope,
                    profile=profile,
                    cwd=cwd or None,
                    run_async=run_async,
                    replace=replace,
                )
            except ConditionError as e:
                return json.dumps({"success": False, "error": str(e)})
            return json.dumps({"success": True, **result})
        except Exception as e:
            log("MCP condition_set failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def condition_list(cwd: str = "") -> str:
        """List condition layers (bundle, profiles, runtime, directory), the active
        conditions in execution order, and files whose names do not parse.

        Args:
            cwd: directory whose repository's directory layer to include; defaults
                to the session's project directory.
        """
        try:
            import os

            from hooks.context.conditions import inventory

            result = inventory(cwd or os.getcwd())
            conditions = [
                {k: e[k] for k in ("file", "step", "matcher", "name", "async", "source", "path", "order")}
                for e in result["conditions"]
            ]
            return json.dumps({"success": True, **result, "conditions": conditions})
        except Exception as e:
            log("MCP condition_list failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def condition_show(file: str, cwd: str = "") -> str:
        """Return the script of an active condition by file name (see condition_list).

        Args:
            file: the condition's file name, e.g. "pre-bash.kubectl-readonly.sh".
            cwd: as in condition_list.
        """
        try:
            import os

            from hooks.context.conditions import inventory

            match = next((e for e in inventory(cwd or os.getcwd())["conditions"] if e["file"] == file), None)
            if match is None:
                return json.dumps({"success": False, "error": f"no active condition named {file!r}"})
            return json.dumps(
                {
                    "success": True,
                    "path": match["path"],
                    "source": match["source"],
                    "script": Path(match["path"]).read_text(),
                }
            )
        except Exception as e:
            log("MCP condition_show failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def condition_clear(file: str, session_id: str, scope: str = "", cwd: str = "") -> str:
        """Delete a condition file. Only when the operator's own message this turn
        asks to remove/clear/delete a condition; refused otherwise.

        Args:
            file: the condition's file name.
            session_id: your session id (from the SessionStart banner).
            scope: "global", "profile" or "directory" when the name exists in several layers.
            cwd: as in condition_list.
        """
        try:
            import os

            from hooks.context.conditions import ConditionError, remove_condition

            try:
                result = remove_condition(file=file, session_id=session_id, scope=scope, cwd=cwd or os.getcwd())
            except ConditionError as e:
                return json.dumps({"success": False, "error": str(e)})
            return json.dumps({"success": True, **result})
        except Exception as e:
            log("MCP condition_clear failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
