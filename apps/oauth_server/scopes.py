"""Canonical OAuth capability scopes for the BrightBean MCP surface."""

from __future__ import annotations

from collections.abc import Collection

MCP_SCOPES = (
    "mcp.read",
    "mcp.content",
    "mcp.publish",
    "mcp.inbox.reply",
    "mcp.admin",
)
LEGACY_MCP_SCOPE = "mcp"
ADVERTISED_MCP_SCOPES = (*MCP_SCOPES, LEGACY_MCP_SCOPE)


def normalize_scopes(scopes: str | Collection[str]) -> tuple[str, ...]:
    values = scopes.split() if isinstance(scopes, str) else scopes
    return tuple(sorted(set(values)))


def has_mcp_scope(scopes: Collection[str]) -> bool:
    granted = set(scopes)
    return LEGACY_MCP_SCOPE in granted or bool(granted.intersection(MCP_SCOPES))


def scope_allows(scopes: Collection[str], required: str) -> bool:
    granted = set(scopes)
    return LEGACY_MCP_SCOPE in granted or required in granted
