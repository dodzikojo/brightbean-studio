"""MCP workspace discovery and read-only context tools."""

from __future__ import annotations

from typing import Any

from django.urls import reverse

from apps.api.schemas import AccountSummary
from apps.mcp.errors import DOMAIN_ERROR_OUTPUT_SCHEMA, DomainError
from apps.mcp.registry import Tool, register_tool
from apps.mcp.results import resource_link, success_result
from apps.social_accounts.models import SocialAccount


def _output(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            DOMAIN_ERROR_OUTPUT_SCHEMA,
        ],
    }


def _list_workspaces(args: dict, context: dict[str, Any]) -> dict:
    del args
    from apps.mcp.policy import evaluate_tool_policy
    from apps.mcp.registry import require_tool

    principal = context["principal"]
    tool = require_tool("list_workspaces")
    workspaces = sorted(
        (
            item
            for item in principal.authorized_workspaces
            if evaluate_tool_policy(principal, tool, workspace=item.workspace).allowed
        ),
        key=lambda item: (item.workspace.name.casefold(), str(item.workspace.id)),
    )
    return success_result(
        {
            "workspaces": [
                {
                    "id": str(item.workspace.id),
                    "name": item.workspace.name,
                    "organization_id": str(item.workspace.organization_id),
                    "role": item.membership.workspace_role,
                    "timezone": item.workspace.effective_timezone,
                }
                for item in workspaces
                if not item.workspace.is_archived
            ]
        },
        resource_links=[resource_link("brightbean://workspaces", name="Authorized workspaces")],
    )


register_tool(
    Tool(
        name="list_workspaces",
        description="List every non-archived workspace authorized for this MCP credential.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema=_output(
            {
                "workspaces": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "format": "uuid"},
                            "name": {"type": "string"},
                            "organization_id": {"type": "string", "format": "uuid"},
                            "role": {"type": "string"},
                            "timezone": {"type": "string"},
                        },
                        "required": ["id", "name", "organization_id", "role", "timezone"],
                        "additionalProperties": False,
                    },
                }
            },
            ["workspaces"],
        ),
        handler=_list_workspaces,
    )
)


def _get_workspace_context(args: dict, context: dict[str, Any]) -> dict:
    del args
    workspace = context["workspace"]
    categories = list(workspace.content_categories.order_by("position", "name", "id").values("id", "name", "color"))
    tags = list(workspace.tags.order_by("name", "id").values("id", "name"))
    templates = list(workspace.post_templates.order_by("name", "id").values("id", "name", "description"))
    payload = {
        "id": str(workspace.id),
        "name": workspace.name,
        "description": workspace.description,
        "timezone": workspace.effective_timezone,
        "brand_colors": {
            "primary": workspace.primary_color,
            "secondary": workspace.secondary_color,
        },
        "default_hashtags": workspace.default_hashtags or [],
        "approval_policy": workspace.approval_workflow_mode,
        "categories": categories,
        "tags": tags,
        "templates": templates,
    }
    return success_result(
        payload,
        resource_links=[
            resource_link(
                f"brightbean://workspaces/{workspace.id}/context",
                name=f"{workspace.name} context",
            )
        ],
    )


register_tool(
    Tool(
        name="get_workspace_context",
        description="Read authorized brand, timezone, approval, taxonomy, and template context.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={"type": "object", "additionalProperties": True},
        handler=_get_workspace_context,
    )
)


def _get_account_health(args: dict, context: dict[str, Any]) -> dict:
    workspace_context = context["workspace_context"]
    account = (
        SocialAccount.objects.filter(
            id=args["social_account_id"],
            workspace_id=workspace_context.workspace_id,
            id__in=workspace_context.allowed_account_ids,
        )
        .only(
            "id",
            "platform",
            "account_name",
            "account_handle",
            "connection_status",
            "analytics_needs_reconnect",
            "webhooks_active",
            "webhook_needs_reconnect",
            "last_health_check_at",
        )
        .first()
    )
    if account is None:
        raise DomainError("resource_not_found", "Social account not found.")
    issues: list[str] = []
    if account.connection_status != SocialAccount.ConnectionStatus.CONNECTED:
        issues.append("connection")
    if account.analytics_needs_reconnect:
        issues.append("analytics")
    if account.webhooks_active is False or account.webhook_needs_reconnect:
        issues.append("webhooks")
    reconnect_path = reverse(
        "social_accounts:reconnect",
        kwargs={"workspace_id": workspace_context.workspace_id, "account_id": account.id},
    )
    return success_result(
        {
            **AccountSummary.from_social_account(account).model_dump(mode="json"),
            "healthy": not issues,
            "needs_reconnect": account.needs_reconnect
            or account.analytics_needs_reconnect
            or account.webhook_needs_reconnect,
            "issues": issues,
            "last_health_check_at": (
                account.last_health_check_at.isoformat() if account.last_health_check_at else None
            ),
            "reconnect_path": reconnect_path,
        }
    )


register_tool(
    Tool(
        name="get_account_health",
        description="Return safe connection capability diagnostics and a browser reconnect path.",
        input_schema={
            "type": "object",
            "properties": {"social_account_id": {"type": "string", "format": "uuid"}},
            "required": ["social_account_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        handler=_get_account_health,
    )
)
