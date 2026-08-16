"""Concrete MCP tool implementations.

Every tool delegates to the same service-layer functions the REST API
uses — ``apps.composer.services.create_post`` for writes, the same
allowlist + permission checks, the same platform quota — so there's no
MCP-only code path that can drift from REST validation.

Tool result envelope mirrors the spec: a list of ``content`` blocks
plus an ``isError`` flag. We serialize structured results as
``{type: "text", text: "<json>"}`` because Claude clients render JSON
in text blocks more reliably than the experimental ``json`` content
type, and agents can always ``JSON.parse`` it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from typing import Any
from uuid import UUID

from django.db.models import Exists, OuterRef
from ninja.errors import HttpError

from apps.analytics.api_builders import build_account_analytics, build_post_analytics
from apps.api.limits import check_platform_quota
from apps.api.schemas import PostResponse
from apps.composer.models import PlatformPost, Post
from apps.composer.services import create_post, transition_platform_post
from apps.mcp.media import MCP_MEDIA_LIMIT_DEFAULT, MCP_MEDIA_LIMIT_MAX
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.results import decode_page_cursor, encode_page_cursor, success_result
from apps.mcp.tools import Tool, register_tool
from apps.social_accounts.models import SocialAccount

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _focused_handler(module_name: str, function_name: str):
    """Resolve a focused handler lazily, avoiding import cycles at app boot."""

    def handler(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        module = import_module(f"apps.mcp.{module_name}")
        return getattr(module, function_name)(args, context)

    return handler


def _wrap_text(payload: Any) -> dict:
    """Return MCP's text-content envelope around a JSON-serializable value.

    Most Claude clients render text blocks reliably; the experimental
    ``json`` content type isn't universally supported yet. Agents can
    always ``JSON.parse`` the returned text.
    """
    return success_result(payload)


def _require_perm(context: dict[str, Any], permission_key: str) -> None:
    """Re-check a workspace permission inside a tool handler.

    Mirrors REST's ``_require_perm`` so MCP can't be used to bypass
    permissions that the REST surface enforces.
    """
    workspace_context = context["workspace_context"]
    if permission_key not in workspace_context.effective_permissions:
        raise JsonRpcError(INVALID_PARAMS, f"Permission denied: {permission_key}")


def _parse_uuid(value: Any, field_name: str) -> UUID:
    if not isinstance(value, str):
        raise JsonRpcError(INVALID_PARAMS, f"{field_name} must be a string UUID")
    try:
        return UUID(value)
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, f"{field_name} is not a valid UUID") from exc


def _resolve_allowed_account(workspace_context, social_account_id_str: str) -> SocialAccount:
    sa_id = _parse_uuid(social_account_id_str, "social_account_id")
    try:
        return SocialAccount.objects.get(
            id=sa_id,
            workspace_id=workspace_context.workspace_id,
            id__in=workspace_context.allowed_account_ids,
        )
    except SocialAccount.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "social_account_id is not in this API key's allowlist") from exc


def _can_view_internal_notes(context: dict[str, Any]) -> bool:
    """Whether this MCP caller may see a post's team-only ``internal_notes``.

    Mirrors the REST router's check: visibility is gated on ``create_posts``,
    the permission held by exactly the workspace roles the composer lets view
    internal notes (i.e. not client/viewer). ``get_post`` isn't permission-
    gated, so without this an OAuth client/viewer could read team notes.
    """
    workspace_context = context.get("workspace_context")
    return bool(workspace_context and "create_posts" in workspace_context.effective_permissions)


def _serialize_post(post: Post, context: dict[str, Any]) -> dict:
    """Serialize a Post for an MCP tool response.

    Delegates to the same Pydantic schema the REST router returns so
    the two surfaces cannot drift in either field set or wire format.
    ``internal_notes`` is redacted unless the caller may view it (see
    ``_can_view_internal_notes``).
    """
    return PostResponse.from_post(post, include_internal_notes=_can_view_internal_notes(context)).model_dump(
        mode="json"
    )


def _visible_posts_qs(api_key):
    """Posts this API key may see, as a queryset.

    Same rule as REST's ``_get_workspace_post``: in the key's workspace,
    with at least one platform target, and *every* target allowlisted — so a
    partial-scope key never learns about siblings. Expressed in SQL (rather
    than filtering child rows in Python) so callers can order, filter and
    paginate on it without scanning rows they may not see.
    """
    allowed = [sa.id for sa in api_key.social_accounts.all()]
    # A key can end up with an empty allowlist at runtime (its last account was
    # disconnected/deleted). Short-circuit rather than leaning on Django folding
    # ``__in=[]`` inside an ``exclude`` into a no-op: fail closed explicitly.
    if not allowed:
        return Post.objects.none()
    children = PlatformPost.objects.filter(post_id=OuterRef("pk"))
    return (
        Post.objects.filter(workspace_id=api_key.workspace_id)
        .filter(Exists(children))
        .exclude(Exists(children.exclude(social_account_id__in=allowed)))
    )


def _get_post_for_key(api_key, post_id_str: str) -> Post:
    """Allowlist-respecting Post fetch shared by ``get_post`` / ``cancel_post``.

    Out-of-scope and nonexistent both surface as "Post not found" — the API
    never reveals which is which.
    """
    post_id = _parse_uuid(post_id_str, "post_id")
    try:
        return _visible_posts_qs(api_key).prefetch_related("platform_posts__social_account").get(id=post_id)
    except Post.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Post not found") from exc


def _parse_iso_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise JsonRpcError(INVALID_PARAMS, f"{field_name} must be a string")
    try:
        # ``fromisoformat`` accepts trailing 'Z' starting in Python 3.11.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, f"{field_name} must be ISO 8601") from exc
    # Interpret a tz-less value as UTC (the documented contract) so it lands on
    # the USE_TZ model as an aware instant — otherwise Django stores it naive
    # (RuntimeWarning) and the workspace-tz list views re-localize it wrongly.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# ---------------------------------------------------------------------------
# Tool: list_accounts
# ---------------------------------------------------------------------------


def _list_accounts(args: dict, context: dict[str, Any]) -> dict:
    api_key = context["api_key"]
    # Reuse the REST schema so MCP and REST stay byte-identical (Gap 4 + 5).
    from apps.api.schemas import AccountSummary

    accounts = [AccountSummary.from_social_account(sa).model_dump(mode="json") for sa in api_key.social_accounts.all()]
    return _wrap_text({"accounts": accounts})


register_tool(
    Tool(
        name="list_accounts",
        description=(
            "List the social media accounts this API key is allowed to act on. "
            "Returns id, platform, account_name, account_handle, connection_status, char_limit, "
            "escaped_chars, needs_title, and supports_first_comment. Call this first to discover which "
            "social_account_id values are valid and what each platform requires."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_accounts,
    )
)


# ---------------------------------------------------------------------------
# Tool: create_draft
# ---------------------------------------------------------------------------


def _create_draft(args: dict, context: dict[str, Any]) -> dict:
    _require_perm(context, "create_posts")
    api_key = context["api_key"]
    if "social_account_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "social_account_id is required")
    if "caption" not in args:
        raise JsonRpcError(INVALID_PARAMS, "caption is required")
    sa = _resolve_allowed_account(api_key, args["social_account_id"])
    proposed_publish_at = None
    if args.get("proposed_publish_at") is not None:
        proposed_publish_at = _parse_iso_datetime(args["proposed_publish_at"], "proposed_publish_at")
    try:
        post = create_post(
            workspace=api_key.workspace,
            social_account=sa,
            caption=args["caption"],
            title=args.get("title", ""),
            first_comment=args.get("first_comment", ""),
            internal_notes=args.get("internal_notes", ""),
            media_asset_ids=args.get("media_asset_ids") or [],
            proposed_publish_at=proposed_publish_at,
            author=api_key.issued_by if api_key.issued_by_id else None,
            status="draft",
        )
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text(_serialize_post(post, context))


register_tool(
    Tool(
        name="create_draft",
        description=(
            "Create a draft post against a connected account. The draft is saved but not "
            "queued for publishing; call schedule_post or the schedule tool later to publish. "
            "Optionally record a non-binding proposed_publish_at suggestion."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "social_account_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "ID of a SocialAccount in this key's allowlist (see list_accounts).",
                },
                "caption": {"type": "string", "maxLength": 10000},
                "title": {"type": "string", "default": "", "maxLength": 255},
                "first_comment": {
                    "type": "string",
                    "default": "",
                    "description": "Optional comment auto-posted after the main post.",
                },
                "internal_notes": {
                    "type": "string",
                    "default": "",
                    "maxLength": 10000,
                    "description": "Private team-only note. Never published to any platform.",
                },
                "media_asset_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "default": [],
                    "description": "MediaAsset UUIDs already uploaded to the workspace's media library.",
                },
                "proposed_publish_at": {
                    "type": "string",
                    "description": (
                        "Optional ISO-8601 UTC suggested publish time (e.g. 2026-06-01T14:00:00Z). "
                        "A non-binding draft hint shown in the drafts/approval views — stored as-is, "
                        "not validated against the future, and never queued for publishing."
                    ),
                },
            },
            "required": ["social_account_id", "caption"],
            "additionalProperties": False,
        },
        handler=_create_draft,
    )
)


# ---------------------------------------------------------------------------
# Tool: schedule_post — create + queue for publishing in one step
# ---------------------------------------------------------------------------


def _schedule_post(args: dict, context: dict[str, Any]) -> dict:
    # Mirrors the REST contract: scheduling sends the post into the
    # publisher's poll loop, which the composer permission model gates
    # on ``publish_directly`` (see apps/composer/views.py:797). Tools/
    # call to ``schedule_post`` requires the same.
    _require_perm(context, "create_posts")
    _require_perm(context, "publish_directly")
    api_key = context["api_key"]
    if "social_account_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "social_account_id is required")
    if "caption" not in args:
        raise JsonRpcError(INVALID_PARAMS, "caption is required")
    if "scheduled_at" not in args:
        raise JsonRpcError(INVALID_PARAMS, "scheduled_at is required (ISO 8601)")
    scheduled_at = _parse_iso_datetime(args["scheduled_at"], "scheduled_at")
    sa = _resolve_allowed_account(api_key, args["social_account_id"])
    # Platform quota is shared with REST; ``check_platform_quota``
    # raises ``HttpError(429,...)`` which we re-shape into a JSON-RPC
    # error so MCP clients see structured feedback rather than HTTP.
    try:
        check_platform_quota(sa)
    except HttpError as exc:
        raise JsonRpcError(
            INVALID_PARAMS,
            f"Per-platform daily quota reached for {sa.platform}: {exc.message}",
        ) from exc
    try:
        post = create_post(
            workspace=api_key.workspace,
            social_account=sa,
            caption=args["caption"],
            title=args.get("title", ""),
            first_comment=args.get("first_comment", ""),
            internal_notes=args.get("internal_notes", ""),
            media_asset_ids=args.get("media_asset_ids") or [],
            scheduled_at=scheduled_at,
            author=api_key.issued_by if api_key.issued_by_id else None,
            status="scheduled",
        )
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text(_serialize_post(post, context))


register_tool(
    Tool(
        name="schedule_post",
        description=(
            "Create a post and schedule it to publish at a specific UTC timestamp. "
            "The publisher polls every ~15s and will fire the post once the time elapses."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "social_account_id": {"type": "string", "format": "uuid"},
                "caption": {"type": "string", "maxLength": 10000},
                "scheduled_at": {
                    "type": "string",
                    "description": "ISO 8601 UTC timestamp (e.g. 2026-06-01T14:00:00Z)",
                },
                "title": {"type": "string", "default": "", "maxLength": 255},
                "first_comment": {"type": "string", "default": ""},
                "internal_notes": {
                    "type": "string",
                    "default": "",
                    "maxLength": 10000,
                    "description": "Private team-only note. Never published to any platform.",
                },
                "media_asset_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "default": [],
                },
            },
            "required": ["social_account_id", "caption", "scheduled_at"],
            "additionalProperties": False,
        },
        handler=_schedule_post,
    )
)


# ---------------------------------------------------------------------------
# Tool: get_post
# ---------------------------------------------------------------------------


def _get_post(args: dict, context: dict[str, Any]) -> dict:
    if "post_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "post_id is required")
    api_key = context["api_key"]
    post = _get_post_for_key(api_key, args["post_id"])
    return _wrap_text(_serialize_post(post, context))


register_tool(
    Tool(
        name="get_post",
        description=(
            "Retrieve a post by ID, including aggregate status and per-platform child state. "
            "Returns 'Post not found' for posts outside the API key's allowlist (same as for "
            "truly nonexistent IDs — the API never reveals which is which)."
        ),
        input_schema={
            "type": "object",
            "properties": {"post_id": {"type": "string", "format": "uuid"}},
            "required": ["post_id"],
            "additionalProperties": False,
        },
        handler=_get_post,
    )
)


# ---------------------------------------------------------------------------
# Tool: list_posts
# ---------------------------------------------------------------------------


_MCP_POST_LIMIT_DEFAULT = 50
_MCP_POST_LIMIT_MAX = 100


def _list_posts(args: dict, context: dict[str, Any]) -> dict:
    api_key = context["api_key"]

    status = args.get("status")
    if status is not None and status not in PlatformPost.Status.values:
        raise JsonRpcError(INVALID_PARAMS, f"status must be one of {', '.join(PlatformPost.Status.values)}")

    # ``or`` would swallow an explicit 0 as "unset" and hand back the default.
    raw_limit = args.get("limit")
    try:
        limit = _MCP_POST_LIMIT_DEFAULT if raw_limit is None else int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, f"limit must be an integer between 1 and {_MCP_POST_LIMIT_MAX}") from exc
    if limit < 1 or limit > _MCP_POST_LIMIT_MAX:
        raise JsonRpcError(INVALID_PARAMS, f"limit must be between 1 and {_MCP_POST_LIMIT_MAX}")

    try:
        offset = decode_page_cursor(args.get("cursor"))
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, "cursor is not a valid pagination cursor") from exc

    # The allowlist lives in the queryset (see ``_visible_posts_qs``), so paging
    # is over posts this key can actually see — no scan cap, nothing silently
    # dropped. ``id`` tiebreaks ``-created_at`` to keep offsets stable.
    qs = _visible_posts_qs(api_key).prefetch_related("platform_posts__social_account")
    if status:
        qs = qs.filter(Exists(PlatformPost.objects.filter(post_id=OuterRef("pk"), status=status)))
    qs = qs.order_by("-created_at", "id")

    # limit + 1 probes for a next page without a second COUNT query.
    rows = list(qs[offset : offset + limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    return _wrap_text(
        {
            "posts": [_serialize_post(post, context) for post in rows],
            "limit": limit,
            "next_cursor": encode_page_cursor(offset + limit) if has_more else None,
        }
    )


register_tool(
    Tool(
        name="list_posts",
        description=(
            "List posts in this API key's workspace, newest first (by creation time). Fills the gap "
            "left by get_post (which needs an id you may not have yet). Each item has the same shape "
            "as get_post (aggregate status + per-platform child state). Only posts whose every "
            "platform target is in the key's allowlist are returned. Optional `status` filter and "
            "`limit` (default 50, max 100). When more posts remain, `next_cursor` is non-null — pass "
            "it back as `cursor` for the next page; a null `next_cursor` means you have them all."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": list(PlatformPost.Status.values),
                    "description": "Optional per-platform status; a post matches if any target has it.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MCP_POST_LIMIT_MAX,
                    "default": _MCP_POST_LIMIT_DEFAULT,
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque cursor from a previous call's `next_cursor`.",
                },
            },
            "additionalProperties": False,
        },
        handler=_list_posts,
    )
)


# ---------------------------------------------------------------------------
# Tool: cancel_post
# ---------------------------------------------------------------------------


def _cancel_post(args: dict, context: dict[str, Any]) -> dict:
    from django.db import transaction

    _require_perm(context, "create_posts")
    if "post_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "post_id is required")
    api_key = context["api_key"]
    post = _get_post_for_key(api_key, args["post_id"])
    scheduled = [pp for pp in post.platform_posts.all() if pp.status == "scheduled"]
    if not scheduled:
        raise JsonRpcError(INVALID_PARAMS, "No scheduled platform posts to cancel")
    # Wrap the per-child loop in a single outer atomic so a mid-loop
    # ValueError (concurrent admin transition, state-machine rejection
    # on a later child) rolls back any earlier ``draft`` commits.
    # Mirrors the REST ``/cancel`` route's atomic block — without this,
    # a multi-account post could end up in a mixed draft/scheduled state
    # that neither the publisher nor the agent expects. Codex PR #53
    # flagged this asymmetry between REST and MCP.
    with transaction.atomic():
        for pp in scheduled:
            try:
                transition_platform_post(pp, "draft")
            except ValueError as exc:
                raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    post.refresh_from_db()
    return _wrap_text(_serialize_post(post, context))


register_tool(
    Tool(
        name="cancel_post",
        description=(
            "Cancel a scheduled post, transitioning it back to draft. "
            "No-op error if there are no scheduled children to cancel."
        ),
        input_schema={
            "type": "object",
            "properties": {"post_id": {"type": "string", "format": "uuid"}},
            "required": ["post_id"],
            "additionalProperties": False,
        },
        handler=_cancel_post,
    )
)


# ---------------------------------------------------------------------------
# Tool: schedule_draft — REST-parity transition of an existing draft post
# ---------------------------------------------------------------------------


def _schedule_draft(args: dict, context: dict[str, Any]) -> dict:
    """Promote every draft child of an existing post to ``scheduled``.

    Mirrors the REST ``POST /api/v1/posts/{post_id}/schedule`` route.
    Closes the asymmetry where MCP previously had no way to transition
    an existing draft to scheduled — ``schedule_post`` always creates a
    NEW post in scheduled state. Without this tool, "draft now, schedule
    later" via pure MCP forced clients to recreate the post or fall back
    to REST for the one transition.
    """
    from django.db import transaction

    _require_perm(context, "create_posts")
    # Same permission contract as the REST route: pushing a post into
    # the publisher's poll loop requires ``publish_directly``.
    _require_perm(context, "publish_directly")
    if "post_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "post_id is required")
    if "scheduled_at" not in args:
        raise JsonRpcError(INVALID_PARAMS, "scheduled_at is required (ISO 8601)")
    scheduled_at = _parse_iso_datetime(args["scheduled_at"], "scheduled_at")

    api_key = context["api_key"]
    post = _get_post_for_key(api_key, args["post_id"])
    drafts = [pp for pp in post.platform_posts.all() if pp.status == "draft"]
    if not drafts:
        raise JsonRpcError(INVALID_PARAMS, "No draft platform posts to schedule")

    # Per-platform 24h quota check, one per child, BEFORE we mutate
    # anything — over-quota fails the whole call with no partial commit.
    for pp in drafts:
        try:
            check_platform_quota(pp.social_account)
        except HttpError as exc:
            raise JsonRpcError(
                INVALID_PARAMS,
                f"Per-platform daily quota reached for {pp.social_account.platform}: {exc.message}",
            ) from exc

    # Wrap the per-child loop in a single outer atomic — same reasoning
    # as ``cancel_post``: a mid-loop ValueError (concurrent admin
    # transition, state-machine rejection on a later child, workspace
    # approval-mode rejection from ``transition_platform_post``) rolls
    # back any earlier ``scheduled`` commits.
    with transaction.atomic():
        for pp in drafts:
            try:
                transition_platform_post(pp, "scheduled", scheduled_at=scheduled_at)
            except ValueError as exc:
                raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    post.refresh_from_db()
    return _wrap_text(_serialize_post(post, context))


register_tool(
    Tool(
        name="schedule_draft",
        description=(
            "Schedule an EXISTING draft post — transitions every draft child to scheduled "
            "at the given UTC timestamp. Use this for the two-step flow "
            "'create_draft now, schedule_draft later'. For one-shot create-and-schedule, "
            "use schedule_post instead. Requires both create_posts and publish_directly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "format": "uuid"},
                "scheduled_at": {
                    "type": "string",
                    "description": "ISO 8601 UTC timestamp (e.g. 2026-06-01T14:00:00Z)",
                },
            },
            "required": ["post_id", "scheduled_at"],
            "additionalProperties": False,
        },
        handler=_schedule_draft,
    )
)


# ---------------------------------------------------------------------------
# Media tool schemas. Implementations live in ``apps.mcp.media``.
# ---------------------------------------------------------------------------

register_tool(
    Tool(
        name="search_media",
        description=(
            "Find media assets already uploaded to this workspace. Defaults to the 20 most "
            "recent assets that are ready to reference. Use this before uploading to avoid "
            "duplicating evergreen content. Returns the same item shape as GET /api/v1/media/."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional substring match on filename and tags.",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["image", "video", "gif", "document"],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "All tags must match (AND semantics).",
                },
                "folder_id": {"type": "string", "format": "uuid"},
                "is_starred": {"type": "boolean"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MCP_MEDIA_LIMIT_MAX,
                    "default": MCP_MEDIA_LIMIT_DEFAULT,
                },
                "cursor": {"type": "string", "description": "Opaque cursor returned by the previous page."},
            },
            "additionalProperties": False,
        },
        handler=_focused_handler("media", "search_media"),
    )
)


register_tool(
    Tool(
        name="get_media",
        description=(
            "Retrieve a single media asset by id. Same response shape as "
            "GET /api/v1/media/{id}. Use this to poll an upload's processing_status "
            "until it transitions from 'pending' to 'completed'."
        ),
        input_schema={
            "type": "object",
            "properties": {"media_id": {"type": "string", "format": "uuid"}},
            "required": ["media_id"],
            "additionalProperties": False,
        },
        handler=_focused_handler("media", "get_media"),
    )
)


register_tool(
    Tool(
        name="upload_media",
        description=(
            "Upload a small media file (≤1 MB raw / ~1.3 MB base64) via base64. "
            "For anything larger use POST /api/v1/media/ over REST instead — multipart "
            "can't ride a JSON-RPC envelope. Returns the same shape as the REST upload "
            "response; processing_status starts at 'pending' until the background task "
            "transitions it to 'completed'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "maxLength": 255},
                "content_base64": {
                    "type": "string",
                    "description": "Base64-encoded file content. Decoded size must be ≤1 MB.",
                },
                "content_type": {"type": "string"},
                "alt_text": {"type": "string", "maxLength": 2000},
                "title": {"type": "string", "maxLength": 255},
                "folder_id": {"type": "string", "format": "uuid"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["filename", "content_base64"],
            "additionalProperties": False,
        },
        handler=_focused_handler("media", "upload_media"),
    )
)


# ---------------------------------------------------------------------------
# Tools: request_media_upload / finalize_media_upload (presigned direct-to-R2)
# ---------------------------------------------------------------------------
#
# Large media (videos especially) can't ride a base64 JSON-RPC envelope, so the
# base64 upload_media above caps at 1 MB. These two tools let an OAuth caller
# upload large files entirely over MCP — no REST API key needed: request a
# presigned POST, upload the bytes straight to object storage, then finalize so
# the server validates the stored object and registers the asset. Because upload
# and the later create_draft both ride the same OAuth connection, they resolve
# the same workspace — the media is always found.

register_tool(
    Tool(
        name="request_media_upload",
        description=(
            "Step 1 of uploading large media (video, or any file >1 MB) over MCP — no REST "
            "API key required. Returns a short-lived presigned POST: 'url' plus 'fields' to "
            "upload the bytes directly to storage (multipart/form-data: send every 'fields' "
            "entry, then a 'file' field with the body), and an 'upload_id'. After the upload "
            "succeeds, call finalize_media_upload with the upload_id. For files ≤1 MB you can "
            "use upload_media (base64) instead. Requires the upload_media permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "maxLength": 255},
                "media_type": {
                    "type": "string",
                    "enum": ["image", "video", "gif", "document"],
                    "description": "Used only to size the upload cap; the stored type is re-sniffed at finalize.",
                },
                "content_type": {"type": "string", "description": "MIME type the client will send (e.g. video/mp4)."},
            },
            "required": ["filename", "media_type"],
            "additionalProperties": False,
        },
        handler=_focused_handler("media", "request_media_upload"),
    )
)


register_tool(
    Tool(
        name="finalize_media_upload",
        description=(
            "Step 2 of a presigned upload: call with the 'upload_id' from request_media_upload "
            "once the bytes are uploaded. The server validates the stored object (size, real "
            "MIME by magic bytes, storage quota) and registers the media asset. Returns the same "
            "shape as get_media; processing_status starts at 'pending'. Safe to retry — a second "
            "call with the same upload_id returns the same asset. Requires the upload_media permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "upload_id": {"type": "string", "format": "uuid"},
                "alt_text": {"type": "string", "maxLength": 2000},
                "title": {"type": "string", "maxLength": 255},
                "folder_id": {"type": "string", "format": "uuid"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["upload_id"],
            "additionalProperties": False,
        },
        handler=_focused_handler("media", "finalize_media_upload"),
    )
)


# ---------------------------------------------------------------------------
# Tool: get_account_analytics
# ---------------------------------------------------------------------------


def _get_account_analytics(args: dict, context: dict[str, Any]) -> dict:
    """Per-channel KPI summary over a rolling window.

    Body is byte-equal to ``GET /api/v1/analytics/accounts/{account_id}``
    because we reuse the same builder; ``test_rest_parity`` enforces
    that.
    """
    _require_perm(context, "view_analytics")
    if "account_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "account_id is required")
    days_raw = args.get("days", 30)
    # Match the REST surface's ``Query(ge=7, le=90)`` constraint so an
    # agent can't pick a wider window via MCP than via REST.
    if not isinstance(days_raw, int) or isinstance(days_raw, bool) or days_raw < 7 or days_raw > 90:
        raise JsonRpcError(INVALID_PARAMS, "days must be an integer between 7 and 90")
    api_key = context["api_key"]
    sa = _resolve_allowed_account(api_key, args["account_id"])
    return _wrap_text(build_account_analytics(sa, days_raw).model_dump(mode="json"))


register_tool(
    Tool(
        name="get_account_analytics",
        description=(
            "Read a channel's analytics summary over a rolling window: hero KPI metrics "
            "(views/likes/reach/etc.), an engagement-rate card when the platform supports it, "
            "and follower growth. Each metric is returned as ``{value, delta, series, kind}`` "
            "where ``delta`` is the percent change vs. the prior equal-length window and "
            "``series`` is the daily sparkline. Includes ``captured_at`` and ``next_sync_eta`` "
            "so an agent can pick a sensible poll delay. Platforms without an analytics surface "
            "(LinkedIn Personal, Bluesky, Mastodon) return ``analytics_available: false`` with "
            "``unavailable_reason``. Requires the view_analytics permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "SocialAccount ID. Must be in this API key's allowlist.",
                },
                "days": {
                    "type": "integer",
                    "minimum": 7,
                    "maximum": 90,
                    "default": 30,
                    "description": "Rolling window size in days. 7, 30, and 90 are the typical values.",
                },
            },
            "required": ["account_id"],
            "additionalProperties": False,
        },
        handler=_focused_handler("analytics", "get_account_analytics"),
    )
)


# ---------------------------------------------------------------------------
# Tool: get_post_analytics
# ---------------------------------------------------------------------------


def _get_post_analytics(args: dict, context: dict[str, Any]) -> dict:
    """Per-post analytics with one envelope per PlatformPost child.

    Designed for the polling loop after ``schedule_post`` /
    ``create_draft``: pass the same ``post_id`` you got back from
    creation and iterate until ``next_sync_eta`` recommends the next
    poll. Drafts and scheduled posts return a valid envelope with empty
    ``metric_tiles`` so the loop has a stable shape from day zero.
    """
    _require_perm(context, "view_analytics")
    if "post_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "post_id is required")
    api_key = context["api_key"]
    post = _get_post_for_key(api_key, args["post_id"])
    return _wrap_text(build_post_analytics(post).model_dump(mode="json"))


register_tool(
    Tool(
        name="get_post_analytics",
        description=(
            "Read a post's analytics, broken down per platform. For each PlatformPost child "
            "returns the latest value and a since-publish daily sparkline for every metric the "
            "platform reports, plus ``captured_at`` and ``next_sync_eta`` for polling. Drafts "
            "and scheduled posts return an empty ``metric_tiles`` array (not an error), so this "
            "tool is safe to call in a polling loop right after ``schedule_post``. Platforms "
            "without analytics (LinkedIn Personal, Bluesky, Mastodon) carry "
            "``analytics_available: false`` per child. Requires the view_analytics permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "post_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Parent Post ID (the same one returned by create_draft / schedule_post).",
                },
            },
            "required": ["post_id"],
            "additionalProperties": False,
        },
        handler=_focused_handler("analytics", "get_post_analytics"),
    )
)
