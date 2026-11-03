#!/usr/bin/env python3
"""AWS S3 storage integration for uploading files and directories from hooks.

Uploads files or directories to S3 with session-based organization.
Destination: $STORAGE_URL/<SESSION-ID>/<PREFIX>/...

Environment Variables:
    STORAGE_URL: S3 base URL (e.g., s3://bucket/path) (optional, no upload if not set)
    IS_EVALUATION: Skip upload in evaluation mode (default: false)

Usage:
    upload_path(session_id="uuid-123", path="/path/to/file.txt", prefix="transcripts")
    upload_path(session_id="uuid-123", path="/path/to/dir", prefix="artifacts")
    storage.py hook --path /app/transcript.jsonl --prefix transcripts
    storage.py hook --path /app/outputs --prefix results --match-uuid
    storage.py upload uuid-123 --path /file.txt --prefix logs

With --match-uuid only files whose name contains the session UUID are uploaded.
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

DEFAULT_STATE_FILE = Path.home() / "conversation_map.json"


@IntegrationRegistry.register
class StorageIntegration(IntegrationBase):
    """S3 Storage integration configuration checker."""

    INTEGRATION_NAME = "storage"
    REQUIRED_ENV_VARS = {
        "STORAGE_URL": "S3 base URL for uploads (e.g., s3://bucket/path)",
    }
    OPTIONAL_ENV_VARS = {
        "IS_EVALUATION": "Skip upload in evaluation mode (default: false)",
        "AWS_PROFILE": "AWS profile to use for credentials",
        "AWS_REGION": "AWS region for S3 bucket",
    }


def load_state_for_session(
    session_id: str,
    state_file: Path = DEFAULT_STATE_FILE,
) -> Optional[Dict[str, Any]]:
    """Load state data for a session from conversation_map.json.

    Uses correlation ID (external UUID) for lookup since the API stores state
    under the external UUID, which may differ from Claude's session ID when
    stateless=True.

    Args:
        session_id: Session UUID to look up (will be mapped to correlation ID)
        state_file: Custom state file path (default: ~/conversation_map.json)

    Returns:
        Dict with session state (conversation_id, platform, etc.) or None if not found
    """
    try:
        if not state_file.exists():
            log("State file not found", {"file": str(state_file)})
            return None

        correlation_id = get_correlation_id(session_id)

        log(
            "Session ID mapping for state lookup",
            {
                "claude_session_id": session_id,
                "correlation_id": correlation_id,
                "same": session_id == correlation_id,
            },
        )

        mappings = json.loads(state_file.read_text())
        state = mappings.get(correlation_id)

        if state:
            log(
                "Loaded session state",
                {
                    "correlation_id": correlation_id,
                    "fields": list(state.keys()),
                },
            )
        else:
            log("Session not found in state file", {"correlation_id": correlation_id})

        return state

    except Exception as e:
        log("Failed to load state", {"error": str(e), "session_id": session_id})
        return None


def state_to_s3_metadata(state: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Convert session state to S3-compatible metadata.

    S3 metadata requires string keys and string values.
    Keys are automatically lowercased by S3.
    Lists and dicts are JSON-encoded.

    Args:
        state: Session state dict from conversation_map.json

    Returns:
        Dict with string keys/values suitable for S3 Metadata parameter
    """
    if not state:
        return {}

    metadata = {}
    for key, value in state.items():
        if value is None:
            continue
        if isinstance(value, bool):
            metadata[key] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            metadata[key] = str(value)
        elif isinstance(value, (list, dict)):
            metadata[key] = json.dumps(value)

    log("Converted state to S3 metadata", {"fields": list(metadata.keys())})
    return metadata


@dataclass
class UploadResult:
    """Result of S3 upload operation."""

    success: bool
    files_uploaded: int = 0
    storage_url: Optional[str] = None
    session_id: Optional[str] = None
    prefix: Optional[str] = None
    error: Optional[str] = None


class S3StorageClient:
    """AWS S3 storage client using boto3."""

    _instance: Optional["S3StorageClient"] = None

    def __init__(
        self,
        storage_url: Optional[str] = None,
        skip_evaluation: bool = True,
    ):
        """Initialize S3 storage client.

        Args:
            storage_url: S3 base URL (default: from env STORAGE_URL)
            skip_evaluation: Skip upload when IS_EVALUATION=true (default: True)
        """
        self._storage_url = storage_url or os.getenv("STORAGE_URL", "")
        self._skip_evaluation = skip_evaluation
        self._s3_client = None

        self._bucket = None
        self._base_prefix = None
        if self._storage_url:
            self._parse_storage_url()

        self._is_evaluation = os.getenv("IS_EVALUATION", "false").lower() == "true"

    def _parse_storage_url(self):
        """Parse S3 URL into bucket and prefix.

        Example: s3://my-bucket/path/to/base → bucket=my-bucket, prefix=path/to/base
        """
        url = self._storage_url
        if not url.startswith("s3://"):
            log("Invalid STORAGE_URL", {"url": url, "expected": "s3://..."})
            return

        parts = url[5:].split("/", 1)
        self._bucket = parts[0]
        self._base_prefix = parts[1] if len(parts) > 1 else ""

        log(
            "Parsed STORAGE_URL",
            {
                "bucket": self._bucket,
                "base_prefix": self._base_prefix,
            },
        )

    @classmethod
    def get_client(
        cls,
        storage_url: Optional[str] = None,
        skip_evaluation: bool = True,
    ) -> "S3StorageClient":
        """Get singleton instance."""
        if cls._instance is None or storage_url:
            cls._instance = cls(
                storage_url=storage_url,
                skip_evaluation=skip_evaluation,
            )
        return cls._instance

    @classmethod
    def clear_cache(cls) -> None:
        """Clear cached instance (for testing)."""
        cls._instance = None

    @property
    def s3(self):
        """Lazy-load boto3 S3 client."""
        if self._s3_client is None:
            if not BOTO3_AVAILABLE:
                raise ImportError("boto3 is required for S3 storage integration")
            self._s3_client = boto3.client("s3")
        return self._s3_client

    def should_skip(self) -> bool:
        """Check if upload should be skipped."""
        if not self._storage_url or not self._bucket:
            log(
                "Storage upload skipped: STORAGE_URL not configured",
                {"integration": "storage", "reason": "missing_config", "var": "STORAGE_URL"},
            )
            return True

        if self._skip_evaluation and self._is_evaluation:
            log(
                "Storage upload skipped: IS_EVALUATION=true",
                {"integration": "storage", "reason": "evaluation_mode"},
            )
            return True

        return False

    def upload_path(
        self,
        session_id: str,
        path: str,
        prefix: str = "",
        match_uuid: bool = False,
        metadata: Optional[Dict[str, str]] = None,
    ) -> UploadResult:
        """Upload file or directory to S3.

        Destination: s3://{bucket}/{base_prefix}/artifacts/<filename>

        Note: Session artifacts are placed flat under 'artifacts/' folder.
        Files should have session_id in filename (e.g., <session-id>-diagram.drawio).

        Args:
            session_id: Session UUID (used for UUID matching, not in path)
            path: Local file or directory path to upload
            prefix: Additional prefix for S3 key (e.g., "transcripts", "diagrams")
            match_uuid: If True, only upload files containing session_id in filename
            metadata: Optional S3 metadata dict to attach to uploaded objects

        Returns:
            UploadResult with success status and upload count
        """
        if self.should_skip():
            return UploadResult(
                success=True,
                files_uploaded=0,
                storage_url=self._storage_url or "not-configured",
                session_id=session_id,
                prefix=prefix,
            )

        try:
            local_path = Path(path)

            if not local_path.exists():
                error_msg = f"Path does not exist: {path}"
                log("Upload failed", {"error": error_msg, "path": path})
                return UploadResult(
                    success=False,
                    storage_url=self._storage_url,
                    session_id=session_id,
                    prefix=prefix,
                    error=error_msg,
                )

            key_parts = []
            if self._base_prefix:
                key_parts.append(self._base_prefix.strip("/"))
            key_parts.append("artifacts")

            s3_prefix = "/".join(key_parts)

            log(
                "Starting S3 upload",
                {
                    "session_id": session_id,
                    "local_path": str(local_path),
                    "bucket": self._bucket,
                    "s3_prefix": s3_prefix,
                    "match_uuid": match_uuid,
                    "has_metadata": bool(metadata),
                },
            )

            if local_path.is_file():
                if match_uuid and not self._filename_matches_uuid(local_path.name, session_id):
                    log("File skipped (UUID not in name)", {"file": local_path.name})
                    files_uploaded = 0
                else:
                    files_uploaded = self._upload_file(local_path, s3_prefix, metadata)
            elif local_path.is_dir():
                files_uploaded = self._upload_directory(local_path, s3_prefix, session_id, match_uuid, metadata)
            else:
                error_msg = f"Path is neither file nor directory: {path}"
                log("Upload failed", {"error": error_msg})
                return UploadResult(
                    success=False,
                    storage_url=self._storage_url,
                    session_id=session_id,
                    prefix=prefix,
                    error=error_msg,
                )

            log(
                "S3 upload succeeded",
                {
                    "files_uploaded": files_uploaded,
                    "session_id": session_id,
                    "s3_prefix": s3_prefix,
                },
            )

            return UploadResult(
                success=True,
                files_uploaded=files_uploaded,
                storage_url=self._storage_url,
                session_id=session_id,
                prefix=prefix,
            )

        except Exception as e:
            error_msg = str(e)
            log(
                "S3 upload failed",
                {
                    "error": error_msg,
                    "session_id": session_id,
                    "path": path,
                },
            )

            return UploadResult(
                success=False,
                storage_url=self._storage_url,
                session_id=session_id,
                prefix=prefix,
                error=error_msg,
            )

    def _filename_matches_uuid(self, filename: str, uuid: str) -> bool:
        """Check if filename contains the UUID.

        Args:
            filename: Filename to check
            uuid: UUID string to search for

        Returns:
            True if UUID is found in filename (case-insensitive)
        """
        return uuid.lower() in filename.lower()

    def _upload_file(
        self,
        file_path: Path,
        s3_prefix: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> int:
        """Upload a single file to S3.

        Args:
            file_path: Path to local file
            s3_prefix: S3 key prefix (without filename)
            metadata: Optional S3 metadata dict to attach to the object

        Returns:
            Number of files uploaded (1 on success)
        """
        s3_key = f"{s3_prefix}/{file_path.name}"

        log(
            "Uploading file",
            {
                "file": str(file_path),
                "s3_key": s3_key,
                "size_bytes": file_path.stat().st_size,
                "has_metadata": bool(metadata),
            },
        )

        put_params = {
            "Bucket": self._bucket,
            "Key": s3_key,
        }

        if metadata:
            put_params["Metadata"] = metadata

        with open(file_path, "rb") as f:
            put_params["Body"] = f
            self.s3.put_object(**put_params)

        log("File uploaded", {"s3_key": s3_key, "metadata_fields": list(metadata.keys()) if metadata else []})
        return 1

    def _upload_directory(
        self,
        dir_path: Path,
        s3_prefix: str,
        session_id: str,
        match_uuid: bool = False,
        metadata: Optional[Dict[str, str]] = None,
    ) -> int:
        """Upload directory recursively to S3.

        Args:
            dir_path: Path to local directory
            s3_prefix: S3 key prefix (directory structure preserved under this)
            session_id: Session UUID for filtering
            match_uuid: If True, only upload files containing session_id in filename
            metadata: Optional S3 metadata dict to attach to uploaded objects

        Returns:
            Number of files uploaded
        """
        files_uploaded = 0
        files_skipped = 0

        for file_path in dir_path.rglob("*"):
            if not file_path.is_file():
                continue

            if match_uuid and not self._filename_matches_uuid(file_path.name, session_id):
                log("File skipped (UUID not in name)", {"file": file_path.name})
                files_skipped += 1
                continue

            relative_path = file_path.relative_to(dir_path)

            s3_key = f"{s3_prefix}/{relative_path}".replace("\\", "/")

            log(
                "Uploading file",
                {
                    "file": str(file_path),
                    "s3_key": s3_key,
                    "size_bytes": file_path.stat().st_size,
                    "has_metadata": bool(metadata),
                },
            )

            try:
                put_params = {
                    "Bucket": self._bucket,
                    "Key": s3_key,
                }

                if metadata:
                    put_params["Metadata"] = metadata

                with open(file_path, "rb") as f:
                    put_params["Body"] = f
                    self.s3.put_object(**put_params)

                files_uploaded += 1
                log("File uploaded", {"s3_key": s3_key, "metadata_fields": list(metadata.keys()) if metadata else []})
            except Exception as e:
                log(
                    "File upload failed",
                    {
                        "file": str(file_path),
                        "error": str(e),
                    },
                )

        if files_skipped > 0:
            log(
                "UUID filtering summary",
                {
                    "files_uploaded": files_uploaded,
                    "files_skipped": files_skipped,
                    "session_id": session_id,
                },
            )

        return files_uploaded


def upload_path(
    session_id: str,
    path: str,
    prefix: str = "",
    storage_url: Optional[str] = None,
    match_uuid: bool = False,
    enrich: bool = False,
    metadata: Optional[Dict[str, str]] = None,
) -> UploadResult:
    """Upload file or directory to S3.

    Convenience function that uses singleton client.

    Args:
        session_id: Session UUID (used in S3 key path)
        path: Local file or directory path to upload
        prefix: Additional prefix for S3 key (e.g., "transcripts", "artifacts")
        storage_url: S3 base URL (default: from env STORAGE_URL)
        match_uuid: If True, only upload files containing session_id in filename
        enrich: If True, load metadata from conversation_map.json for this session
        metadata: Optional explicit metadata dict (overrides enrich if both provided)

    Returns:
        UploadResult with success status and upload count

    Examples:
        # Upload transcript file
        result = upload_path(
            session_id="uuid-123",
            path="/app/transcript.jsonl",
            prefix="transcripts"
        )

        # Upload artifacts directory with metadata enrichment
        result = upload_path(
            session_id="uuid-456",
            path="/app/output",
            prefix="artifacts",
            enrich=True  # Loads conversation_id, platform, etc. from state
        )
    """
    if enrich and not metadata:
        state = load_state_for_session(session_id)
        metadata = state_to_s3_metadata(state)

    client = S3StorageClient.get_client(storage_url=storage_url)
    return client.upload_path(session_id, path, prefix, match_uuid, metadata)


def main():
    """CLI entry point for S3 storage operations.

    Hook mode prints to stdout, where Claude Code shows it, and exits 0 so a failed upload never breaks Claude Code.
    """
    if len(sys.argv) < 2:
        print("Usage: /app/hooks/integrations/storage.py <command> [args]")
        print("")
        print("Commands:")
        print("  check                    Check configuration status")
        print("  check --json             Output status as JSON")
        print("  hook --path <path> --prefix <prefix> [--match-uuid] [--enrich] [--demo]")
        print("      Read Stop event from stdin, extract session_id, upload path")
        print("      --match-uuid: Only upload files containing session_id in filename")
        print("      --enrich: Attach session metadata (conversation_id, platform, etc.) to S3 objects")
        print("      --demo: Test mode, prints message and exits")
        print("")
        print("  upload <session_id> --path <path> --prefix <prefix> [--match-uuid] [--enrich]")
        print("      Upload path directly with explicit session_id")
        print("      --match-uuid: Only upload files containing session_id in filename")
        print("      --enrich: Attach session metadata from ~/conversation_map.json to S3 objects")
        print("")
        print("Environment Variables:")
        print("  STORAGE_URL           S3 base URL (e.g., s3://bucket/path) (required)")
        print("  IS_EVALUATION         Skip upload in evaluation mode")
        print("  AWS_PROFILE           AWS profile to use for credentials")
        print("  AWS_REGION            AWS region for S3 bucket")
        print("")
        print("Examples:")
        print("  # Check configuration")
        print("  /app/hooks/integrations/storage.py check")
        print("")
        print("  # As Claude Code hook")
        print(
            "  /app/hooks/integrations/storage.py hook --path /app/transcript.jsonl --prefix transcripts < stop_event.json"
        )
        print("")
        print("  # As hook with metadata enrichment (attaches conversation_id, platform to S3 objects)")
        print(
            "  /app/hooks/integrations/storage.py hook --path /app/artifacts --prefix diagrams --match-uuid --enrich < stop_event.json"
        )
        print("")
        print("  # Test mode (no actual upload)")
        print("  /app/hooks/integrations/storage.py hook --demo")
        print("")
        print("  # As hook with UUID matching (only upload files with UUID in name)")
        print(
            "  /app/hooks/integrations/storage.py hook --path /app/outputs --prefix results --match-uuid < stop_event.json"
        )
        print("")
        print("  # Direct upload")
        print('  export STORAGE_URL="s3://my-bucket/agents"')
        print("  /app/hooks/integrations/storage.py upload uuid-123 --path /file.txt --prefix logs")
        print("")
        print("  # Direct upload with UUID matching and metadata enrichment")
        print(
            "  /app/hooks/integrations/storage.py upload abc-uuid-123 --path /app/outputs --prefix logs --match-uuid --enrich"
        )
        sys.exit(1)

    command = sys.argv[1]

    if command == "check":
        integration = StorageIntegration()
        as_json = "--json" in sys.argv
        integration.print_status(as_json=as_json)
        sys.exit(0 if integration.is_configured else 1)

    elif command == "hook":
        try:
            if "--demo" in sys.argv:
                log("[DEMO] Storage hook triggered - S3 integration ready")
                sys.exit(0)

            integration = StorageIntegration()
            if not integration.is_configured:
                missing = integration.get_missing_required()
                error_msg = f"Storage hook SKIPPED - missing env vars: {missing}"
                log(error_msg, {"integration": "storage", "missing": missing})
                print(f"\n{'=' * 60}")
                print(f"[STORAGE] ERROR: {error_msg}")
                print(f"{'=' * 60}\n")
                sys.exit(0)

            path = None
            prefix = ""
            match_uuid = False
            enrich = False

            i = 2
            while i < len(sys.argv):
                if sys.argv[i] == "--path" and i + 1 < len(sys.argv):
                    path = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == "--prefix" and i + 1 < len(sys.argv):
                    prefix = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == "--match-uuid":
                    match_uuid = True
                    i += 1
                elif sys.argv[i] == "--enrich":
                    enrich = True
                    i += 1
                elif sys.argv[i] == "--demo":
                    i += 1
                else:
                    i += 1

            if not path:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": "--path flag is required",
                        }
                    )
                )
                sys.exit(1)

            payload = json.load(sys.stdin)
            session_id = payload.get("session_id", "")

            if not session_id:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": "session_id not found in hook payload",
                        }
                    )
                )
                sys.exit(1)

            result = upload_path(session_id, path, prefix, match_uuid=match_uuid, enrich=enrich)

            print(
                json.dumps(
                    {
                        "success": result.success,
                        "files_uploaded": result.files_uploaded,
                        "session_id": result.session_id,
                        "prefix": result.prefix,
                        "storage_url": result.storage_url,
                        "error": result.error,
                    }
                )
            )

            sys.exit(0 if result.success else 1)

        except Exception as e:
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": str(e),
                    }
                )
            )
            sys.exit(0)

    elif command == "upload":
        if len(sys.argv) < 3:
            print("Error: Missing session_id")
            print(
                "Usage: /app/hooks/integrations/storage.py upload <session_id> --path <path> --prefix <prefix> [--enrich]"
            )
            sys.exit(1)

        session_id = sys.argv[2]

        path = None
        prefix = ""
        match_uuid = False
        enrich = False

        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == "--path" and i + 1 < len(sys.argv):
                path = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--prefix" and i + 1 < len(sys.argv):
                prefix = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--match-uuid":
                match_uuid = True
                i += 1
            elif sys.argv[i] == "--enrich":
                enrich = True
                i += 1
            else:
                i += 1

        if not path:
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": "--path flag is required",
                    }
                )
            )
            sys.exit(1)

        try:
            result = upload_path(session_id, path, prefix, match_uuid=match_uuid, enrich=enrich)

            print(
                json.dumps(
                    {
                        "success": result.success,
                        "files_uploaded": result.files_uploaded,
                        "session_id": result.session_id,
                        "prefix": result.prefix,
                        "storage_url": result.storage_url,
                        "error": result.error,
                    }
                )
            )

            sys.exit(0 if result.success else 1)

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
        print("Usage: /app/hooks/integrations/storage.py <command> [args]")
        sys.exit(1)


if __name__ == "__main__":
    main()
