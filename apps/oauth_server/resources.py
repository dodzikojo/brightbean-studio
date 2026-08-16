"""Canonical OAuth resource identity for BrightBean MCP."""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def canonical_mcp_resource_uri() -> str:
    base = settings.MCP_PUBLIC_BASE_URL.rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ImproperlyConfigured("MCP_PUBLIC_BASE_URL must be an absolute HTTP(S) origin.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ImproperlyConfigured("MCP_PUBLIC_BASE_URL must contain only a scheme and authority.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ImproperlyConfigured("MCP_PUBLIC_BASE_URL must use HTTPS outside development and tests.")
    return f"{base}/api/v1/mcp"
