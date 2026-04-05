"""SQS & Webhook messaging MCP tools."""

import json

from hooks.common import log


def register(mcp):
    @mcp.tool()
    def sqs_send_message(
        message_body: str,
        enrich: bool = True,
    ) -> str:
        """Send a message to SQS queue with optional state enrichment.

        Args:
            message_body: JSON string with message data
            enrich: If True, enriches message with state from .agent-state.json (default: True)

        Returns:
            JSON with success status, message_id, and enriched message data
        """
        try:
            from hooks.integrations.sqs import send_message

            message_dict = json.loads(message_body)
            result = send_message(message_dict, enrich_from_state=enrich)

            return json.dumps(
                {
                    "success": result.success,
                    "message_id": result.message_id,
                    "enriched": result.enriched,
                    "state_fields": result.state_fields,
                    "error": result.error,
                }
            )

        except json.JSONDecodeError as e:
            log("MCP sqs_send_message JSON parse failed", {"error": str(e)})
            return json.dumps({"success": False, "error": f"Invalid JSON: {str(e)}"})
        except Exception as e:
            log("MCP sqs_send_message failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def webhook_send(
        payload: str,
        method: str = "POST",
        webhook_url: str = "",
        enrich: bool = False,
    ) -> str:
        """Send HTTP request to configured webhook URL.

        Sends JSON payload to webhook endpoint with optional state enrichment.
        Uses header-based authentication if WEBHOOK_AUTH_TOKEN is configured.

        Args:
            payload: JSON string with data to send
            method: HTTP method (default: POST)
            webhook_url: Custom URL (default: from WEBHOOK_URL env var)
            enrich: If True, enriches payload with state from conversation_map.json (default: False)

        Returns:
            JSON with success status, status_code, and response_body
        """
        try:
            from hooks.integrations.webhook import send as http_send

            payload_dict = json.loads(payload)

            result = http_send(
                payload=payload_dict,
                method=method,
                webhook_url=webhook_url if webhook_url else None,
                enrich_from_state=enrich,
            )

            return json.dumps(
                {
                    "success": result.success,
                    "status_code": result.status_code,
                    "response_body": result.response_body,
                    "webhook_url": result.webhook_url,
                    "error": result.error,
                }
            )

        except json.JSONDecodeError as e:
            log("MCP webhook_send JSON parse failed", {"error": str(e)})
            return json.dumps({"success": False, "error": f"Invalid JSON: {str(e)}"})
        except Exception as e:
            log("MCP webhook_send failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
