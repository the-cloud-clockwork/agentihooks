#!/usr/bin/env python3
"""AWS DynamoDB integration for storing payloads from hooks.

Writes JSON payloads to DynamoDB tables with configurable partition and sort keys.
Supports both simple (PK only) and composite (PK + SK) key schemas.

Environment Variables:
    DYNAMODB_TABLE_NAME: DynamoDB table name (required)
    DYNAMODB_PARTITION_KEY: Partition key attribute name (default: session_id)
    DYNAMODB_SORT_KEY: Sort key attribute name (optional, for composite keys)
    DYNAMODB_ENDPOINT_URL: Custom endpoint URL (for LocalStack testing)
    AWS_REGION: AWS region (optional, uses default chain)
    IS_EVALUATION: Skip write in evaluation mode (default: false)

Usage:
    put_item({"session_id": "uuid-123", "status": "success"}, enrich_from_state=True)
    dynamodb.py check [--json]
    dynamodb.py hook [--demo] < stop_event.json
    dynamodb.py put --data '{"session_id": "uuid-123", "key": "value"}'
"""

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_script_dir = Path(__file__).resolve().parent
_app_dir = _script_dir.parent.parent
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

try:
    import boto3
    from botocore.config import Config  # noqa: F401

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

from hooks.common import get_correlation_id, log
from hooks.integrations.base import IntegrationBase, IntegrationRegistry


@IntegrationRegistry.register
class DynamoDBIntegration(IntegrationBase):
    """DynamoDB integration configuration checker."""

    INTEGRATION_NAME = "dynamodb"
    REQUIRED_ENV_VARS = {
        "DYNAMODB_TABLE_NAME": "DynamoDB table name",
    }
    OPTIONAL_ENV_VARS = {
        "DYNAMODB_PARTITION_KEY": "Partition key attribute name (default: session_id)",
        "DYNAMODB_SORT_KEY": "Sort key attribute name (optional, for composite keys)",
        "DYNAMODB_ENDPOINT_URL": "Custom endpoint URL (for LocalStack testing)",
        "AWS_REGION": "AWS region for DynamoDB",
        "IS_EVALUATION": "Skip write in evaluation mode (default: false)",
    }


DEFAULT_STATE_FILE = Path.home() / "conversation_map.json"
DEFAULT_PARTITION_KEY = "session_id"


@dataclass
class DynamoDBResult:
    """Result of DynamoDB put_item operation."""

    success: bool
    table_name: Optional[str] = None
    partition_key: Optional[str] = None
    partition_key_value: Optional[str] = None
    sort_key: Optional[str] = None
    sort_key_value: Optional[str] = None
    error: Optional[str] = None


def load_state(session_id: str, state_file: Optional[Path] = None) -> Dict[str, Any]:
    """Load state data for a session from conversation_map.json.

    A legacy state stored as a plain string is converted to a dict.

    Uses correlation ID (external UUID) for lookup since the API stores state
    under the external UUID, which may differ from Claude's session ID.

    Args:
        session_id: Session UUID to look up (will be mapped to correlation ID)
        state_file: Custom state file path (default: ~/conversation_map.json)

    Returns:
        Dict with state data, or empty dict if not found
    """
    state_path = state_file or DEFAULT_STATE_FILE

    try:
        if not state_path.exists():
            return {}

        correlation_id = get_correlation_id(session_id)

        mappings = json.loads(state_path.read_text())
        state = mappings.get(correlation_id, {})

        if isinstance(state, str):
            return {"conversation_id": state, "wait": False}

        return state if isinstance(state, dict) else {}

    except Exception as e:
        log("Failed to load state", {"session_id": session_id, "error": str(e)})
        return {}


class DynamoDBClient:
    """AWS DynamoDB client using boto3."""

    _instance: Optional["DynamoDBClient"] = None

    def __init__(
        self,
        table_name: Optional[str] = None,
        partition_key: Optional[str] = None,
        sort_key: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        skip_evaluation: bool = True,
        state_file: Optional[Path] = None,
    ):
        """Initialize DynamoDB client.

        Args:
            table_name: DynamoDB table name (default: from env)
            partition_key: Partition key attribute name (default: from env or session_id)
            sort_key: Sort key attribute name (default: from env, optional)
            endpoint_url: Custom endpoint URL (for LocalStack)
            skip_evaluation: Skip write when IS_EVALUATION=true (default: True)
            state_file: Custom state file path (default: ~/conversation_map.json)
        """
        self._table_name = table_name or os.getenv("DYNAMODB_TABLE_NAME", "")
        self._partition_key = partition_key or os.getenv("DYNAMODB_PARTITION_KEY", DEFAULT_PARTITION_KEY)
        self._sort_key = sort_key or os.getenv("DYNAMODB_SORT_KEY", "")
        self._endpoint_url = endpoint_url or os.getenv("DYNAMODB_ENDPOINT_URL", "")
        self._skip_evaluation = skip_evaluation
        self._state_file = state_file or DEFAULT_STATE_FILE
        self._dynamodb_client = None
        self._dynamodb_resource = None

        self._is_evaluation = os.getenv("IS_EVALUATION", "false").lower() == "true"

    @classmethod
    def get_client(
        cls,
        table_name: Optional[str] = None,
        partition_key: Optional[str] = None,
        sort_key: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        skip_evaluation: bool = True,
        state_file: Optional[Path] = None,
    ) -> "DynamoDBClient":
        """Get singleton instance."""
        if cls._instance is None or table_name or state_file:
            cls._instance = cls(
                table_name=table_name,
                partition_key=partition_key,
                sort_key=sort_key,
                endpoint_url=endpoint_url,
                skip_evaluation=skip_evaluation,
                state_file=state_file,
            )
        return cls._instance

    @classmethod
    def clear_cache(cls) -> None:
        """Clear cached instance (for testing)."""
        cls._instance = None

    @property
    def table(self):
        """Lazy-load boto3 DynamoDB table resource."""
        if self._dynamodb_resource is None:
            if not BOTO3_AVAILABLE:
                raise ImportError("boto3 is required for DynamoDB integration")

            kwargs = {}
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url

            self._dynamodb_resource = boto3.resource("dynamodb", **kwargs)

        return self._dynamodb_resource.Table(self._table_name)

    def should_skip(self) -> bool:
        """Check if write should be skipped."""
        if not self._table_name:
            log(
                "DynamoDB put_item skipped: DYNAMODB_TABLE_NAME not configured",
                {"integration": "dynamodb", "reason": "missing_config", "var": "DYNAMODB_TABLE_NAME"},
            )
            return True

        if self._skip_evaluation and self._is_evaluation:
            log(
                "DynamoDB put_item skipped: IS_EVALUATION=true",
                {"integration": "dynamodb", "reason": "evaluation_mode"},
            )
            return True

        return False

    def put_item(
        self,
        payload: Dict[str, Any],
        enrich_from_state: bool = False,
    ) -> DynamoDBResult:
        """Write an item to DynamoDB table.

        State fields fill gaps in the payload and payload keys win. A state with
        wait true skips the write, because the caller takes a synchronous
        response. A missing sort key value defaults to the current UTC time.
        Failures are logged and returned, never raised.

        Args:
            payload: Item data to write (must contain partition key)
            enrich_from_state: If True and payload has 'session_id',
                              merges data from conversation_map.json

        Returns:
            DynamoDBResult with success status and key info
        """
        if self.should_skip():
            return DynamoDBResult(
                success=True,
                table_name=self._table_name or "not-configured",
            )

        try:
            enriched_payload = payload.copy()

            if enrich_from_state and "session_id" in payload:
                session_id = payload["session_id"]
                state = load_state(session_id, self._state_file)

                if state.get("wait", False):
                    log(
                        "Skipping DynamoDB put_item",
                        {
                            "reason": "wait=true (synchronous response)",
                            "session_id": session_id,
                        },
                    )
                    return DynamoDBResult(
                        success=True,
                        table_name=self._table_name,
                    )

                enriched_payload = {**state, **payload}

                log(
                    "Enriched payload with state",
                    {
                        "session_id": session_id,
                        "state_fields": list(state.keys()),
                    },
                )

            pk_value = enriched_payload.get(self._partition_key)
            if not pk_value:
                error_msg = f"Partition key '{self._partition_key}' not found in payload"
                log("DynamoDB put_item failed", {"error": error_msg})
                return DynamoDBResult(
                    success=False,
                    table_name=self._table_name,
                    partition_key=self._partition_key,
                    error=error_msg,
                )

            sk_value = None
            if self._sort_key:
                sk_value = enriched_payload.get(self._sort_key)
                if not sk_value:
                    sk_value = datetime.now(timezone.utc).isoformat()
                    enriched_payload[self._sort_key] = sk_value
                    log(
                        "Auto-generated sort key value",
                        {"sort_key": self._sort_key, "value": sk_value},
                    )

            enriched_payload["_created_at"] = datetime.now(timezone.utc).isoformat()
            enriched_payload["_integration"] = "dynamodb"

            log(
                "Writing to DynamoDB",
                {
                    "table_name": self._table_name,
                    "partition_key": self._partition_key,
                    "partition_key_value": str(pk_value)[:50],
                    "sort_key": self._sort_key or None,
                    "sort_key_value": str(sk_value)[:50] if sk_value else None,
                    "item_size": len(json.dumps(enriched_payload)),
                },
            )

            self.table.put_item(Item=enriched_payload)

            log(
                "DynamoDB put_item succeeded",
                {
                    "table_name": self._table_name,
                    "partition_key_value": str(pk_value)[:50],
                },
            )

            return DynamoDBResult(
                success=True,
                table_name=self._table_name,
                partition_key=self._partition_key,
                partition_key_value=str(pk_value),
                sort_key=self._sort_key or None,
                sort_key_value=str(sk_value) if sk_value else None,
            )

        except Exception as e:
            error_msg = str(e)
            log(
                "DynamoDB put_item failed",
                {
                    "error": error_msg,
                    "table_name": self._table_name,
                },
            )

            return DynamoDBResult(
                success=False,
                table_name=self._table_name,
                error=error_msg,
            )

    def query_items(
        self,
        partition_key_value: str,
        partition_key_name: Optional[str] = None,
        limit: int = 10,
        scan_forward: bool = False,
    ) -> List[Dict[str, Any]]:
        """Query DynamoDB by partition key.

        Args:
            partition_key_value: Value for partition key
            partition_key_name: Attribute name (default: from env or configured value)
            limit: Max items to return
            scan_forward: True=ascending, False=descending (newest first)

        Returns:
            List of items (empty list on error or not found)
        """
        if not self._table_name:
            log(
                "DynamoDB query skipped: DYNAMODB_TABLE_NAME not configured",
                {"integration": "dynamodb", "reason": "missing_config"},
            )
            return []

        try:
            pk_name = partition_key_name or self._partition_key

            log(
                "Querying DynamoDB",
                {
                    "table_name": self._table_name,
                    "partition_key": pk_name,
                    "partition_key_value": str(partition_key_value)[:50],
                    "limit": limit,
                    "scan_forward": scan_forward,
                },
            )

            response = self.table.query(
                KeyConditionExpression=f"{pk_name} = :pk",
                ExpressionAttributeValues={":pk": partition_key_value},
                Limit=limit,
                ScanIndexForward=scan_forward,
            )

            items = response.get("Items", [])
            log(
                "DynamoDB query succeeded",
                {
                    "table_name": self._table_name,
                    "items_returned": len(items),
                },
            )

            return items

        except Exception as e:
            log(
                "DynamoDB query failed",
                {
                    "error": str(e),
                    "table_name": self._table_name,
                },
            )
            return []


def put_item(
    payload: Dict[str, Any],
    table_name: Optional[str] = None,
    partition_key: Optional[str] = None,
    sort_key: Optional[str] = None,
    enrich_from_state: bool = False,
    state_file: Optional[Path] = None,
) -> DynamoDBResult:
    """Write an item to DynamoDB table.

    Convenience function that uses singleton client.

    Args:
        payload: Item data to write (must contain partition key)
        table_name: DynamoDB table name (default: from env)
        partition_key: Partition key attribute name (default: from env or session_id)
        sort_key: Sort key attribute name (default: from env, optional)
        enrich_from_state: If True and payload has 'session_id',
                          merges data from conversation_map.json
        state_file: Custom state file path (default: ~/conversation_map.json)

    Returns:
        DynamoDBResult with success status and key info

    Examples:
        # Simple write
        result = put_item({"session_id": "uuid-123", "status": "success"})

        # With state enrichment
        result = put_item(
            {"session_id": "uuid-123", "transcript_path": "/path"},
            enrich_from_state=True
        )

        # Custom table and keys
        result = put_item(
            {"pk": "uuid-123", "sk": "2024-01-01", "data": "value"},
            table_name="my-table",
            partition_key="pk",
            sort_key="sk"
        )
    """
    client = DynamoDBClient.get_client(
        table_name=table_name,
        partition_key=partition_key,
        sort_key=sort_key,
        state_file=state_file,
    )
    return client.put_item(payload, enrich_from_state=enrich_from_state)


def query_items(
    partition_key_value: str,
    partition_key_name: Optional[str] = None,
    limit: int = 10,
    scan_forward: bool = False,
    table_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query DynamoDB by partition key.

    Convenience function that uses singleton client.

    Args:
        partition_key_value: Value for partition key
        partition_key_name: Attribute name (default: from env or "PK")
        limit: Max items to return
        scan_forward: True=ascending, False=descending (newest first)
        table_name: Override table name (default: from env)

    Returns:
        List of items (empty list on error or not found)

    Examples:
        # Query by partition key
        items = query_items("ARTIFACT#uuid-123")

        # Query with custom PK name and limit
        items = query_items(
            partition_key_value="USER#john",
            partition_key_name="PK",
            limit=5,
        )
    """
    client = DynamoDBClient.get_client(
        table_name=table_name,
        partition_key=partition_key_name,
    )
    return client.query_items(
        partition_key_value=partition_key_value,
        partition_key_name=partition_key_name,
        limit=limit,
        scan_forward=scan_forward,
    )


def poll_for_item(
    partition_key_value: str,
    partition_key_name: Optional[str] = None,
    ready_field: str = "status",
    ready_value: str = "ready",
    timeout: float = 60.0,
    interval: float = 2.0,
    table_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Poll DynamoDB until an item with the specified ready condition is found.

    Polls the table at regular intervals until an item is found where
    the ready_field equals ready_value, or until timeout is reached.

    Args:
        partition_key_value: Value to query
        partition_key_name: Attribute name (default: from env or "PK")
        ready_field: Field to check for readiness (default: "status")
        ready_value: Value that indicates ready (default: "ready")
        timeout: Max time to wait in seconds (default: 60.0)
        interval: Time between polls in seconds (default: 2.0)
        table_name: Override table name

    Returns:
        The item dict if found and ready, None on timeout

    Examples:
        # Poll for an artifact to be ready
        item = poll_for_item(
            partition_key_value="ARTIFACT#uuid-123",
            partition_key_name="PK",
            ready_field="status",
            ready_value="ready",
            timeout=60.0,
        )

        if item:
            print(f"Found: {item.get('signed_urls')}")
        else:
            print("Timeout waiting for item")
    """
    start = time.time()
    attempts = 0

    while time.time() - start < timeout:
        attempts += 1
        items = query_items(
            partition_key_value=partition_key_value,
            partition_key_name=partition_key_name,
            limit=1,
            scan_forward=False,
            table_name=table_name,
        )

        if items:
            item = items[0]
            if item.get(ready_field) == ready_value:
                elapsed = time.time() - start
                log(
                    "Poll succeeded",
                    {
                        "pk": partition_key_value,
                        "elapsed": round(elapsed, 2),
                        "attempts": attempts,
                    },
                )
                return item

        time.sleep(interval)

    elapsed = time.time() - start
    log(
        "Poll timeout",
        {
            "pk": partition_key_value,
            "timeout": timeout,
            "elapsed": round(elapsed, 2),
            "attempts": attempts,
        },
    )
    return None


def get_last_assistant_response(transcript_path: str) -> Optional[str]:
    """Extract the last assistant response from transcript JSONL."""
    try:
        transcript = Path(transcript_path)
        if not transcript.exists():
            return None

        last_response = None
        with open(transcript, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("type") == "assistant":
                        message = entry.get("message", {})
                        content = message.get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                last_response = block.get("text")
                except json.JSONDecodeError:
                    continue
        return last_response
    except Exception:
        return None


def main():
    """CLI entry point for DynamoDB operations; hook warns on stdout, where Claude Code shows it, and exits 0."""
    if len(sys.argv) < 2:
        print("Usage: /app/hooks/integrations/dynamodb.py <command> [args]")
        print("")
        print("Commands:")
        print("  check                    Check configuration status")
        print("  check --json             Output status as JSON")
        print("  hook [--demo]            Read Stop event from stdin, write to DynamoDB")
        print("                           --demo: Test mode, prints message and exits")
        print("  put --data <json>        Write JSON item to DynamoDB")
        print("  put --data <json> --enrich  Write with state enrichment")
        print("  query --pk <value>       Query items by partition key")
        print("  poll --pk <value>        Poll until item is ready")
        print("")
        print("Query Options:")
        print("  --pk <value>             Partition key value (required)")
        print("  --pk-name <name>         Partition key attribute name (default: from env)")
        print("  --limit <n>              Max items to return (default: 10)")
        print("  --asc                    Sort ascending (default: descending)")
        print("")
        print("Poll Options:")
        print("  --pk <value>             Partition key value (required)")
        print("  --pk-name <name>         Partition key attribute name (default: from env)")
        print("  --ready-field <name>     Field to check for readiness (default: status)")
        print("  --ready-value <value>    Value that indicates ready (default: ready)")
        print("  --timeout <seconds>      Max wait time (default: 60)")
        print("  --interval <seconds>     Time between polls (default: 2)")
        print("")
        print("Environment Variables:")
        print("  DYNAMODB_TABLE_NAME     DynamoDB table name (required)")
        print("  DYNAMODB_PARTITION_KEY  Partition key attribute (default: session_id)")
        print("  DYNAMODB_SORT_KEY       Sort key attribute (optional)")
        print("  DYNAMODB_ENDPOINT_URL   Custom endpoint (for LocalStack)")
        print("  AWS_REGION              AWS region")
        print("  IS_EVALUATION           Skip write in evaluation mode")
        print("")
        print("Examples:")
        print("  # Check configuration")
        print("  /app/hooks/integrations/dynamodb.py check")
        print("")
        print("  # As Claude Code hook")
        print("  /app/hooks/integrations/dynamodb.py hook < stop_event.json")
        print("")
        print("  # Test mode (no actual write)")
        print("  /app/hooks/integrations/dynamodb.py hook --demo")
        print("")
        print("  # Manual testing")
        print('  export DYNAMODB_TABLE_NAME="my-table"')
        print('  /app/hooks/integrations/dynamodb.py put --data \'{"session_id": "uuid-123", "status": "ok"}\'')
        print("")
        print("  # Query items")
        print('  /app/hooks/integrations/dynamodb.py query --pk "ARTIFACT#uuid-123" --limit 5')
        print("")
        print("  # Poll until ready")
        print('  /app/hooks/integrations/dynamodb.py poll --pk "ARTIFACT#uuid-123" --timeout 60')
        sys.exit(1)

    command = sys.argv[1]

    if command == "check":
        integration = DynamoDBIntegration()
        as_json = "--json" in sys.argv
        integration.print_status(as_json=as_json)
        sys.exit(0 if integration.is_configured else 1)

    elif command == "hook":
        try:
            if "--demo" in sys.argv:
                log("[DEMO] DynamoDB hook triggered - DynamoDB integration ready")
                sys.exit(0)

            integration = DynamoDBIntegration()
            if not integration.is_configured:
                missing = integration.get_missing_required()
                error_msg = f"DynamoDB hook SKIPPED - missing env vars: {missing}"
                log(error_msg, {"integration": "dynamodb", "missing": missing})
                print(f"\n{'=' * 60}")
                print(f"[DYNAMODB] ERROR: {error_msg}")
                print(f"{'=' * 60}\n")
                sys.exit(0)

            payload = json.load(sys.stdin)

            transcript_path = payload.get("transcript_path", "")
            if transcript_path:
                last_response = get_last_assistant_response(transcript_path)
                if last_response:
                    payload["last_response"] = last_response

            payload["integration"] = "dynamodb"

            put_item(payload, enrich_from_state=True)
            sys.exit(0)
        except Exception:
            sys.exit(0)

    elif command == "put":
        data_json = None
        enrich = "--enrich" in sys.argv or "-e" in sys.argv

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--data" and i + 1 < len(sys.argv):
                data_json = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not data_json:
            print("Error: Missing --data flag")
            print("Usage: /app/hooks/integrations/dynamodb.py put --data <json> [--enrich]")
            sys.exit(1)

        try:
            payload = json.loads(data_json)

            if not isinstance(payload, dict):
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": "Payload must be a JSON object/dict",
                        }
                    )
                )
                sys.exit(1)

            result = put_item(payload, enrich_from_state=enrich)

            print(
                json.dumps(
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
            )

            sys.exit(0 if result.success else 1)

        except json.JSONDecodeError as e:
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": f"Invalid JSON: {str(e)}",
                    }
                )
            )
            sys.exit(1)

        except Exception as e:
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": str(e),
                    }
                )
            )
            sys.exit(1)

    elif command == "query":
        pk_value = None
        pk_name = None
        limit = 10
        scan_forward = False

        args = sys.argv[2:]
        i = 0
        while i < len(args):
            if args[i] == "--pk" and i + 1 < len(args):
                pk_value = args[i + 1]
                i += 2
            elif args[i] == "--pk-name" and i + 1 < len(args):
                pk_name = args[i + 1]
                i += 2
            elif args[i] == "--limit" and i + 1 < len(args):
                limit = int(args[i + 1])
                i += 2
            elif args[i] == "--asc":
                scan_forward = True
                i += 1
            else:
                i += 1

        if not pk_value:
            print(json.dumps({"success": False, "error": "Missing --pk flag"}))
            sys.exit(1)

        try:
            items = query_items(
                partition_key_value=pk_value,
                partition_key_name=pk_name,
                limit=limit,
                scan_forward=scan_forward,
            )

            print(
                json.dumps(
                    {
                        "success": True,
                        "count": len(items),
                        "items": items,
                    },
                    indent=2,
                    default=str,
                )
            )
            sys.exit(0)

        except Exception as e:
            print(json.dumps({"success": False, "error": str(e)}))
            sys.exit(1)

    elif command == "poll":
        pk_value = None
        pk_name = None
        ready_field = "status"
        ready_value = "ready"
        timeout = 60.0
        interval = 2.0

        args = sys.argv[2:]
        i = 0
        while i < len(args):
            if args[i] == "--pk" and i + 1 < len(args):
                pk_value = args[i + 1]
                i += 2
            elif args[i] == "--pk-name" and i + 1 < len(args):
                pk_name = args[i + 1]
                i += 2
            elif args[i] == "--ready-field" and i + 1 < len(args):
                ready_field = args[i + 1]
                i += 2
            elif args[i] == "--ready-value" and i + 1 < len(args):
                ready_value = args[i + 1]
                i += 2
            elif args[i] == "--timeout" and i + 1 < len(args):
                timeout = float(args[i + 1])
                i += 2
            elif args[i] == "--interval" and i + 1 < len(args):
                interval = float(args[i + 1])
                i += 2
            else:
                i += 1

        if not pk_value:
            print(json.dumps({"success": False, "error": "Missing --pk flag"}))
            sys.exit(1)

        try:
            item = poll_for_item(
                partition_key_value=pk_value,
                partition_key_name=pk_name,
                ready_field=ready_field,
                ready_value=ready_value,
                timeout=timeout,
                interval=interval,
            )

            if item:
                print(
                    json.dumps(
                        {
                            "success": True,
                            "item": item,
                        },
                        indent=2,
                        default=str,
                    )
                )
                sys.exit(0)
            else:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": "timeout",
                            "pk": pk_value,
                            "timeout": timeout,
                        }
                    )
                )
                sys.exit(1)

        except Exception as e:
            print(json.dumps({"success": False, "error": str(e)}))
            sys.exit(1)

    else:
        print(f"Error: Unknown command '{command}'")
        print("Usage: /app/hooks/integrations/dynamodb.py <command> [args]")
        sys.exit(1)


if __name__ == "__main__":
    main()
