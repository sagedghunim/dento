class ApiError(Exception):
    """A refusal the client can understand: a code, a message in the user's language, and fields (design 6.1)."""

    def __init__(self, code, message, status=400, fields=None, retry_after=None, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or {}
        self.retry_after = retry_after
        self.extra = extra or {}  # more detail for the screen, e.g. the bookings a schedule change would strand

    def body(self):
        error = {"code": self.code, "message": str(self.message)}
        if self.fields:
            error["fields"] = self.fields
        error.update(self.extra)
        return {"error": error}
