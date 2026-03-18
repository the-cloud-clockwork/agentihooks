"""Utility MCP tools — markdown writing, env, tool listing."""

import json
import os

from hooks.common import log
from hooks.config import AGENTIHOOKS_HOME


def register(mcp):
    @mcp.tool()
    def write_markdown(filepath: str, content: str) -> str:
        """Write a markdown file.

        MANDATORY tool for docgen agent - Write tool is blocked for markdown files.

        Args:
            filepath: Path to write (must be .md extension, under $AGENTIHOOKS_HOME/package or /tmp)
            content: Markdown content to write

        Returns:
            JSON with write result
        """
        try:
            from pathlib import Path

            path = Path(filepath)
            if path.suffix.lower() != ".md":
                return json.dumps({"success": False, "error": f"Only .md files allowed, got: '{path.suffix}'"})

            resolved = path.resolve()
            allowed_prefixes = [str(AGENTIHOOKS_HOME / "package"), "/tmp"]
            if not any(str(resolved).startswith(p) for p in allowed_prefixes):
                return json.dumps(
                    {"success": False, "error": f"Path not allowed. Must be under: {allowed_prefixes}. Got: {resolved}"}
                )

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            bytes_written = len(content.encode("utf-8"))

            log(
                "MCP write_markdown completed",
                {"filepath": str(path), "bytes_written": bytes_written},
            )

            return json.dumps(
                {
                    "success": True,
                    "filepath": str(path),
                    "bytes_written": bytes_written,
                }
            )

        except Exception as e:
            log("MCP write_markdown failed", {"filepath": filepath, "error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def get_env(filter: str = "") -> str:
        """Get environment variables, optionally filtered by a substring.

        Returns environment variables that contain the filter string (case-insensitive).
        If no filter is provided, returns all environment variables.

        Args:
            filter: Substring to filter environment variable names (case-insensitive).

        Returns:
            JSON with matching environment variables (names and values)
        """
        try:
            env_vars = dict(os.environ)

            if filter:
                filter_lower = filter.lower()
                filtered_vars = {k: v for k, v in env_vars.items() if filter_lower in k.lower()}
            else:
                filtered_vars = env_vars

            return json.dumps(
                {
                    "success": True,
                    "filter": filter if filter else None,
                    "count": len(filtered_vars),
                    "variables": filtered_vars,
                }
            )

        except Exception as e:
            log("MCP get_env failed", {"filter": filter, "error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def hooks_list_tools() -> str:
        """List all available MCP tools in this server.

        Returns:
            JSON with tool names grouped by category
        """
        from hooks.mcp._registry import CATEGORY_MODULES

        tools = {
            "aws": [
                "aws_get_profiles",
                "aws_get_account_id",
                "aws_get_all_accounts",
                "aws_find_account",
            ],
            "email": [
                "email_send",
                "email_send_markdown_file",
            ],
            "messaging": [
                "sqs_send_message",
                "sqs_load_state",
                "webhook_send",
            ],
            "storage": [
                "storage_upload_path",
            ],
            "database": [
                "dynamodb_put_item",
                "postgres_insert",
                "postgres_execute",
            ],
            "compute": [
                "lambda_invoke_function",
            ],
            "observability": [
                "metrics_start_timer",
                "metrics_stop_timer",
                "metrics_create_collector",
                "metrics_get_summary",
                "log_message",
                "log_command_output",
                "tail_container_logs",
            ],
            "utilities": [
                "write_markdown",
                "get_env",
                "hooks_list_tools",
            ],
        }

        # Filter to only categories that are actually loaded
        registered_tools = {t.name for t in mcp._tool_manager.list_tools()}
        active = {}
        for cat, cat_tools in tools.items():
            present = [t for t in cat_tools if t in registered_tools]
            if present:
                active[cat] = present

        total = sum(len(t) for t in active.values())

        return json.dumps(
            {
                "success": True,
                "total_tools": total,
                "available_categories": list(CATEGORY_MODULES.keys()),
                "categories": active,
            }
        )
