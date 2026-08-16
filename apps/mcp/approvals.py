"""MCP editorial comments and confirmed approval transitions."""

from __future__ import annotations

from typing import Any

from apps.approvals.comments import create_comment, get_comments_for_post
from apps.approvals.models import PostComment
from apps.approvals.services import approve_post, reject_post, request_changes, submit_for_review
from apps.mcp.content import _post
from apps.mcp.errors import DomainError
from apps.mcp.registry import Tool, register_tool
from apps.mcp.results import decode_page_cursor, encode_page_cursor, success_result


def _comment_payload(comment: PostComment) -> dict[str, Any]:
    return {
        "id": str(comment.id),
        "post_id": str(comment.post_id),
        "author_id": str(comment.author_id) if comment.author_id else None,
        "author_name": comment.author.display_name if comment.author else "Former user",
        "parent_comment_id": str(comment.parent_comment_id) if comment.parent_comment_id else None,
        "body": comment.body,
        "visibility": comment.visibility,
        "created_at": comment.created_at.isoformat(),
        "updated_at": comment.updated_at.isoformat(),
    }


def _list_post_comments(args: dict, context: dict[str, Any]) -> dict:
    post = _post(context, args["post_id"])
    try:
        offset = decode_page_cursor(args.get("cursor"))
    except ValueError as exc:
        raise DomainError("invalid_request", "cursor is invalid.") from exc
    limit = args.get("limit", 50)
    top_level = get_comments_for_post(post, context["principal"].user)
    allowed_ids: list[Any] = []
    for comment in top_level:
        allowed_ids.append(comment.id)
        allowed_ids.extend(reply.id for reply in comment.replies.all())
    comments = PostComment.objects.filter(id__in=allowed_ids)
    if context["membership"].workspace_role == "client":
        comments = comments.filter(visibility=PostComment.Visibility.EXTERNAL)
    page = list(comments.select_related("author").order_by("created_at", "id")[offset : offset + limit + 1])
    has_more = len(page) > limit
    return success_result(
        {
            "comments": [_comment_payload(comment) for comment in page[:limit]],
            "limit": limit,
            "next_cursor": encode_page_cursor(offset + limit) if has_more else None,
        }
    )


register_tool(
    Tool(
        name="list_post_comments",
        description="List authorized editorial comments, respecting client visibility rules.",
        input_schema={
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "format": "uuid"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                "cursor": {"type": "string"},
            },
            "required": ["post_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_list_post_comments,
    )
)


def _add_post_comment(args: dict, context: dict[str, Any]) -> dict:
    post = _post(context, args["post_id"])
    visibility = args.get("visibility", PostComment.Visibility.INTERNAL)
    if context["membership"].workspace_role == "client" and visibility != PostComment.Visibility.EXTERNAL:
        raise DomainError("forbidden", "Client comments must use external visibility.")
    try:
        comment = create_comment(
            post,
            context["principal"].user,
            args["body"],
            visibility,
            parent_id=args.get("parent_comment_id"),
        )
    except ValueError as exc:
        raise DomainError("invalid_request", str(exc)) from exc
    return success_result(_comment_payload(comment))


register_tool(
    Tool(
        name="add_post_comment",
        description="Add an internal or external editorial comment without an attachment.",
        input_schema={
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "format": "uuid"},
                "body": {"type": "string", "minLength": 1, "maxLength": 10000},
                "visibility": {"type": "string", "enum": list(PostComment.Visibility.values)},
                "parent_comment_id": {"type": "string", "format": "uuid"},
            },
            "required": ["post_id", "body"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_add_post_comment,
    )
)


def _transition(args: dict, context: dict[str, Any], action: str) -> dict:
    post = _post(context, args["post_id"])
    services = {
        "submit_for_review": lambda: submit_for_review(post, context["principal"].user, context["workspace"]),
        "approve_post": lambda: approve_post(
            post, context["principal"].user, context["workspace"], args.get("comment", "")
        ),
        "request_changes": lambda: request_changes(
            post, context["principal"].user, context["workspace"], args.get("comment", "")
        ),
        "reject_post": lambda: reject_post(
            post, context["principal"].user, context["workspace"], args.get("comment", "")
        ),
    }
    try:
        moved = services[action]()
    except ValueError as exc:
        raise DomainError("invalid_request", str(exc)) from exc
    if not moved:
        raise DomainError("invalid_state", "The post has no targets eligible for this transition.")
    post.refresh_from_db()
    return success_result({"id": str(post.id), "post_id": str(post.id), "status": post.status})


def _transition_tool(name: str, description: str, *, comment_required: bool = False) -> None:
    properties: dict[str, Any] = {
        "post_id": {"type": "string", "format": "uuid"},
        "comment": {"type": "string", "maxLength": 10000},
    }
    required = ["post_id", *(("comment",) if comment_required else ())]

    def handler(args: dict[str, Any], context: dict[str, Any]) -> dict:
        return _transition(args, context, name)

    register_tool(
        Tool(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            handler=handler,
        )
    )


_transition_tool("submit_for_review", "Submit eligible draft targets for internal review.")
_transition_tool("approve_post", "Approve eligible post targets under the workspace approval policy.")
_transition_tool("request_changes", "Request changes with a required editorial comment.", comment_required=True)
_transition_tool(
    "reject_post", "Reject eligible post targets with a required editorial comment.", comment_required=True
)
