"""Deterministic policy evaluation shared by MCP discovery and invocation."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.conf import settings

from apps.mcp.principal import McpPrincipal
from apps.mcp.registry import Tool


@dataclass(frozen=True)
class McpPolicyDecision:
    allowed: bool
    code: str | None = None
    reason: str | None = None


def _deny(code: str, reason: str) -> McpPolicyDecision:
    return McpPolicyDecision(False, code=code, reason=reason)


def requested_account_ids(arguments: dict) -> frozenset[str]:
    """Extract account references from validated top-level tool arguments."""
    values: set[str] = set()
    for field in ("account_id", "social_account_id"):
        value = arguments.get(field)
        if isinstance(value, (str, UUID)):
            values.add(str(value))
    for field in ("account_ids", "social_account_ids"):
        items = arguments.get(field)
        if isinstance(items, (list, tuple, set, frozenset)):
            values.update(str(value) for value in items if isinstance(value, (str, UUID)))
    return frozenset(values)


def is_tool_discoverable(principal: McpPrincipal, tool: Tool) -> bool:
    workspaces = getattr(principal, "authorized_workspaces", None)
    if tool.workspace_scoped and workspaces is not None:
        return any(evaluate_tool_policy(principal, tool, workspace=item.workspace).allowed for item in workspaces)
    return evaluate_tool_policy(principal, tool, workspace=None).allowed


def policy_error(decision: McpPolicyDecision, tool_name: str):
    from apps.mcp.errors import DomainError

    messages = {
        "server_disabled": "The MCP server is disabled.",
        "organization_disabled": "MCP is disabled for this organization.",
        "tool_disabled": "This tool is currently disabled.",
        "forbidden": "This credential cannot use that tool.",
    }
    code = decision.code or "forbidden"
    details = {"tool": tool_name} if code == "tool_disabled" else None
    return DomainError(code, messages.get(code, "This operation is not allowed."), details=details)


def evaluate_tool_policy(
    principal: McpPrincipal,
    tool: Tool,
    *,
    workspace,
    requested_account_ids: set[str | UUID] | frozenset[str | UUID] = frozenset(),
) -> McpPolicyDecision:
    """Evaluate policy in the documented precedence order without side effects."""
    authorized_workspaces = getattr(principal, "authorized_workspaces", ())
    if workspace is None and not tool.workspace_scoped and authorized_workspaces:
        decisions = [
            evaluate_tool_policy(
                principal,
                tool,
                workspace=item.workspace,
                requested_account_ids=requested_account_ids,
            )
            for item in authorized_workspaces
        ]
        if any(decision.allowed for decision in decisions):
            return McpPolicyDecision(True)
        return decisions[0]

    if not settings.MCP_SERVER_ENABLED:
        return _deny("server_disabled", "infrastructure")

    organization = workspace.organization if workspace is not None else None
    if organization is not None:
        from apps.mcp.models import McpOrganizationConfig, McpToolPolicy

        config = McpOrganizationConfig.objects.filter(organization_id=organization.id).only("enabled").first()
        if config is not None and not config.enabled:
            return _deny("organization_disabled", "organization_disabled")

    if not tool.enabled:
        return _deny("tool_disabled", "code_disabled")

    if organization is not None:
        org_policy = (
            McpToolPolicy.objects.filter(
                organization_id=organization.id,
                workspace__isnull=True,
                tool_name=tool.name,
            )
            .only("enabled")
            .first()
        )
        if org_policy is not None and not org_policy.enabled:
            return _deny("tool_disabled", "organization_tool_policy")

        workspace_policy = (
            McpToolPolicy.objects.filter(workspace_id=workspace.id, tool_name=tool.name).only("enabled").first()
        )
        if workspace_policy is not None and not workspace_policy.enabled:
            return _deny("tool_disabled", "workspace_tool_policy")

    required_permissions = frozenset(tool.required_permissions)
    if principal.credential_kind == "oauth":
        from apps.oauth_server.scopes import scope_allows

        if not scope_allows(principal.granted_scopes, tool.required_scope):
            return _deny("forbidden", "oauth_scope")
    elif not required_permissions.issubset(principal.api_key_permissions):
        return _deny("forbidden", "api_key_permission")

    if workspace is not None:
        principal_workspace = next(
            (item for item in principal.authorized_workspaces if item.workspace.id == workspace.id),
            None,
        )
        if principal_workspace is None:
            return _deny("forbidden", "workspace_membership")
        current_permissions = frozenset(
            key for key, enabled in principal_workspace.membership.effective_permissions.items() if enabled
        )
        if not required_permissions.issubset(current_permissions):
            return _deny("forbidden", "rbac")

    if principal.credential_kind == "api_key" and requested_account_ids:
        requested = {str(value) for value in requested_account_ids}
        allowed = {str(value) for value in principal.account_allowlist_ids}
        if not requested.issubset(allowed):
            return _deny("forbidden", "account_allowlist")

    return McpPolicyDecision(True)
