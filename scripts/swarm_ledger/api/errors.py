class APIError(ValueError):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details

    def envelope(self) -> dict:
        error = {"code": self.code, "message": str(self)}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}
