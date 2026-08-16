"""Authorized MCP tools for BrightBean's unified social inbox."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from apps.inbox.models import InboxMessage
from apps.inbox.services import add_note, assign_message, send_reply, set_status
from apps.mcp.errors import DomainError
from apps.mcp.registry import Tool, register_tool
from apps.mcp.results import decode_page_cursor, encode_page_cursor, success_result


def _messages(context: dict[str, Any]):
    workspace_context = context["workspace_context"]
    return InboxMessage.objects.filter(
        workspace_id=workspace_context.workspace_id,
        social_account_id__in=workspace_context.allowed_account_ids,
    )


def _message(context: dict[str, Any], message_id: str) -> InboxMessage:
    try:
        parsed = UUID(message_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainError("invalid_request", "message_id must be a valid UUID.") from exc
    message = (
        _messages(context)
        .select_related("social_account", "assigned_to", "workspace")
        .filter(id=parsed)
        .first()
    )
    if message is None:
        raise DomainError("resource_not_found", "Inbox message not found.")
    return message


def _summary(message: InboxMessage, *, include_body: bool = True) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "workspace_id": str(message.workspace_id),
        "social_account_id": str(message.social_account_id),
        "platform": message.social_account.platform,
        "message_type": message.message_type,
        "sender_name": message.sender_name,
        "sender_handle": message.sender_handle,
        "body": message.body if include_body else "",
        "sentiment": message.sentiment,
        "status": message.status,
        "assigned_to_id": str(message.assigned_to_id) if message.assigned_to_id else None,
        "received_at": message.received_at.isoformat(),
    }


def _list_inbox(args: dict, context: dict[str, Any]) -> dict:
    try:
        offset = decode_page_cursor(args.get("cursor"))
    except ValueError as exc:
        raise DomainError("invalid_request", "cursor is invalid.") from exc
    limit = args.get("limit", 50)
    queryset = _messages(context).select_related("social_account", "assigned_to")
    for field in ("status", "message_type", "sentiment"):
        if args.get(field):
            queryset = queryset.filter(**{field: args[field]})
    if args.get("social_account_id"):
        queryset = queryset.filter(social_account_id=args["social_account_id"])
    page = list(queryset.order_by("-received_at", "id")[offset : offset + limit + 1])
    has_more = len(page) > limit
    return success_result(
        {
            "messages": [_summary(message) for message in page[:limit]],
            "limit": limit,
            "next_cursor": encode_page_cursor(offset + limit) if has_more else None,
        }
    )


register_tool(
    Tool(
        name="list_inbox",
        description="List authorized inbox messages with safe filters and cursor pagination.",
        input_schema={
            "type": "object",
            "properties": {
                "social_account_id": {"type": "string", "format": "uuid"},
                "status": {"type": "string", "enum": list(InboxMessage.Status.values)},
                "message_type": {"type": "string", "enum": list(InboxMessage.MessageType.values)},
                "sentiment": {"type": "string", "enum": list(InboxMessage.Sentiment.values)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
        handler=_list_inbox,
    )
)


def _get_inbox_message(args: dict, context: dict[str, Any]) -> dict:
    message = _message(context, args["message_id"])
    payload = _summary(message)
    payload["replies"] = [
        {
            "id": str(reply.id),
            "author_id": str(reply.author_id) if reply.author_id else None,
            "body": reply.body,
            "sent_at": reply.sent_at.isoformat(),
        }
        for reply in message.replies.order_by("sent_at", "id")
    ]
    payload["notes"] = [
        {
            "id": str(note.id),
            "author_id": str(note.author_id) if note.author_id else None,
            "body": note.body,
            "created_at": note.created_at.isoformat(),
        }
        for note in message.internal_notes.order_by("created_at", "id")
    ]
    return success_result(payload)


register_tool(
    Tool(
        name="get_inbox_message",
        description="Read one authorized inbox message with its replies and internal notes.",
        input_schema={
            "type": "object",
            "properties": {"message_id": {"type": "string", "format": "uuid"}},
            "required": ["message_id"],
            "additionalProperties": False,
        },
        handler=_get_inbox_message,
    )
)


def _add_inbox_note(args: dict, context: dict[str, Any]) -> dict:
    note = add_note(
        message=_message(context, args["message_id"]),
        author=context["principal"].user,
        body=args["body"],
    )
    return success_result(
        {
            "id": str(note.id),
            "message_id": str(note.inbox_message_id),
            "author_id": str(note.author_id) if note.author_id else None,
            "body": note.body,
            "created_at": note.created_at.isoformat(),
        }
    )


register_tool(
    Tool(
        name="add_inbox_note",
        description="Add a team-only note to an authorized inbox message.",
        input_schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "format": "uuid"},
                "body": {"type": "string", "minLength": 1, "maxLength": 10000},
            },
            "required": ["message_id", "body"],
            "additionalProperties": False,
        },
        handler=_add_inbox_note,
    )
)


def _assign_inbox_message(args: dict, context: dict[str, Any]) -> dict:
    try:
        message = assign_message(
            message=_message(context, args["message_id"]),
            actor=context["principal"].user,
            user_id=args.get("user_id"),
        )
    except ValueError as exc:
        raise DomainError("invalid_request", str(exc)) from exc
    return success_result(_summary(message))


register_tool(
    Tool(
        name="assign_inbox_message",
        description="Assign or unassign an inbox message to a current workspace member.",
        input_schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "format": "uuid"},
                "user_id": {"type": ["string", "null"], "format": "uuid"},
            },
            "required": ["message_id"],
            "additionalProperties": False,
        },
        handler=_assign_inbox_message,
    )
)


def _set_inbox_status(args: dict, context: dict[str, Any]) -> dict:
    try:
        message = set_status(message=_message(context, args["message_id"]), status=args["status"])
    except ValueError as exc:
        raise DomainError("invalid_request", str(exc)) from exc
    return success_result(_summary(message))


register_tool(
    Tool(
        name="set_inbox_status",
        description="Set an authorized inbox message to unread, open, resolved, or archived.",
        input_schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "format": "uuid"},
                "status": {"type": "string", "enum": list(InboxMessage.Status.values)},
            },
            "required": ["message_id", "status"],
            "additionalProperties": False,
        },
        handler=_set_inbox_status,
    )
)


def _send_inbox_reply(args: dict, context: dict[str, Any]) -> dict:
    reply = send_reply(
        message=_message(context, args["message_id"]),
        author=context["principal"].user,
        body=args["body"],
    )
    return success_result(
        {
            "id": str(reply.id),
            "message_id": str(reply.inbox_message_id),
            "status": reply.inbox_message.status,
            "sent_at": reply.sent_at.isoformat(),
        }
    )


register_tool(
    Tool(
        name="send_inbox_reply",
        description="Preview, then send exactly one externally visible reply to an inbox message.",
        input_schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "format": "uuid"},
                "body": {"type": "string", "minLength": 1, "maxLength": 10000},
            },
            "required": ["message_id", "body"],
            "additionalProperties": False,
        },
        handler=_send_inbox_reply,
    )
)
