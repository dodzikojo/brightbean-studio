"""Classify MCP requests for BrightBean's read and write rate buckets."""

from __future__ import annotations

import json
from typing import Any

from apps.mcp.registry import get_tool

_READ_ONLY_METHODS = frozenset(
    {
        "completion/complete",
        "initialize",
        "logging/setLevel",
        "notifications/cancelled",
        "notifications/initialized",
        "ping",
        "prompts/get",
        "prompts/list",
        "resources/list",
        "resources/read",
        "resources/templates/list",
        "roots/list",
        "server/discover",
        "tools/list",
    }
)


def message_is_write(message: Any) -> bool:
    """Fail closed for malformed/unknown messages and classify known calls."""
    if not isinstance(message, dict):
        return True
    method = message.get("method")
    if method == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        tool = get_tool(name, include_disabled=True) if isinstance(name, str) else None
        return tool is None or not tool.annotations.read_only
    return not isinstance(method, str) or method not in _READ_ONLY_METHODS


def request_is_write(request_body: bytes) -> bool:
    try:
        message = json.loads(request_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True
    return message_is_write(message)
