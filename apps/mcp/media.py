"""Focused MCP media handlers."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone

from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.results import decode_page_cursor, encode_page_cursor

MCP_MEDIA_LIMIT_DEFAULT = 20
MCP_MEDIA_LIMIT_MAX = 100
MCP_UPLOAD_MAX_BYTES = 1024 * 1024
PRESIGN_LOCAL_MODE_MESSAGE = (
    "Presigned upload requires S3/R2 storage. In local mode use upload_media "
    "(base64, ≤1 MB) or POST /api/v1/media/ (multipart)."
)


def _common_helpers():
    """Load transport-neutral compatibility helpers without an import cycle."""
    from apps.mcp.handlers import _parse_uuid, _require_perm, _wrap_text

    return _parse_uuid, _require_perm, _wrap_text


def _visible_media_queryset(workspace_context):
    from apps.media_library.models import MediaAsset

    workspace = workspace_context.workspace
    return MediaAsset.objects.for_workspace_with_shared(
        workspace_id=workspace.id,
        organization_id=workspace.organization_id,
    )


def _serialize_media(asset) -> dict[str, Any]:
    from apps.api.schemas import MediaAssetResponse

    return MediaAssetResponse.from_asset(asset, last_used_at=getattr(asset, "last_used_at", None)).model_dump(
        mode="json"
    )


def _resolve_folder(workspace, args: dict[str, Any]):
    folder_id = args.get("folder_id")
    if not folder_id:
        return None
    from apps.media_library.models import MediaFolder

    parse_uuid, _, _ = _common_helpers()
    try:
        return MediaFolder.objects.get(
            id=parse_uuid(folder_id, "folder_id"),
            organization=workspace.organization,
        )
    except MediaFolder.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "folder_id not found in this organization") from exc


def _parse_tags(args: dict[str, Any]) -> list[str]:
    tags = args.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise JsonRpcError(INVALID_PARAMS, "tags must be a list of strings")
    return tags


def search_media(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    from apps.media_library.models import MediaAsset

    parse_uuid, _, wrap_text = _common_helpers()
    workspace_context = context["workspace_context"]
    query = args.get("query") or None
    media_type = args.get("media_type") or None
    tags = args.get("tags") or []
    folder_id = args.get("folder_id") or None
    is_starred = args.get("is_starred")
    limit = int(args.get("limit") or MCP_MEDIA_LIMIT_DEFAULT)
    if limit < 1 or limit > MCP_MEDIA_LIMIT_MAX:
        raise JsonRpcError(INVALID_PARAMS, f"limit must be between 1 and {MCP_MEDIA_LIMIT_MAX}")

    queryset = MediaAsset.objects.with_last_used_at(_visible_media_queryset(workspace_context)).filter(
        processing_status="completed"
    )
    if media_type:
        queryset = queryset.filter(media_type=media_type)
    if folder_id:
        queryset = queryset.filter(folder_id=parse_uuid(folder_id, "folder_id"))
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
    return wrap_text(
        {
            "items": [_serialize_media(asset) for asset in page[:limit]],
            "limit": limit,
            "next_cursor": encode_page_cursor(offset + limit) if has_more else None,
        }
    )


def get_media(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    from apps.media_library.models import MediaAsset

    parse_uuid, _, wrap_text = _common_helpers()
    if "media_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "media_id is required")
    media_id = parse_uuid(args["media_id"], "media_id")
    queryset = MediaAsset.objects.with_last_used_at(_visible_media_queryset(context["workspace_context"]))
    try:
        asset = queryset.get(id=media_id)
    except MediaAsset.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Media asset not found") from exc
    return wrap_text(_serialize_media(asset))


def upload_media(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Upload at most one MiB of validated base64 content."""
    from apps.media_library.quotas import StorageQuotaExceededError
    from apps.media_library.services import create_asset
    from apps.media_library.tasks import process_media_asset

    _, require_permission, wrap_text = _common_helpers()
    require_permission(context, "upload_media")
    if "filename" not in args:
        raise JsonRpcError(INVALID_PARAMS, "filename is required")
    if "content_base64" not in args:
        raise JsonRpcError(INVALID_PARAMS, "content_base64 is required")
    filename = args["filename"]
    if not isinstance(filename, str) or not filename.strip():
        raise JsonRpcError(INVALID_PARAMS, "filename must be a non-empty string")
    try:
        raw = base64.b64decode(args["content_base64"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise JsonRpcError(INVALID_PARAMS, "content_base64 is not valid base64") from exc
    if len(raw) > MCP_UPLOAD_MAX_BYTES:
        raise JsonRpcError(
            INVALID_PARAMS,
            "MCP upload limit is 1 MB. Use POST /api/v1/media/ (multipart) for larger files.",
        )

    workspace_context = context["workspace_context"]
    workspace = workspace_context.workspace
    principal = context["principal"]
    uploaded = SimpleUploadedFile(
        name=filename,
        content=raw,
        content_type=args.get("content_type") or "application/octet-stream",
    )
    try:
        asset = create_asset(
            organization=workspace.organization,
            workspace=workspace,
            uploaded_file=uploaded,
            uploaded_by=principal.user,
            folder=_resolve_folder(workspace, args),
            alt_text=args.get("alt_text", "") or "",
            title=args.get("title", "") or "",
            tags=_parse_tags(args),
        )
    except StorageQuotaExceededError as exc:
        raise JsonRpcError(
            INVALID_PARAMS,
            f"Storage quota exceeded: used={exc.used} limit={exc.limit} attempted={exc.attempted}",
        ) from exc
    except ValidationError as exc:
        raise JsonRpcError(INVALID_PARAMS, "; ".join(getattr(exc, "messages", [str(exc)]))) from exc
    process_media_asset(str(asset.id))
    return wrap_text(_serialize_media(asset))


def request_media_upload(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    from apps.media_library.services import create_pending_upload
    from apps.media_library.storage import is_s3_backend
    from apps.media_library.validators import ALL_ALLOWED_MIMES

    _, require_permission, wrap_text = _common_helpers()
    require_permission(context, "upload_media")
    if not is_s3_backend():
        raise JsonRpcError(INVALID_PARAMS, PRESIGN_LOCAL_MODE_MESSAGE)
    filename = args.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise JsonRpcError(INVALID_PARAMS, "filename must be a non-empty string")
    media_type = args["media_type"]
    content_type = args.get("content_type") or "application/octet-stream"
    if content_type != "application/octet-stream" and content_type not in ALL_ALLOWED_MIMES:
        raise JsonRpcError(
            INVALID_PARAMS,
            "content_type must be one of " + ", ".join(sorted(ALL_ALLOWED_MIMES)) + " (or omitted).",
        )

    workspace = context["workspace_context"].workspace
    pending, presigned = create_pending_upload(
        organization=workspace.organization,
        workspace=workspace,
        created_by=context["principal"].user,
        declared_filename=filename,
        content_type=content_type,
        requested_media_type=media_type,
    )
    return wrap_text(
        {
            "upload_id": str(pending.id),
            "method": presigned["method"],
            "url": presigned["url"],
            "fields": presigned["fields"],
            "max_bytes": pending.max_bytes,
            "expires_at": pending.expires_at.isoformat(),
            "instructions": (
                "Upload the raw bytes to 'url' as a multipart/form-data POST: send every "
                "key/value in 'fields' as form fields, then a final 'file' field holding the "
                "binary body. Then call finalize_media_upload with this upload_id."
            ),
        }
    )


def finalize_media_upload(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    from apps.media_library.models import MediaAsset, PendingUpload
    from apps.media_library.quotas import StorageQuotaExceededError
    from apps.media_library.services import inspect_uploaded_object, register_uploaded_asset
    from apps.media_library.storage import is_s3_backend
    from apps.media_library.tasks import process_media_asset

    parse_uuid, require_permission, wrap_text = _common_helpers()
    require_permission(context, "upload_media")
    if not is_s3_backend():
        raise JsonRpcError(INVALID_PARAMS, PRESIGN_LOCAL_MODE_MESSAGE)
    upload_id = parse_uuid(args.get("upload_id"), "upload_id")
    workspace = context["workspace_context"].workspace
    folder = _resolve_folder(workspace, args)
    tags = _parse_tags(args)
    try:
        pending = PendingUpload.objects.get(id=upload_id, workspace_id=workspace.id)
    except PendingUpload.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Upload not found") from exc

    if pending.finalized_at:
        if pending.media_asset_id is None:
            raise JsonRpcError(INVALID_PARAMS, "This upload was already finalized; its media asset no longer exists.")
        asset = pending.media_asset
    else:
        if pending.expires_at < timezone.now():
            raise JsonRpcError(INVALID_PARAMS, "This upload request has expired; request a new one.")
        try:
            inspected = inspect_uploaded_object(pending)
        except FileNotFoundError as exc:
            raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
        except StorageQuotaExceededError as exc:
            raise JsonRpcError(
                INVALID_PARAMS,
                f"Storage quota exceeded: used={exc.used} limit={exc.limit} attempted={exc.attempted}",
            ) from exc
        except ValidationError as exc:
            raise JsonRpcError(INVALID_PARAMS, "; ".join(getattr(exc, "messages", [str(exc)]))) from exc

        with transaction.atomic():
            locked = PendingUpload.objects.select_for_update().get(id=upload_id, workspace_id=workspace.id)
            if locked.finalized_at and locked.media_asset_id:
                asset = locked.media_asset
            else:
                asset = register_uploaded_asset(
                    pending=locked,
                    inspected=inspected,
                    uploaded_by=context["principal"].user,
                    folder=folder,
                    alt_text=args.get("alt_text", "") or "",
                    title=args.get("title", "") or "",
                    tags=tags,
                )
                locked.finalized_at = timezone.now()
                locked.media_asset = asset
                locked.save(update_fields=["finalized_at", "media_asset"])

    assert asset is not None
    if asset.processing_status == MediaAsset.ProcessingStatus.PENDING:
        process_media_asset(str(asset.id))
    return wrap_text(_serialize_media(asset))
