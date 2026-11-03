#!/usr/bin/env python3
"""PostgreSQL integration for storing payloads from hooks.

Writes JSON payloads to PostgreSQL tables using psycopg2 (synchronous).
Supports both JSONB column storage and parameterized SQL queries.

Environment Variables:
    POSTGRES_HOST: PostgreSQL server hostname (required)
    POSTGRES_NAME: Database name (required)
    POSTGRES_USERNAME: Database username (required)
    POSTGRES_PASSWORD: Database password (required)
    POSTGRES_PORT: Database port (default: 5432)
    POSTGRES_TABLE: Default table for hook inserts (optional)
    IS_EVALUATION: Skip write in evaluation mode (default: false)

Usage:
    insert("hook_events", {"session_id": "uuid-123", "status": "success"}, enrich_from_state=True)
    postgres.py check [--json]
    postgres.py hook [--table hook_events] [--demo] < stop_event.json
    postgres.py insert --table logs --data '{"session_id": "...", "message": "..."}'
"""

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_script_dir = Path(__file__).resolve().parent
_app_dir = _script_dir.parent.parent
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

try:
    import psycopg2
    from psycopg2.extras import Json

    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    Json = None  # type: ignore

from hooks.common import get_correlation_id, log
from hooks.integrations.base import IntegrationBase, IntegrationRegistry


@IntegrationRegistry.register
class PostgresIntegration(IntegrationBase):
    """PostgreSQL integration configuration checker."""

    INTEGRATION_NAME = "postgres"
    REQUIRED_ENV_VARS = {
        "POSTGRES_HOST": "PostgreSQL server hostname",
        "POSTGRES_NAME": "Database name",
        "POSTGRES_USERNAME": "Database username",
        "POSTGRES_PASSWORD": "Database password",
    }
    OPTIONAL_ENV_VARS = {
        "POSTGRES_PORT": "Database port (default: 5432)",
        "POSTGRES_TABLE": "Default table for hook inserts",
        "IS_EVALUATION": "Skip write in evaluation mode (default: false)",
    }


DEFAULT_STATE_FILE = Path.home() / "conversation_map.json"
DEFAULT_PORT = 5432


@dataclass
class PostgresResult:
    """Result of PostgreSQL operation."""

    success: bool
    table_name: Optional[str] = None
    rows_affected: int = 0
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


class PostgresClient:
    """PostgreSQL client using psycopg2."""

    _instance: Optional["PostgresClient"] = None

    def __init__(
        self,
        host: Optional[str] = None,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        port: Optional[int] = None,
        default_table: Optional[str] = None,
        skip_evaluation: bool = True,
        state_file: Optional[Path] = None,
    ):
        """Initialize PostgreSQL client.

        Args:
            host: PostgreSQL server hostname (default: from env)
            database: Database name (default: from env)
            username: Database username (default: from env)
            password: Database password (default: from env)
            port: Database port (default: from env or 5432)
            default_table: Default table for inserts (default: from env)
            skip_evaluation: Skip write when IS_EVALUATION=true (default: True)
            state_file: Custom state file path (default: ~/conversation_map.json)
        """
        self._host = host or os.getenv("POSTGRES_HOST", "")
        self._database = database or os.getenv("POSTGRES_NAME", "")
        self._username = username or os.getenv("POSTGRES_USERNAME", "")
        self._password = password or os.getenv("POSTGRES_PASSWORD", "")
        self._port = port or int(os.getenv("POSTGRES_PORT", str(DEFAULT_PORT)))
        self._default_table = default_table or os.getenv("POSTGRES_TABLE", "")
        self._skip_evaluation = skip_evaluation
        self._state_file = state_file or DEFAULT_STATE_FILE
        self._connection = None

        self._is_evaluation = os.getenv("IS_EVALUATION", "false").lower() == "true"

    @classmethod
    def get_client(
        cls,
        host: Optional[str] = None,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        port: Optional[int] = None,
        default_table: Optional[str] = None,
        skip_evaluation: bool = True,
        state_file: Optional[Path] = None,
    ) -> "PostgresClient":
        """Get singleton instance."""
        if cls._instance is None or host or database or state_file:
            cls._instance = cls(
                host=host,
                database=database,
                username=username,
                password=password,
                port=port,
                default_table=default_table,
                skip_evaluation=skip_evaluation,
                state_file=state_file,
            )
        return cls._instance

    @classmethod
    def clear_cache(cls) -> None:
        """Clear cached instance (for testing).

        A failing close is ignored, because hooks must never crash the parent process.
        """
        if cls._instance and cls._instance._connection:
            try:
                cls._instance._connection.close()
            except Exception:  # NOSONAR
                pass
        cls._instance = None

    def _get_connection(self):
        """Get or create database connection."""
        if self._connection is None or self._connection.closed:
            if not PSYCOPG2_AVAILABLE:
                raise ImportError("psycopg2 is required for PostgreSQL integration")

            self._connection = psycopg2.connect(
                host=self._host,
                database=self._database,
                user=self._username,
                password=self._password,
                port=self._port,
                connect_timeout=30,
            )
            self._connection.autocommit = False

        return self._connection

    def should_skip(self) -> bool:
        """Check if write should be skipped."""
        if not all([self._host, self._database, self._username, self._password]):
            missing = []
            if not self._host:
                missing.append("POSTGRES_HOST")
            if not self._database:
                missing.append("POSTGRES_NAME")
            if not self._username:
                missing.append("POSTGRES_USERNAME")
            if not self._password:
                missing.append("POSTGRES_PASSWORD")

            log(
                f"PostgreSQL write skipped: Missing config ({', '.join(missing)})",
                {"integration": "postgres", "reason": "missing_config", "vars": missing},
            )
            return True

        if self._skip_evaluation and self._is_evaluation:
            log(
                "PostgreSQL write skipped: IS_EVALUATION=true",
                {"integration": "postgres", "reason": "evaluation_mode"},
            )
            return True

        return False

    def insert(
        self,
        table: Optional[str],
        payload: Dict[str, Any],
        enrich_from_state: bool = False,
    ) -> PostgresResult:
        """Insert a row into PostgreSQL table as JSONB.

        The table should have a column named 'data' of type JSONB.
        The parameterized insert writes session_id, data and created_at, so the
        table should have id SERIAL, session_id TEXT, data JSONB and created_at
        TIMESTAMPTZ.

        State fields fill gaps in the payload and payload keys win. A state with
        wait true skips the write, because the caller takes a synchronous
        response. Failures roll back and are logged and returned, never raised,
        because hooks must never crash the parent process.

        Args:
            table: Table name (uses default_table if None)
            payload: Data to insert as JSONB
            enrich_from_state: If True and payload has 'session_id',
                              merges data from conversation_map.json

        Returns:
            PostgresResult with success status
        """
        table_name = table or self._default_table

        if self.should_skip():
            return PostgresResult(
                success=True,
                table_name=table_name or "not-configured",
            )

        if not table_name:
            return PostgresResult(
                success=False,
                error="No table specified and POSTGRES_TABLE not set",
            )

        try:
            enriched_payload = payload.copy()

            if enrich_from_state and "session_id" in payload:
                session_id = payload["session_id"]
                state = load_state(session_id, self._state_file)

                if state.get("wait", False):
                    log(
                        "Skipping PostgreSQL insert",
                        {
                            "reason": "wait=true (synchronous response)",
                            "session_id": session_id,
                        },
                    )
                    return PostgresResult(
                        success=True,
                        table_name=table_name,
                    )

                enriched_payload = {**state, **payload}

                log(
                    "Enriched payload with state",
                    {
                        "session_id": session_id,
                        "state_fields": list(state.keys()),
                    },
                )

            enriched_payload["_created_at"] = datetime.now(timezone.utc).isoformat()
            enriched_payload["_integration"] = "postgres"

            log(
                "Inserting into PostgreSQL",
                {
                    "table_name": table_name,
                    "session_id": enriched_payload.get("session_id", "N/A"),
                    "payload_size": len(json.dumps(enriched_payload)),
                },
            )

            conn = self._get_connection()
            cursor = conn.cursor()

            session_id = enriched_payload.get("session_id", "")
            created_at = datetime.now(timezone.utc)

            query = f"""
                INSERT INTO {table_name} (session_id, data, created_at)
                VALUES (%s, %s, %s)
            """

            cursor.execute(query, (session_id, Json(enriched_payload), created_at))
            rows_affected = cursor.rowcount

            conn.commit()
            cursor.close()

            log(
                "PostgreSQL insert succeeded",
                {
                    "table_name": table_name,
                    "rows_affected": rows_affected,
                },
            )

            return PostgresResult(
                success=True,
                table_name=table_name,
                rows_affected=rows_affected,
            )

        except Exception as e:
            if self._connection and not self._connection.closed:
                try:
                    self._connection.rollback()
                except Exception:  # NOSONAR
                    pass

            error_msg = str(e)
            log(
                "PostgreSQL insert failed",
                {
                    "error": error_msg,
                    "table_name": table_name,
                },
            )

            return PostgresResult(
                success=False,
                table_name=table_name,
                error=error_msg,
            )

    def execute(
        self,
        query: str,
        params: Optional[Tuple] = None,
    ) -> PostgresResult:
        """Execute a parameterized SQL query.

        Failures roll back and are returned, never raised, because hooks must
        never crash the parent process.

        Args:
            query: SQL query with %s placeholders
            params: Tuple of parameters for the query

        Returns:
            PostgresResult with success status and rows affected
        """
        if self.should_skip():
            return PostgresResult(
                success=True,
                table_name="query",
            )

        try:
            log(
                "Executing PostgreSQL query",
                {
                    "query_preview": query[:100] + "..." if len(query) > 100 else query,
                    "has_params": params is not None,
                },
            )

            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute(query, params or ())
            rows_affected = cursor.rowcount

            conn.commit()
            cursor.close()

            log(
                "PostgreSQL query succeeded",
                {"rows_affected": rows_affected},
            )

            return PostgresResult(
                success=True,
                rows_affected=rows_affected,
            )

        except Exception as e:
            if self._connection and not self._connection.closed:
                try:
                    self._connection.rollback()
                except Exception:  # NOSONAR
                    pass

            error_msg = str(e)
            log(
                "PostgreSQL query failed",
                {"error": error_msg},
            )

            return PostgresResult(
                success=False,
                error=error_msg,
            )


def insert(
    table: str,
    payload: Dict[str, Any],
    enrich_from_state: bool = False,
    state_file: Optional[Path] = None,
) -> PostgresResult:
    """Insert a row into PostgreSQL table as JSONB.

    Convenience function that uses singleton client.

    Args:
        table: Table name
        payload: Data to insert as JSONB
        enrich_from_state: If True and payload has 'session_id',
                          merges data from conversation_map.json
        state_file: Custom state file path (default: ~/conversation_map.json)

    Returns:
        PostgresResult with success status

    Examples:
        # Simple insert
        result = insert("hook_events", {"session_id": "uuid-123", "status": "success"})

        # With state enrichment
        result = insert(
            "hook_events",
            {"session_id": "uuid-123", "transcript_path": "/path"},
            enrich_from_state=True
        )
    """
    client = PostgresClient.get_client(state_file=state_file)
    return client.insert(table, payload, enrich_from_state=enrich_from_state)


def execute(
    query: str,
    params: Optional[Tuple] = None,
) -> PostgresResult:
    """Execute a parameterized SQL query.

    Convenience function that uses singleton client.

    Args:
        query: SQL query with %s placeholders
        params: Tuple of parameters for the query

    Returns:
        PostgresResult with success status and rows affected

    Examples:
        # Simple query
        result = execute("UPDATE users SET active = TRUE WHERE id = %s", (123,))

        # Insert with parameters
        result = execute(
            "INSERT INTO logs (session_id, message) VALUES (%s, %s)",
            ("uuid-123", "Operation completed")
        )
    """
    client = PostgresClient.get_client()
    return client.execute(query, params)


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
    """CLI entry point for PostgreSQL operations.

    Hook mode prints a missing configuration warning to stdout, where Claude Code
    shows it, and exits 0 on any failure, so a hook never breaks Claude Code.
    """
    if len(sys.argv) < 2:
        print("Usage: /app/hooks/integrations/postgres.py <command> [args]")
        print("")
        print("Commands:")
        print("  check                    Check configuration status")
        print("  check --json             Output status as JSON")
        print("  hook --table <table> [--demo]")
        print("                           Read Stop event from stdin, insert to table")
        print("                           --demo: Test mode, prints message and exits")
        print("  insert --table <table> --data <json>")
        print("                           Insert JSON data to table")
        print("  insert --table <table> --data <json> --enrich")
        print("                           Insert with state enrichment")
        print("")
        print("Environment Variables:")
        print("  POSTGRES_HOST           PostgreSQL server hostname (required)")
        print("  POSTGRES_NAME           Database name (required)")
        print("  POSTGRES_USERNAME       Database username (required)")
        print("  POSTGRES_PASSWORD       Database password (required)")
        print("  POSTGRES_PORT           Database port (default: 5432)")
        print("  POSTGRES_TABLE          Default table for inserts")
        print("  IS_EVALUATION           Skip write in evaluation mode")
        print("")
        print("Table Schema (recommended):")
        print("  CREATE TABLE hook_events (")
        print("    id SERIAL PRIMARY KEY,")
        print("    session_id TEXT,")
        print("    data JSONB NOT NULL,")
        print("    created_at TIMESTAMPTZ DEFAULT NOW()")
        print("  );")
        print("")
        print("Examples:")
        print("  # Check configuration")
        print("  /app/hooks/integrations/postgres.py check")
        print("")
        print("  # As Claude Code hook")
        print("  /app/hooks/integrations/postgres.py hook --table hook_events < stop_event.json")
        print("")
        print("  # Test mode (no actual write)")
        print("  /app/hooks/integrations/postgres.py hook --demo")
        print("")
        print("  # Manual testing")
        print('  /app/hooks/integrations/postgres.py insert --table logs --data \'{"session_id": "uuid-123"}\'')
        sys.exit(1)

    command = sys.argv[1]

    if command == "check":
        integration = PostgresIntegration()
        as_json = "--json" in sys.argv
        integration.print_status(as_json=as_json)
        sys.exit(0 if integration.is_configured else 1)

    elif command == "hook":
        try:
            if "--demo" in sys.argv:
                log("[DEMO] PostgreSQL hook triggered - PostgreSQL integration ready")
                sys.exit(0)

            integration = PostgresIntegration()
            if not integration.is_configured:
                missing = integration.get_missing_required()
                error_msg = f"PostgreSQL hook SKIPPED - missing env vars: {missing}"
                log(error_msg, {"integration": "postgres", "missing": missing})
                print(f"\n{'=' * 60}")
                print(f"[POSTGRES] ERROR: {error_msg}")
                print(f"{'=' * 60}\n")
                sys.exit(0)

            table_name = None
            i = 2
            while i < len(sys.argv):
                if sys.argv[i] == "--table" and i + 1 < len(sys.argv):
                    table_name = sys.argv[i + 1]
                    i += 2
                else:
                    i += 1

            if not table_name:
                table_name = os.getenv("POSTGRES_TABLE", "")
                if not table_name:
                    print(json.dumps({"success": False, "error": "--table required or set POSTGRES_TABLE"}))
                    sys.exit(1)

            payload = json.load(sys.stdin)

            transcript_path = payload.get("transcript_path", "")
            if transcript_path:
                last_response = get_last_assistant_response(transcript_path)
                if last_response:
                    payload["last_response"] = last_response

            payload["integration"] = "postgres"

            insert(table_name, payload, enrich_from_state=True)
            sys.exit(0)
        except Exception:
            sys.exit(0)

    elif command == "insert":
        table_name = None
        data_json = None
        enrich = "--enrich" in sys.argv or "-e" in sys.argv

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--table" and i + 1 < len(sys.argv):
                table_name = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--data" and i + 1 < len(sys.argv):
                data_json = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        if not table_name:
            print("Error: Missing --table flag")
            print("Usage: /app/hooks/integrations/postgres.py insert --table <table> --data <json>")
            sys.exit(1)

        if not data_json:
            print("Error: Missing --data flag")
            print("Usage: /app/hooks/integrations/postgres.py insert --table <table> --data <json>")
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

            result = insert(table_name, payload, enrich_from_state=enrich)

            print(
                json.dumps(
                    {
                        "success": result.success,
                        "table_name": result.table_name,
                        "rows_affected": result.rows_affected,
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
        print("Usage: /app/hooks/integrations/postgres.py <command> [args]")
        sys.exit(1)


if __name__ == "__main__":
    main()
