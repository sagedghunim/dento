class ApiError(Exception):
    """A refusal the client can understand: a code, a message in the user's language, and fields (design 6.1)."""

    def __init__(self, code, message, status=400, fields=None, retry_after=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or {}
        self.retry_after = retry_after

    def body(self):
        error = {"code": self.code, "message": str(self.message)}
        if self.fields:
            error["fields"] = self.fields
        return {"error": error}
