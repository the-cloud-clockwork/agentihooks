"""DynamoDB & PostgreSQL MCP tools."""

import json

from hooks.common import log


def register(mcp):
    @mcp.tool()
    def dynamodb_put_item(
        payload: str,
        table_name: str = "",
        partition_key: str = "",
        sort_key: str = "",
        enrich: bool = False,
    ) -> str:
        """Write an item to DynamoDB table.

        Writes JSON payload to DynamoDB with configurable partition and sort keys.
        Supports state enrichment from conversation_map.json.

        Args:
            payload: JSON string with item data (must contain partition key field)
            table_name: DynamoDB table name (default: from DYNAMODB_TABLE_NAME env var)
            partition_key: Partition key attribute name (default: from env or 'session_id')
            sort_key: Sort key attribute name (default: from env, optional)
            enrich: If True, enriches payload with state from conversation_map.json (default: False)

        Returns:
            JSON with success status, table_name, partition_key_value, sort_key_value
        """
        try:
            from hooks.integrations.dynamodb import put_item

            payload_dict = json.loads(payload)

            result = put_item(
                payload=payload_dict,
                table_name=table_name if table_name else None,
                partition_key=partition_key if partition_key else None,
                sort_key=sort_key if sort_key else None,
                enrich_from_state=enrich,
            )

            return json.dumps(
                {
                    "success": result.success,
                    "table_name": result.table_name,
                    "partition_key": result.partition_key,
                    "partition_key_value": result.partition_key_value,
                    "sort_key": result.sort_key,
                    "sort_key_value": result.sort_key_value,
                    "error": result.error,
                }
            )

        except json.JSONDecodeError as e:
            log("MCP dynamodb_put_item JSON parse failed", {"error": str(e)})
            return json.dumps({"success": False, "error": f"Invalid JSON: {str(e)}"})
        except Exception as e:
            log("MCP dynamodb_put_item failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def postgres_execute(
        query: str,
        params: str = "[]",
    ) -> str:
        """Execute a parameterized SQL query.

        Executes a SQL query with parameterized values for safety.
        Use %s placeholders for parameters.

        Args:
            query: SQL query with %s placeholders
            params: JSON array of parameter values (default: [])

        Returns:
            JSON with success status and rows_affected

        Examples:
            postgres_execute("UPDATE users SET active = TRUE WHERE id = %s", "[123]")
        """
        try:
            from hooks.integrations.postgres import execute

            params_list = json.loads(params)

            result = execute(
                query=query,
                params=tuple(params_list) if params_list else None,
            )

            return json.dumps(
                {
                    "success": result.success,
                    "rows_affected": result.rows_affected,
                    "error": result.error,
                }
            )

        except json.JSONDecodeError as e:
            log("MCP postgres_execute JSON parse failed", {"error": str(e)})
            return json.dumps({"success": False, "error": f"Invalid JSON: {str(e)}"})
        except Exception as e:
            log("MCP postgres_execute failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
