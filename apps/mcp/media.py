"""Focused MCP media handlers.

The upload implementations retain their battle-tested compatibility functions
for this release cycle; discovery and invocation route through this module so
the legacy transport file is no longer the public media boundary.
"""

from __future__ import annotations

from typing import Any

from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.results import decode_page_cursor, encode_page_cursor


def search_media(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    from apps.mcp.handlers import (
        _MCP_MEDIA_LIMIT_DEFAULT,
        _MCP_MEDIA_LIMIT_MAX,
        _parse_uuid,
        _serialize_media,
        _visible_media_qs,
        _wrap_text,
    )
    from apps.media_library.models import MediaAsset

    api_key = context["api_key"]
    query = args.get("query") or None
    media_type = args.get("media_type") or None
    tags = args.get("tags") or []
    folder_id_raw = args.get("folder_id") or None
    is_starred = args.get("is_starred")
    limit = int(args.get("limit") or _MCP_MEDIA_LIMIT_DEFAULT)
    if limit < 1 or limit > _MCP_MEDIA_LIMIT_MAX:
        raise JsonRpcError(INVALID_PARAMS, f"limit must be between 1 and {_MCP_MEDIA_LIMIT_MAX}")

    queryset = MediaAsset.objects.with_last_used_at(_visible_media_qs(api_key)).filter(processing_status="completed")
    if media_type:
        queryset = queryset.filter(media_type=media_type)
    if folder_id_raw:
        queryset = queryset.filter(folder_id=_parse_uuid(folder_id_raw, "folder_id"))
    if is_starred is not None:
        queryset = queryset.filter(is_starred=bool(is_starred))
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, str):
                raise JsonRpcError(INVALID_PARAMS, "tags must be a list of strings")
            queryset = queryset.filter(tags__contains=[tag])
    if query:
        queryset = MediaAsset.objects.search(query, queryset=queryset)
    try:
        offset = decode_page_cursor(args.get("cursor"))
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, "cursor is invalid") from exc
    page = list(queryset.order_by("-created_at", "id")[offset : offset + limit + 1])
    has_more = len(page) > limit
    return _wrap_text(
        {
            "items": [_serialize_media(asset) for asset in page[:limit]],
            "limit": limit,
            "next_cursor": encode_page_cursor(offset + limit) if has_more else None,
        }
    )


def get_media(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    from apps.mcp.handlers import _parse_uuid, _serialize_media, _visible_media_qs, _wrap_text
    from apps.media_library.models import MediaAsset

    if "media_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "media_id is required")
    media_id = _parse_uuid(args["media_id"], "media_id")
    queryset = MediaAsset.objects.with_last_used_at(_visible_media_qs(context["api_key"]))
    try:
        asset = queryset.get(id=media_id)
    except MediaAsset.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Media asset not found") from exc
    return _wrap_text(_serialize_media(asset))


def upload_media(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    from apps.mcp.handlers import _upload_media

    return _upload_media(args, context)


def request_media_upload(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    from apps.mcp.handlers import _request_media_upload

    return _request_media_upload(args, context)


def finalize_media_upload(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    from apps.mcp.handlers import _finalize_media_upload

    return _finalize_media_upload(args, context)
