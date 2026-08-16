"""Canonical metadata-rich registry for BrightBean MCP tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from apps.api.schemas import (
    AccountAnalyticsResponse,
    AccountSummary,
    MediaAssetResponse,
    PostAnalyticsResponse,
    PostResponse,
)
from apps.mcp.errors import DOMAIN_ERROR_OUTPUT_SCHEMA

ToolHandler = Callable[[dict[str, Any], Any], dict[str, Any]]


@dataclass(frozen=True)
class ToolAnnotations:
    """Framework-neutral representation of MCP tool behavior hints."""

    title: str | None = None
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False

    def to_mcp_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }


def _default_output_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": True}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    output_schema: dict[str, Any] = field(default_factory=_default_output_schema)
    annotations: ToolAnnotations = field(default_factory=ToolAnnotations)
    enabled: bool = True
    risk_level: str = "low"
    required_scope: str = "mcp"
    required_permission: str | None = None
    required_permissions: tuple[str, ...] = ()
    confirmation_required: bool = False
    workspace_scoped: bool = False

    def __post_init__(self) -> None:
        """Keep the old singular permission accessor during policy migration."""
        if self.required_permissions:
            if self.required_permission not in (None, self.required_permissions[0]):
                raise ValueError("required_permission must match the first required_permissions entry")
            object.__setattr__(self, "required_permission", self.required_permissions[0])
        elif self.required_permission is not None:
            object.__setattr__(self, "required_permissions", (self.required_permission,))

    def to_mcp_dict(self) -> dict[str, Any]:
        """Return JSON metadata shared by SDK and legacy discovery."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": self.annotations.to_mcp_dict(),
        }


class ToolRegistry:
    """Own registration, discovery, and stale-call lookup semantics."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate MCP tool registered: {tool.name}")
        self._tools[tool.name] = tool

    def discover(self) -> list[Tool]:
        return sorted((tool for tool in self._tools.values() if tool.enabled), key=lambda tool: tool.name)

    def get(self, name: str, *, include_disabled: bool = False) -> Tool | None:
        tool = self._tools.get(name)
        if tool is None or (not tool.enabled and not include_disabled):
            return None
        return tool

    def clear(self) -> None:
        self._tools.clear()


registry = ToolRegistry()


_READ_TOOLS = frozenset(
    {
        "get_account_analytics",
        "get_account_health",
        "get_best_times",
        "get_calendar",
        "get_media",
        "get_inbox_message",
        "get_post",
        "get_post_analytics",
        "get_workspace_context",
        "get_workspace_analytics",
        "list_accounts",
        "list_ideas",
        "list_inbox",
        "list_post_comments",
        "list_posts",
        "list_queues",
        "list_workspaces",
        "search_media",
    }
)
_READ_PERMISSIONS = {
    "get_inbox_message": ("use_inbox",),
    "list_inbox": ("use_inbox",),
}
_PUBLISH_TOOLS = frozenset(
    {
        "approve_post",
        "cancel_post",
        "enqueue_post",
        "publish_post",
        "reject_post",
        "reschedule_post",
        "request_changes",
        "schedule_draft",
        "schedule_post",
        "submit_for_review",
    }
)
_PUBLISH_PERMISSIONS = {
    "approve_post": ("approve_posts",),
    "cancel_post": ("create_posts",),
    "enqueue_post": ("create_posts", "publish_directly"),
    "publish_post": ("create_posts", "publish_directly"),
    "reject_post": ("approve_posts",),
    "request_changes": ("approve_posts",),
    "reschedule_post": ("create_posts", "publish_directly"),
    "schedule_draft": ("create_posts", "publish_directly"),
    "schedule_post": ("create_posts", "publish_directly"),
    "submit_for_review": ("create_posts",),
}
_CONTENT_TOOLS = frozenset(
    {
        "clone_post",
        "convert_idea_to_draft",
        "add_post_comment",
        "create_draft",
        "create_idea",
        "finalize_media_upload",
        "request_media_upload",
        "update_draft",
        "update_idea",
        "upload_media",
        "add_inbox_note",
        "assign_inbox_message",
        "set_inbox_status",
    }
)
_CONTENT_PERMISSIONS = {
    "add_post_comment": ("create_posts",),
    "clone_post": ("create_posts",),
    "convert_idea_to_draft": ("create_posts",),
    "create_draft": ("create_posts",),
    "create_idea": ("create_posts",),
    "finalize_media_upload": ("upload_media",),
    "request_media_upload": ("upload_media",),
    "update_draft": ("create_posts",),
    "update_idea": ("create_posts",),
    "upload_media": ("upload_media",),
    "add_inbox_note": ("reply_from_inbox",),
    "assign_inbox_message": ("reply_from_inbox",),
    "set_inbox_status": ("reply_from_inbox",),
}
_INBOX_REPLY_TOOLS = frozenset({"send_inbox_reply"})
_GLOBAL_TOOLS = frozenset({"list_workspaces"})

_CONFIRMATION_PREVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirmation_required": {"const": True},
        "confirmation_token": {"type": "string"},
        "payload_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "expires_at": {"type": "string", "format": "date-time"},
        "preview": {"type": "object", "additionalProperties": True},
    },
    "required": [
        "confirmation_required",
        "confirmation_token",
        "payload_hash",
        "expires_at",
        "preview",
    ],
    "additionalProperties": False,
}

_CONFIRMED_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "post_id": {"type": "string"},
        "status": {"type": "string"},
        "scheduled_at": {"type": ["string", "null"]},
        "replayed": {"type": "boolean"},
    },
    "required": ["replayed"],
    "additionalProperties": True,
}


class _StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ListAccountsOutput(_StrictOutput):
    accounts: list[AccountSummary]


class _ListPostsOutput(_StrictOutput):
    posts: list[PostResponse]
    limit: int
    next_cursor: str | None


class _SearchMediaOutput(_StrictOutput):
    items: list[MediaAssetResponse]
    limit: int
    next_cursor: str | None


class _RequestMediaUploadOutput(_StrictOutput):
    upload_id: str
    method: str
    url: str
    fields: dict[str, str]
    max_bytes: int
    expires_at: str
    instructions: str


class _WorkspaceSummary(_StrictOutput):
    id: UUID
    name: str
    organization_id: UUID
    role: str
    timezone: str


class _ListWorkspacesOutput(_StrictOutput):
    workspaces: list[_WorkspaceSummary]


class _BrandColors(_StrictOutput):
    primary: str
    secondary: str


class _NamedColor(_StrictOutput):
    id: UUID
    name: str
    color: str


class _NamedItem(_StrictOutput):
    id: UUID
    name: str


class _TemplateSummary(_NamedItem):
    description: str


class _WorkspaceContextOutput(_StrictOutput):
    id: UUID
    name: str
    description: str
    timezone: str
    brand_colors: _BrandColors
    default_hashtags: list[str]
    approval_policy: str
    categories: list[_NamedColor]
    tags: list[_NamedItem]
    templates: list[_TemplateSummary]


class _AccountHealthOutput(AccountSummary):
    healthy: bool
    needs_reconnect: bool
    issues: list[str]
    last_health_check_at: datetime | None
    reconnect_path: str


class _IdeaOutput(_StrictOutput):
    id: UUID
    workspace_id: UUID
    title: str
    description: str
    tags: list[str]
    status: str
    group_id: UUID | None
    media_asset_id: UUID | None
    post_id: UUID | None
    created_at: datetime
    updated_at: datetime


class _ListIdeasOutput(_StrictOutput):
    ideas: list[_IdeaOutput]
    limit: int
    next_cursor: str | None


class _CommentOutput(_StrictOutput):
    id: UUID
    post_id: UUID
    author_id: UUID | None
    author_name: str
    parent_comment_id: UUID | None
    body: str
    visibility: str
    created_at: datetime
    updated_at: datetime


class _ListCommentsOutput(_StrictOutput):
    comments: list[_CommentOutput]
    limit: int
    next_cursor: str | None


class _CalendarPost(_StrictOutput):
    post_id: UUID
    platform_post_id: UUID
    social_account_id: UUID
    platform: str
    title: str
    status: str
    scheduled_at: datetime | None


class _CalendarEvent(_StrictOutput):
    id: UUID
    title: str
    description: str
    start_date: str
    end_date: str
    color: str


class _CalendarOutput(_StrictOutput):
    start_date: str
    end_date: str
    posts: list[_CalendarPost]
    events: list[_CalendarEvent]


class _QueueOutput(_StrictOutput):
    id: UUID
    name: str
    social_account_id: UUID
    platform: str
    category_id: UUID | None
    is_active: bool
    entry_count: int


class _ListQueuesOutput(_StrictOutput):
    queues: list[_QueueOutput]


class _TransitionOutput(_StrictOutput):
    id: UUID
    post_id: UUID
    status: str


class _ScheduledTransitionOutput(_TransitionOutput):
    scheduled_at: datetime


class _QueueTransitionOutput(_ScheduledTransitionOutput):
    queue_id: UUID


class _WorkspaceAnalyticsOutput(_StrictOutput):
    workspace_id: UUID
    days: int
    account_count: int
    analytics_available_count: int
    captured_at: datetime | None
    accounts: list[AccountAnalyticsResponse]


class _BestTimeRecommendation(_StrictOutput):
    day_of_week: int
    day_name: str
    hour: int
    local_time: str
    score: float
    sample_size: int


class _BestTimesOutput(_StrictOutput):
    workspace_id: UUID
    account_id: UUID | None
    days: int
    timezone: str
    status: str
    minimum_sample_size: int
    analyzed_posts: int
    recommendations: list[_BestTimeRecommendation]


class _InboxMessageSummary(_StrictOutput):
    id: UUID
    workspace_id: UUID
    social_account_id: UUID
    platform: str
    message_type: str
    sender_name: str
    sender_handle: str
    body: str
    sentiment: str
    status: str
    assigned_to_id: UUID | None
    received_at: datetime


class _InboxThreadItem(_StrictOutput):
    id: UUID
    author_id: UUID | None
    body: str


class _InboxReplyItem(_InboxThreadItem):
    sent_at: datetime


class _InboxNoteItem(_InboxThreadItem):
    created_at: datetime


class _InboxMessageDetail(_InboxMessageSummary):
    replies: list[_InboxReplyItem]
    notes: list[_InboxNoteItem]


class _ListInboxOutput(_StrictOutput):
    messages: list[_InboxMessageSummary]
    limit: int
    next_cursor: str | None


class _InboxNoteOutput(_InboxThreadItem):
    message_id: UUID
    created_at: datetime


class _InboxReplyOutput(_StrictOutput):
    id: UUID
    message_id: UUID
    status: str
    sent_at: datetime


def _typed_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Advertise one exact success shape plus BrightBean's common error shape."""
    success_schema = model.model_json_schema(mode="serialization")
    definitions = success_schema.pop("$defs", None)
    schema: dict[str, Any] = {
        "type": "object",
        "oneOf": [success_schema, DOMAIN_ERROR_OUTPUT_SCHEMA],
    }
    if definitions:
        schema["$defs"] = definitions
    return schema


_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_workspaces": _typed_output_schema(_ListWorkspacesOutput),
    "get_workspace_context": _typed_output_schema(_WorkspaceContextOutput),
    "get_account_health": _typed_output_schema(_AccountHealthOutput),
    "list_ideas": _typed_output_schema(_ListIdeasOutput),
    "create_idea": _typed_output_schema(_IdeaOutput),
    "update_idea": _typed_output_schema(_IdeaOutput),
    "add_post_comment": _typed_output_schema(_CommentOutput),
    "list_post_comments": _typed_output_schema(_ListCommentsOutput),
    "get_calendar": _typed_output_schema(_CalendarOutput),
    "list_queues": _typed_output_schema(_ListQueuesOutput),
    "submit_for_review": _typed_output_schema(_TransitionOutput),
    "approve_post": _typed_output_schema(_TransitionOutput),
    "request_changes": _typed_output_schema(_TransitionOutput),
    "reject_post": _typed_output_schema(_TransitionOutput),
    "enqueue_post": _typed_output_schema(_QueueTransitionOutput),
    "reschedule_post": _typed_output_schema(_ScheduledTransitionOutput),
    "publish_post": _typed_output_schema(_ScheduledTransitionOutput),
    "get_workspace_analytics": _typed_output_schema(_WorkspaceAnalyticsOutput),
    "get_best_times": _typed_output_schema(_BestTimesOutput),
    "list_inbox": _typed_output_schema(_ListInboxOutput),
    "get_inbox_message": _typed_output_schema(_InboxMessageDetail),
    "add_inbox_note": _typed_output_schema(_InboxNoteOutput),
    "assign_inbox_message": _typed_output_schema(_InboxMessageSummary),
    "set_inbox_status": _typed_output_schema(_InboxMessageSummary),
    "send_inbox_reply": _typed_output_schema(_InboxReplyOutput),
    "list_accounts": _typed_output_schema(_ListAccountsOutput),
    "list_posts": _typed_output_schema(_ListPostsOutput),
    "search_media": _typed_output_schema(_SearchMediaOutput),
    "request_media_upload": _typed_output_schema(_RequestMediaUploadOutput),
    "get_account_analytics": _typed_output_schema(AccountAnalyticsResponse),
    "get_post_analytics": _typed_output_schema(PostAnalyticsResponse),
}
for _post_tool in {
    "cancel_post",
    "clone_post",
    "convert_idea_to_draft",
    "create_draft",
    "get_post",
    "schedule_draft",
    "schedule_post",
    "update_draft",
}:
    _OUTPUT_SCHEMAS[_post_tool] = _typed_output_schema(PostResponse)
for _media_tool in {"finalize_media_upload", "get_media", "upload_media"}:
    _OUTPUT_SCHEMAS[_media_tool] = _typed_output_schema(MediaAssetResponse)


def _with_builtin_metadata(tool: Tool) -> Tool:
    """Enrich the original surface while handlers migrate incrementally."""
    properties = dict(tool.input_schema.get("properties", {}))
    workspace_scoped = tool.name not in _GLOBAL_TOOLS
    if workspace_scoped:
        properties["workspace_id"] = {
            "type": "string",
            "format": "uuid",
            "description": "Explicit workspace for OAuth callers; optional for a pinned API key.",
        }
    input_schema = {**tool.input_schema, "properties": properties}
    output_schema = _OUTPUT_SCHEMAS.get(tool.name, tool.output_schema)
    if "oneOf" not in output_schema:
        success_schema = {**output_schema, "not": {"required": ["error"]}}
        output_schema = {
            "type": "object",
            "oneOf": [success_schema, DOMAIN_ERROR_OUTPUT_SCHEMA],
        }
    tool = replace(
        tool,
        input_schema=input_schema,
        output_schema=output_schema,
        workspace_scoped=workspace_scoped,
    )
    if tool.name in _READ_TOOLS:
        permission = "view_analytics" if tool.name.endswith("analytics") or tool.name == "get_best_times" else None
        permissions = _READ_PERMISSIONS.get(tool.name, (permission,) if permission else ())
        return replace(
            tool,
            annotations=ToolAnnotations(
                title=tool.name.replace("_", " ").title(),
                read_only=True,
                idempotent=True,
            ),
            required_scope="mcp.read",
            required_permissions=permissions,
        )
    if tool.name in _INBOX_REPLY_TOOLS:
        confirmation_properties = dict(tool.input_schema.get("properties", {}))
        confirmation_properties.update(
            {
                "confirmation_token": {
                    "type": "string",
                    "description": "One-use token returned by the matching preview call.",
                },
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "Caller-generated replay key required with confirmation_token.",
                },
            }
        )
        output_schema = dict(tool.output_schema)
        variants = list(output_schema.get("oneOf", []))
        if variants:
            variants = [variants[0], _CONFIRMATION_PREVIEW_SCHEMA, _CONFIRMED_ACTION_SCHEMA, *variants[1:]]
            output_schema["oneOf"] = variants
        return replace(
            tool,
            input_schema={**tool.input_schema, "properties": confirmation_properties},
            output_schema=output_schema,
            annotations=ToolAnnotations(title="Send Inbox Reply", open_world=True),
            risk_level="high",
            required_scope="mcp.inbox.reply",
            required_permissions=("reply_from_inbox",),
            confirmation_required=True,
        )
    if tool.name in _PUBLISH_TOOLS:
        confirmation_properties = dict(tool.input_schema.get("properties", {}))
        confirmation_properties.update(
            {
                "confirmation_token": {
                    "type": "string",
                    "description": "One-use token returned by the matching preview call.",
                },
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "Caller-generated replay key required with confirmation_token.",
                },
            }
        )
        output_schema = dict(tool.output_schema)
        variants = list(output_schema.get("oneOf", []))
        if variants:
            variants = [variants[0], _CONFIRMATION_PREVIEW_SCHEMA, _CONFIRMED_ACTION_SCHEMA, *variants[1:]]
            output_schema["oneOf"] = variants
        return replace(
            tool,
            input_schema={**tool.input_schema, "properties": confirmation_properties},
            output_schema=output_schema,
            annotations=ToolAnnotations(
                title=tool.name.replace("_", " ").title(),
                open_world=True,
            ),
            risk_level="high",
            required_scope="mcp.publish",
            required_permissions=_PUBLISH_PERMISSIONS[tool.name],
            confirmation_required=True,
        )
    if tool.name in _CONTENT_TOOLS:
        return replace(
            tool,
            annotations=ToolAnnotations(title=tool.name.replace("_", " ").title()),
            risk_level="medium",
            required_scope="mcp.content",
            required_permissions=_CONTENT_PERMISSIONS[tool.name],
        )
    return tool


def register_tool(tool: Tool) -> None:
    registry.register(_with_builtin_metadata(tool))


def all_tools() -> list[Tool]:
    return registry.discover()


def get_tool(name: str, *, include_disabled: bool = False) -> Tool | None:
    return registry.get(name, include_disabled=include_disabled)


def require_tool(name: str, *, registry: ToolRegistry = registry) -> Tool:
    from apps.mcp.errors import DomainError, tool_disabled_error

    tool = registry.get(name, include_disabled=True)
    if tool is None:
        raise DomainError("unknown_tool", "Unknown tool.")
    if not tool.enabled:
        raise tool_disabled_error(name)
    return tool


def _reset_registry_for_tests() -> None:  # pragma: no cover
    registry.clear()
