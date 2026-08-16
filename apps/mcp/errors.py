"""Safe, stable domain failures shared by MCP transports."""

from __future__ import annotations

import json
import re
from typing import Any

_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]*$")

DOMAIN_ERROR_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "pattern": _ERROR_CODE.pattern},
                "message": {"type": "string"},
                "details": {"type": "object"},
                "retryable": {"type": "boolean"},
            },
            "required": ["code", "message", "retryable"],
            "additionalProperties": False,
        }
    },
    "required": ["error"],
    "additionalProperties": False,
}


class DomainError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        if not _ERROR_CODE.fullmatch(code):
            raise ValueError("Domain error codes must be stable snake_case identifiers.")
        try:
            json.dumps(details)
        except (TypeError, ValueError) as exc:
            raise TypeError("Domain error details must be JSON-serializable.") from exc
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable

    def as_structured_content(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details is not None:
            payload["details"] = self.details
        return {"error": payload}


def domain_error_result(error: DomainError) -> dict[str, Any]:
    structured = error.as_structured_content()
    return {
        "content": [{"type": "text", "text": json.dumps(structured)}],
        "structuredContent": structured,
        "isError": True,
    }


def tool_disabled_error(name: str) -> DomainError:
    return DomainError(
        "tool_disabled",
        "This tool is currently disabled.",
        details={"tool": name},
    )
