"""Transport-independent, structured publication errors."""

from __future__ import annotations


class PublishError(Exception):
    def __init__(
        self,
        code: str,
        status: int = 400,
        detail: str = "",
        *,
        field: str | None = None,
        actual: object = None,
        limit: object = None,
        action: str | None = None,
        retry_after: int | None = None,
    ):
        self.code = code
        self.status = status
        self.detail = detail or code.replace("_", " ")
        self.field = field
        self.actual = actual
        self.limit = limit
        self.retry_after = retry_after
        self.action = action
        super().__init__(self.detail)
