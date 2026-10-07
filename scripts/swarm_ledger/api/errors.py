class APIError(ValueError):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details

    def envelope(self) -> dict:
        from .resources import MAX_REPLY, reply_size

        error = {"code": self.code, "message": str(self)[:1000]}
        if self.details is not None:
            details = dict(self.details)
            if "_meta" in details:
                meta = details["_meta"]
                details["_meta"] = {**meta, "warnings": [warning[:1000] for warning in meta.get("warnings", [])[-20:]]}
            error["details"] = details
            if reply_size({"error": error}) > MAX_REPLY:
                error["details"] = {"rejected": details.get("rejected", [])}
        return {"error": error}
