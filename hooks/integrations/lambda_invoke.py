#!/usr/bin/env python3
"""AWS Lambda integration for invoking functions with payloads from hooks.

Invokes Lambda functions with JSON payloads, supporting both synchronous
(RequestResponse) and asynchronous (Event) invocation types.

Environment Variables:
    LAMBDA_FUNCTION_NAME: Lambda function ARN or name (required)
    LAMBDA_INVOCATION_TYPE: RequestResponse waits for the response (default),
                            Event fires and forgets
    AWS_REGION: AWS region (optional, uses default chain)
    IS_EVALUATION: Skip invocation in evaluation mode (default: false)

Usage:
    invoke({"session_id": "uuid-123"}, async_invoke=True, enrich_from_state=True)
    lambda_invoke.py check [--json]
    lambda_invoke.py hook [--demo] < stop_event.json
    lambda_invoke.py invoke '{"key": "value"}' [--async]
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

_script_dir = Path(__file__).resolve().parent
_app_dir = _script_dir.parent.parent
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

try:
    import boto3

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

from hooks.common import get_correlation_id, log
from hooks.integrations.base import IntegrationBase, IntegrationRegistry


@IntegrationRegistry.register
class LambdaIntegration(IntegrationBase):
    """Lambda integration configuration checker."""

    INTEGRATION_NAME = "lambda"
    REQUIRED_ENV_VARS = {
        "LAMBDA_FUNCTION_NAME": "Lambda function ARN or name",
    }
    OPTIONAL_ENV_VARS = {
        "LAMBDA_INVOCATION_TYPE": "InvocationType (RequestResponse or Event, default: RequestResponse)",
        "AWS_REGION": "AWS region for Lambda",
        "IS_EVALUATION": "Skip invocation in evaluation mode (default: false)",
    }


DEFAULT_STATE_FILE = Path.home() / "conversation_map.json"


@dataclass
class LambdaResult:
    """Result of Lambda invocation operation."""

    success: bool
    status_code: Optional[int] = None
    response_payload: Optional[Dict[str, Any]] = None
    function_name: Optional[str] = None
    invocation_type: Optional[str] = None
    error: Optional[str] = None


def load_state(session_id: str, state_file: Optional[Path] = None) -> Dict[str, Any]:
    """Load state data for a session from conversation_map.json.

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


class LambdaClient:
    """AWS Lambda client using boto3."""

    _instance: Optional["LambdaClient"] = None

    def __init__(
        self,
        function_name: Optional[str] = None,
        invocation_type: Optional[str] = None,
        skip_evaluation: bool = True,
        state_file: Optional[Path] = None,
    ):
        """Initialize Lambda client.

        Args:
            function_name: Lambda function ARN or name (default: from env)
            invocation_type: InvocationType (default: from env or RequestResponse)
            skip_evaluation: Skip invocation when IS_EVALUATION=true (default: True)
            state_file: Custom state file path (default: ~/conversation_map.json)
        """
        self._function_name = function_name or os.getenv("LAMBDA_FUNCTION_NAME", "")
        self._invocation_type = invocation_type or os.getenv("LAMBDA_INVOCATION_TYPE", "RequestResponse")
        self._skip_evaluation = skip_evaluation
        self._state_file = state_file or DEFAULT_STATE_FILE
        self._lambda_client = None

        self._is_evaluation = os.getenv("IS_EVALUATION", "false").lower() == "true"

    @classmethod
    def get_client(
        cls,
        function_name: Optional[str] = None,
        invocation_type: Optional[str] = None,
        skip_evaluation: bool = True,
        state_file: Optional[Path] = None,
    ) -> "LambdaClient":
        """Get singleton instance."""
        if cls._instance is None or function_name or state_file:
            cls._instance = cls(
                function_name=function_name,
                invocation_type=invocation_type,
                skip_evaluation=skip_evaluation,
                state_file=state_file,
            )
        return cls._instance

    @classmethod
    def clear_cache(cls) -> None:
        """Clear cached instance (for testing)."""
        cls._instance = None

    @property
    def lambda_client(self):
        """Lazy-load boto3 Lambda client."""
        if self._lambda_client is None:
            if not BOTO3_AVAILABLE:
                raise ImportError("boto3 is required for Lambda integration")
            self._lambda_client = boto3.client("lambda")
        return self._lambda_client

    def should_skip(self) -> bool:
        """Check if invocation should be skipped."""
        if not self._function_name:
            log(
                "Lambda invoke skipped: LAMBDA_FUNCTION_NAME not configured",
                {"integration": "lambda", "reason": "missing_config", "var": "LAMBDA_FUNCTION_NAME"},
            )
            return True

        if self._skip_evaluation and self._is_evaluation:
            log(
                "Lambda invoke skipped: IS_EVALUATION=true",
                {"integration": "lambda", "reason": "evaluation_mode"},
            )
            return True

        return False

    def invoke(
        self,
        payload: Dict[str, Any],
        async_invoke: bool = False,
        enrich_from_state: bool = False,
    ) -> LambdaResult:
        """Invoke a Lambda function with the given payload.

        State fields fill gaps in the payload and payload keys win. A state with
        wait true skips the call, because the caller takes a synchronous
        response. A sync call succeeds on status 200, an async one on 202.
        Failures are logged and returned, never raised, because hooks must never
        crash the parent process.

        Args:
            payload: Event data to send to Lambda
            async_invoke: If True, use Event invocation type (fire and forget)
            enrich_from_state: If True and payload has 'session_id',
                              merges data from conversation_map.json

        Returns:
            LambdaResult with success status and response info
        """
        if self.should_skip():
            return LambdaResult(
                success=True,
                function_name=self._function_name or "not-configured",
            )

        try:
            enriched_payload = payload.copy()

            if enrich_from_state and "session_id" in payload:
                session_id = payload["session_id"]
                state = load_state(session_id, self._state_file)

                if state.get("wait", False):
                    log(
                        "Skipping Lambda invoke",
                        {
                            "reason": "wait=true (synchronous response)",
                            "session_id": session_id,
                        },
                    )
                    return LambdaResult(
                        success=True,
                        function_name=self._function_name,
                    )

                enriched_payload = {**state, **payload}

                log(
                    "Enriched payload with state",
                    {
                        "session_id": session_id,
                        "state_fields": list(state.keys()),
                    },
                )

            invocation_type = "Event" if async_invoke else self._invocation_type

            log(
                "Invoking Lambda function",
                {
                    "function_name": self._function_name,
                    "invocation_type": invocation_type,
                    "payload_size": len(json.dumps(enriched_payload)),
                },
            )

            response = self.lambda_client.invoke(
                FunctionName=self._function_name,
                InvocationType=invocation_type,
                Payload=json.dumps(enriched_payload),
            )

            status_code = response.get("StatusCode", 0)
            success = status_code in (200, 202)

            response_payload = None
            error = None

            if invocation_type == "RequestResponse":
                response_data = response.get("Payload")
                if response_data:
                    try:
                        response_payload = json.loads(response_data.read())
                    except Exception:  # NOSONAR
                        pass

                if "FunctionError" in response:
                    success = False
                    error = response.get("FunctionError")
                    if response_payload and isinstance(response_payload, dict):
                        error = response_payload.get("errorMessage", error)

            log(
                "Lambda invoke completed",
                {
                    "status_code": status_code,
                    "success": success,
                    "function_name": self._function_name,
                    "invocation_type": invocation_type,
                },
            )

            return LambdaResult(
                success=success,
                status_code=status_code,
                response_payload=response_payload,
                function_name=self._function_name,
                invocation_type=invocation_type,
                error=error,
            )

        except Exception as e:
            error_msg = str(e)
            log(
                "Lambda invoke failed",
                {
                    "error": error_msg,
                    "function_name": self._function_name,
                },
            )

            return LambdaResult(
                success=False,
                function_name=self._function_name,
                error=error_msg,
            )


def invoke(
    payload: Dict[str, Any],
    function_name: Optional[str] = None,
    async_invoke: bool = False,
    enrich_from_state: bool = False,
    state_file: Optional[Path] = None,
) -> LambdaResult:
    """Invoke a Lambda function with the given payload.

    Convenience function that uses singleton client.

    Args:
        payload: Event data to send to Lambda
        function_name: Lambda function ARN or name (default: from env)
        async_invoke: If True, use Event invocation type (fire and forget)
        enrich_from_state: If True and payload has 'session_id',
                          merges data from conversation_map.json
        state_file: Custom state file path (default: ~/conversation_map.json)

    Returns:
        LambdaResult with success status and response info

    Examples:
        # Synchronous invocation
        result = invoke({"event": "completion", "status": "success"})

        # Asynchronous invocation
        result = invoke({"session_id": "uuid-123"}, async_invoke=True)

        # With state enrichment
        result = invoke(
            {"session_id": "uuid-123", "transcript_path": "/path"},
            enrich_from_state=True
        )
    """
    client = LambdaClient.get_client(function_name=function_name, state_file=state_file)
    return client.invoke(payload, async_invoke=async_invoke, enrich_from_state=enrich_from_state)


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
    """CLI entry point for Lambda operations."""
    if len(sys.argv) < 2:
        print("Usage: /app/hooks/integrations/lambda_invoke.py <command> [args]")
        print("")
        print("Commands:")
        print("  check                    Check configuration status")
        print("  check --json             Output status as JSON")
        print("  hook [--demo]            Read Stop event from stdin, invoke Lambda")
        print("                           --demo: Test mode, prints message and exits")
        print("  invoke <json>            Invoke Lambda with JSON payload")
        print("  invoke <json> --async    Invoke asynchronously (fire and forget)")
        print("  invoke <json> --enrich   Invoke with state enrichment")
        print("")
        print("Environment Variables:")
        print("  LAMBDA_FUNCTION_NAME    Lambda function ARN or name (required)")
        print("  LAMBDA_INVOCATION_TYPE  InvocationType (default: RequestResponse)")
        print("  AWS_REGION              AWS region")
        print("  IS_EVALUATION           Skip invocation in evaluation mode")
        print("")
        print("Examples:")
        print("  # Check configuration")
        print("  /app/hooks/integrations/lambda_invoke.py check")
        print("")
        print("  # As Claude Code hook")
        print("  /app/hooks/integrations/lambda_invoke.py hook < stop_event.json")
        print("")
        print("  # Test mode (no actual invocation)")
        print("  /app/hooks/integrations/lambda_invoke.py hook --demo")
        print("")
        print("  # Manual testing")
        print('  export LAMBDA_FUNCTION_NAME="my-function"')
        print('  /app/hooks/integrations/lambda_invoke.py invoke \'{"event": "test"}\'')
        print('  /app/hooks/integrations/lambda_invoke.py invoke \'{"event": "test"}\' --async')
        sys.exit(1)

    command = sys.argv[1]

    if command == "check":
        integration = LambdaIntegration()
        as_json = "--json" in sys.argv
        integration.print_status(as_json=as_json)
        sys.exit(0 if integration.is_configured else 1)

    elif command == "hook":
        try:
            if "--demo" in sys.argv:
                log("[DEMO] Lambda hook triggered - Lambda integration ready")
                sys.exit(0)

            integration = LambdaIntegration()
            if not integration.is_configured:
                missing = integration.get_missing_required()
                error_msg = f"Lambda hook SKIPPED - missing env vars: {missing}"
                log(error_msg, {"integration": "lambda", "missing": missing})
                print(f"\n{'=' * 60}")
                print(f"[LAMBDA] ERROR: {error_msg}")
                print(f"{'=' * 60}\n")
                sys.exit(0)

            payload = json.load(sys.stdin)

            transcript_path = payload.get("transcript_path", "")
            if transcript_path:
                last_response = get_last_assistant_response(transcript_path)
                if last_response:
                    payload["last_response"] = last_response

            payload["integration"] = "lambda"

            invoke(payload, async_invoke=True, enrich_from_state=True)
            sys.exit(0)
        except Exception:
            sys.exit(0)

    elif command == "invoke":
        if len(sys.argv) < 3:
            print("Error: Missing JSON payload")
            print("Usage: /app/hooks/integrations/lambda_invoke.py invoke <json> [--async] [--enrich]")
            sys.exit(1)

        json_str = sys.argv[2]
        async_invoke = "--async" in sys.argv or "-a" in sys.argv
        enrich = "--enrich" in sys.argv or "-e" in sys.argv

        try:
            payload = json.loads(json_str)

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

            result = invoke(payload, async_invoke=async_invoke, enrich_from_state=enrich)

            print(
                json.dumps(
                    {
                        "success": result.success,
                        "status_code": result.status_code,
                        "function_name": result.function_name,
                        "invocation_type": result.invocation_type,
                        "response_payload": result.response_payload,
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

    else:
        print(f"Error: Unknown command '{command}'")
        print("Usage: /app/hooks/integrations/lambda_invoke.py <command> [args]")
        sys.exit(1)


if __name__ == "__main__":
    main()
