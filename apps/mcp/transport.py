"""Compatibility facade for legacy transport helpers.

JSON-RPC batching and the handwritten endpoint live exclusively in
``apps.mcp.legacy``. Imports remain here for one release cycle so existing
extensions do not break while deployments migrate to the SDK backend.
"""

from apps.mcp.legacy import (
    METHODS,
    _status_for_response,
    _ToolValidationError,
    _validate_tool_arguments,
    mcp_endpoint,
    router,
)

__all__ = [
    "METHODS",
    "_status_for_response",
    "_ToolValidationError",
    "_validate_tool_arguments",
    "mcp_endpoint",
    "router",
]
