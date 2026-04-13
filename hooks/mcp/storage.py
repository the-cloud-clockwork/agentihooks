"""S3 storage & filesystem MCP tools."""

import json

from hooks.common import log


def register(mcp):
    @mcp.tool()
    def storage_upload_path(
        session_id: str,
        path: str,
        prefix: str = "",
        match_uuid: bool = False,
    ) -> str:
        """Upload a local path to S3 storage with session prefix.

        Uploads file or directory to S3 under sessions/<session_id>/ or custom prefix.

        Args:
            session_id: Session ID for S3 prefix
            path: Local file or directory path to upload
            prefix: S3 key prefix (default: sessions/<session_id>/)
            match_uuid: If True, extract UUID from filename and use as prefix (default: False)

        Returns:
            JSON with success status, storage_url, uploaded file count
        """
        try:
            from hooks.integrations.storage import upload_path

            result = upload_path(
                session_id,
                path=path,
                prefix=prefix if prefix else None,
                match_uuid=match_uuid,
            )

            return json.dumps(
                {
                    "success": result.success,
                    "storage_url": result.storage_url,
                    "files_uploaded": result.files_uploaded,
                    "error": result.error,
                }
            )

        except Exception as e:
            log("MCP storage_upload_path failed", {"path": path, "error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
