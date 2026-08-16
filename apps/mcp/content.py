"""MCP idea and draft content tools backed by shared composer services."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError

from apps.composer.content_services import (
    convert_idea_to_draft,
    create_idea,
    serialize_idea,
    update_draft_fields,
    update_idea,
)
from apps.composer.models import Idea
from apps.composer.services import clone_post
from apps.mcp.errors import DomainError
from apps.mcp.protocol import JsonRpcError
from apps.mcp.registry import Tool, register_tool
from apps.mcp.results import decode_page_cursor, encode_page_cursor, success_result
from apps.social_accounts.models import SocialAccount


def _uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("invalid_request", f"{field} must be a valid UUID.") from exc


def _idea(context: dict[str, Any], idea_id: str) -> Idea:
    try:
        return Idea.objects.select_related("workspace").get(
            id=_uuid(idea_id, "idea_id"),
            workspace=context["workspace"],
        )
    except (Idea.DoesNotExist, ValidationError) as exc:
        raise DomainError("resource_not_found", "Idea not found.") from exc


def _post(context: dict[str, Any], post_id: str):
    from apps.mcp.handlers import _get_post_for_key

    try:
        return _get_post_for_key(context["api_key"], post_id)
    except JsonRpcError as exc:
        raise DomainError("resource_not_found", "Post not found.") from exc


def _serialized_post(post, context: dict[str, Any]) -> dict[str, Any]:
    from apps.mcp.handlers import _serialize_post

    post = type(post).objects.filter(id=post.id).prefetch_related("platform_posts__social_account").get()
    return _serialize_post(post, context)


def _list_ideas(args: dict, context: dict[str, Any]) -> dict:
    limit = args.get("limit", 50)
    try:
        offset = decode_page_cursor(args.get("cursor"))
    except ValueError as exc:
        raise DomainError("invalid_request", "cursor is invalid.") from exc
    queryset = Idea.objects.filter(workspace=context["workspace"]).order_by("-created_at", "-id")
    if args.get("status"):
        queryset = queryset.filter(status=args["status"])
    page = list(queryset[offset : offset + limit + 1])
    has_more = len(page) > limit
    ideas = page[:limit]
    return success_result(
        {
            "ideas": [serialize_idea(idea) for idea in ideas],
            "limit": limit,
            "next_cursor": encode_page_cursor(offset + limit) if has_more else None,
        }
    )


register_tool(
    Tool(
        name="list_ideas",
        description="List content ideas in an authorized workspace using cursor pagination.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": list(Idea.Status.values)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_list_ideas,
    )
)


def _create_idea(args: dict, context: dict[str, Any]) -> dict:
    try:
        idea = create_idea(
            workspace=context["workspace"],
            author=context["principal"].user,
            title=args["title"],
            description=args.get("description", ""),
            tags=args.get("tags"),
            status=args.get("status", Idea.Status.UNASSIGNED),
            group_id=_uuid(args["group_id"], "group_id") if args.get("group_id") else None,
        )
    except ValueError as exc:
        raise DomainError("invalid_request", str(exc)) from exc
    return success_result(serialize_idea(idea))


register_tool(
    Tool(
        name="create_idea",
        description="Create a workspace content idea without publishing or scheduling anything.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 255},
                "description": {"type": "string", "maxLength": 10000},
                "tags": {"type": "array", "items": {"type": "string", "maxLength": 100}},
                "status": {"type": "string", "enum": list(Idea.Status.values)},
                "group_id": {"type": "string", "format": "uuid"},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_create_idea,
    )
)


def _update_idea(args: dict, context: dict[str, Any]) -> dict:
    idea = _idea(context, args["idea_id"])
    kwargs: dict[str, Any] = {key: args[key] for key in ("title", "description", "tags", "status") if key in args}
    if "group_id" in args:
        kwargs["group_id"] = _uuid(args["group_id"], "group_id") if args["group_id"] else None
    try:
        update_idea(idea, **kwargs)
    except ValueError as exc:
        raise DomainError("invalid_request", str(exc)) from exc
    return success_result(serialize_idea(idea))


register_tool(
    Tool(
        name="update_idea",
        description="Update fields on an existing workspace content idea.",
        input_schema={
            "type": "object",
            "properties": {
                "idea_id": {"type": "string", "format": "uuid"},
                "title": {"type": "string", "minLength": 1, "maxLength": 255},
                "description": {"type": "string", "maxLength": 10000},
                "tags": {"type": "array", "items": {"type": "string", "maxLength": 100}},
                "status": {"type": "string", "enum": list(Idea.Status.values)},
                "group_id": {"type": ["string", "null"], "format": "uuid"},
            },
            "required": ["idea_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_update_idea,
    )
)


def _convert_idea_to_draft(args: dict, context: dict[str, Any]) -> dict:
    idea = _idea(context, args["idea_id"])
    allowed_ids = context["workspace_context"].allowed_account_ids
    requested = [_uuid(value, "social_account_ids") for value in args.get("social_account_ids", [])]
    queryset = SocialAccount.objects.filter(workspace=context["workspace"], id__in=allowed_ids)
    if requested:
        accounts = list(queryset.filter(id__in=requested).order_by("platform", "account_name", "id"))
        if len(accounts) != len(set(requested)):
            raise DomainError("forbidden", "One or more social accounts are not authorized.")
    else:
        accounts = list(
            queryset.filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED).order_by(
                "platform", "account_name", "id"
            )
        )
    if not accounts:
        raise DomainError(
            "invalid_request",
            "Choose at least one connected, authorized social account.",
        )
    try:
        post = convert_idea_to_draft(
            idea,
            author=context["principal"].user,
            social_accounts=accounts,
        )
    except ValueError as exc:
        raise DomainError("invalid_request", str(exc)) from exc
    return success_result(_serialized_post(post, context))


register_tool(
    Tool(
        name="convert_idea_to_draft",
        description="Convert one idea into an editable draft for authorized social accounts.",
        input_schema={
            "type": "object",
            "properties": {
                "idea_id": {"type": "string", "format": "uuid"},
                "social_account_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "uniqueItems": True,
                },
            },
            "required": ["idea_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_convert_idea_to_draft,
    )
)


def _update_draft(args: dict, context: dict[str, Any]) -> dict:
    post = _post(context, args["post_id"])
    if post.status not in {"draft", "changes_requested", "rejected", "approved"}:
        raise DomainError("invalid_state", "Only a non-scheduled editable draft can be updated with this tool.")
    kwargs = {key: args[key] for key in ("title", "caption", "first_comment", "internal_notes", "tags") if key in args}
    try:
        update_draft_fields(post, **kwargs)
    except ValueError as exc:
        raise DomainError("invalid_request", str(exc)) from exc
    return success_result(_serialized_post(post, context))


_DRAFT_FIELDS = {
    "post_id": {"type": "string", "format": "uuid"},
    "title": {"type": "string", "maxLength": 255},
    "caption": {"type": "string", "maxLength": 10000},
    "first_comment": {"type": "string", "maxLength": 10000},
    "internal_notes": {"type": "string", "maxLength": 10000},
    "tags": {"type": "array", "items": {"type": "string", "maxLength": 100}},
}

register_tool(
    Tool(
        name="update_draft",
        description="Update editable fields on an authorized draft post.",
        input_schema={
            "type": "object",
            "properties": _DRAFT_FIELDS,
            "required": ["post_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_update_draft,
    )
)


def _clone_post(args: dict, context: dict[str, Any]) -> dict:
    source = _post(context, args["post_id"])
    post = clone_post(source, author=context["principal"].user)
    return success_result(_serialized_post(post, context))


register_tool(
    Tool(
        name="clone_post",
        description="Create an editable draft copy of an authorized post and its targets/media.",
        input_schema={
            "type": "object",
            "properties": {"post_id": {"type": "string", "format": "uuid"}},
            "required": ["post_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_clone_post,
    )
)
