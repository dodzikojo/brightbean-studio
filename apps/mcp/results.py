"""Canonical MCP tool result, resource-link, and cursor helpers."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping
from typing import Any

from apps.api.pagination import encode_offset_cursor


def _json_value(value: Any) -> Any:
    """Normalize legacy UUID/datetime values into a JSON-safe structure."""
    return json.loads(json.dumps(value, default=str))


def resource_link(
    uri: str,
    *,
    name: str,
    description: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    link: dict[str, Any] = {"type": "resource_link", "uri": uri, "name": name}
    if description is not None:
        link["description"] = description
    if mime_type is not None:
        link["mimeType"] = mime_type
    return link


def success_result(
    structured_content: Mapping[str, Any],
    *,
    text: str | None = None,
    resource_links: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    normalized = _json_value(dict(structured_content))
    fallback = text if text is not None else json.dumps(normalized)
    return {
        "content": [{"type": "text", "text": fallback}, *[_json_value(link) for link in resource_links]],
        "structuredContent": normalized,
        "isError": False,
    }


def encode_page_cursor(offset: int) -> str:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("Invalid cursor offset.")
    return encode_offset_cursor(offset)


def decode_page_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("Invalid cursor.")
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode((cursor + padding).encode()).decode())
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid cursor.") from exc
    if not isinstance(payload, dict) or "o" not in payload:
        raise ValueError("Invalid cursor.")
    offset = payload["o"]
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("Invalid cursor.")
    return offset
