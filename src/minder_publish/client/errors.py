"""Safe structured client and API failures."""

from __future__ import annotations

from typing import Any


class ClientError(Exception):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        status: int = 0,
        field: str | None = None,
        actual: Any = None,
        limit: Any = None,
        action: str | None = None,
    ):
        self.code, self.detail, self.status = code, detail, status
        self.field, self.actual, self.limit, self.action = field, actual, limit, action
        super().__init__(detail)

    def as_dict(self) -> dict:
        sensitive = self.field and any(
            word in self.field.lower()
            for word in ("password", "token", "secret", "authorization")
        )
        return {
            key: value
            for key, value in {
                "code": self.code,
                "detail": self.detail,
                "status": self.status,
                "field": self.field,
                "actual": "[redacted]" if sensitive else self.actual,
                "limit": self.limit,
                "action": self.action,
            }.items()
            if value is not None
        }

    @property
    def exit_code(self) -> int:
        if self.status in (401, 403):
            return 3
        if self.status in (404, 410):
            return 4
        if self.status == 409:
            return 5
        if self.status in (413, 422, 429, 507):
            return 6
        if self.code in ("transport_error", "timeout"):
            return 7
        return 2 if self.status < 500 else 1
