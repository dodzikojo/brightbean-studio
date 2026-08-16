from __future__ import annotations

import functools
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.members.models import WorkspaceMembership, has_org_permission


@login_required
def settings_index(request):
    return render(request, "settings_manager/index.html")


def _mcp_access(view_func):
    @functools.wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if request.org is None:
            raise PermissionDenied("You do not have access to MCP settings.")
        request.mcp_can_manage_org = has_org_permission(request.org_membership, "manage_api_keys")
        memberships = (
            WorkspaceMembership.objects.filter(
                user=request.user,
                workspace__organization=request.org,
                workspace__is_archived=False,
            )
            .select_related("workspace", "custom_role")
            .order_by("workspace__name")
        )
        if request.mcp_can_manage_org:
            from apps.workspaces.models import Workspace

            request.mcp_workspaces = list(
                Workspace.objects.filter(organization=request.org, is_archived=False).order_by("name", "id")
            )
        else:
            request.mcp_workspaces = [
                membership.workspace
                for membership in memberships
                if membership.effective_permissions.get("manage_workspace_settings", False)
            ]
        if not request.mcp_can_manage_org and not request.mcp_workspaces:
            raise PermissionDenied("You do not have access to MCP settings.")
        return view_func(request, *args, **kwargs)

    return login_required(wrapped)


def _activity_queryset(request):
    from apps.mcp.models import McpActivityEvent

    queryset = McpActivityEvent.objects.filter(organization=request.org).select_related(
        "workspace",
        "actor",
        "api_key",
        "oauth_application",
    )
    if not request.mcp_can_manage_org:
        queryset = queryset.filter(workspace_id__in=[workspace.id for workspace in request.mcp_workspaces])
    return queryset


def _base_context(request):
    return {
        "settings_active": "mcp",
        "mcp_section": "overview",
        "can_manage_org": request.mcp_can_manage_org,
        "available_workspaces": request.mcp_workspaces,
    }


@_mcp_access
@require_http_methods(["GET", "POST"])
def mcp_overview(request):
    from apps.mcp.models import McpOrganizationConfig
    from apps.mcp.protocol import SERVER_VERSION

    config = McpOrganizationConfig.objects.filter(organization=request.org).first()
    if request.method == "POST":
        if not request.mcp_can_manage_org:
            raise PermissionDenied("Only organization administrators can change organization MCP access.")
        raw_enabled = request.POST.get("enabled")
        if raw_enabled not in {"true", "false"}:
            raise PermissionDenied("Invalid MCP organization setting.")
        config, _ = McpOrganizationConfig.objects.update_or_create(
            organization=request.org,
            defaults={"enabled": raw_enabled == "true"},
        )
        messages.success(request, "Organization MCP access updated.")
        return redirect("settings_manager:mcp_overview")

    organization_enabled = config.enabled if config is not None else True
    infrastructure_enabled = bool(settings.MCP_SERVER_ENABLED)
    context = _base_context(request)
    context.update(
        {
            "endpoint": request.build_absolute_uri("/api/v1/mcp"),
            "server_version": SERVER_VERSION,
            "transport_backend": settings.MCP_TRANSPORT_BACKEND,
            "infrastructure_enabled": infrastructure_enabled,
            "organization_enabled": organization_enabled,
            "effective_enabled": infrastructure_enabled and organization_enabled,
            "recent_events": list(_activity_queryset(request).order_by("-created_at")[:8]),
        }
    )
    return render(request, "settings_manager/mcp/overview.html", context)


def _selected_workspace(request, raw_workspace_id):
    if not raw_workspace_id:
        return None
    try:
        workspace_id = UUID(str(raw_workspace_id))
    except (TypeError, ValueError) as exc:
        raise PermissionDenied("Invalid workspace selection.") from exc
    workspace = next((item for item in request.mcp_workspaces if item.id == workspace_id), None)
    if workspace is None:
        raise PermissionDenied("You cannot manage MCP for that workspace.")
    return workspace


@_mcp_access
@require_http_methods(["GET"])
def mcp_activity(request):
    from apps.mcp.models import McpActivityEvent

    queryset = _activity_queryset(request).order_by("-created_at")
    selected_workspace = _selected_workspace(request, request.GET.get("workspace"))
    if selected_workspace is not None:
        queryset = queryset.filter(workspace=selected_workspace)
    primitive = request.GET.get("primitive", "")
    if primitive in McpActivityEvent.Primitive.values:
        queryset = queryset.filter(primitive=primitive)
    outcome = request.GET.get("outcome", "")
    if outcome in McpActivityEvent.Status.values:
        queryset = queryset.filter(status=outcome)
    confirmation = request.GET.get("confirmation", "")
    if confirmation in McpActivityEvent.ConfirmationState.values:
        queryset = queryset.filter(confirmation_state=confirmation)
    tool_query = request.GET.get("tool", "").strip()[:128]
    if tool_query:
        queryset = queryset.filter(name__icontains=tool_query)
    actor_query = request.GET.get("actor", "").strip()[:128]
    if actor_query:
        queryset = queryset.filter(Q(actor__email__icontains=actor_query) | Q(actor__name__icontains=actor_query))
    credential = request.GET.get("credential", "")
    if credential in McpActivityEvent.CredentialType.values:
        queryset = queryset.filter(credential_type=credential)
    since = request.GET.get("since", "")
    since_days = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}.get(since)
    if since_days is not None:
        queryset = queryset.filter(created_at__gte=timezone.now() - timedelta(days=since_days))

    context = _base_context(request)
    context.update(
        {
            "mcp_section": "activity",
            "page_obj": Paginator(queryset, 50).get_page(request.GET.get("page")),
            "selected_workspace": selected_workspace,
            "filters": {
                "primitive": primitive,
                "outcome": outcome,
                "confirmation": confirmation,
                "tool": tool_query,
                "actor": actor_query,
                "credential": credential,
                "since": since,
            },
            "primitive_choices": McpActivityEvent.Primitive.choices,
            "outcome_choices": McpActivityEvent.Status.choices,
            "confirmation_choices": McpActivityEvent.ConfirmationState.choices,
            "credential_choices": McpActivityEvent.CredentialType.choices,
        }
    )
    return render(request, "settings_manager/mcp/activity.html", context)


@_mcp_access
@require_http_methods(["GET", "POST"])
def mcp_tools(request):
    from apps.mcp.models import McpToolPolicy
    from apps.mcp.registry import all_tools, get_tool

    if request.method == "POST":
        tool_name = request.POST.get("tool_name", "")
        if get_tool(tool_name, include_disabled=True) is None:
            raise PermissionDenied("Unknown MCP tool.")
        raw_enabled = request.POST.get("enabled")
        if raw_enabled not in {"true", "false"}:
            raise PermissionDenied("Invalid MCP tool setting.")
        enabled = raw_enabled == "true"
        scope = request.POST.get("scope")
        if scope == "organization":
            if not request.mcp_can_manage_org:
                raise PermissionDenied("Only organization administrators can change organization tool policies.")
            McpToolPolicy.objects.update_or_create(
                organization=request.org,
                workspace=None,
                tool_name=tool_name,
                defaults={"enabled": enabled},
            )
        elif scope == "workspace":
            workspace = _selected_workspace(request, request.POST.get("workspace_id"))
            if workspace is None:
                raise PermissionDenied("Choose a workspace.")
            organization_policy = McpToolPolicy.objects.filter(
                organization=request.org,
                workspace__isnull=True,
                tool_name=tool_name,
                enabled=False,
            ).exists()
            if enabled and organization_policy:
                raise PermissionDenied("An organization restriction cannot be overridden by a workspace.")
            McpToolPolicy.objects.update_or_create(
                organization=request.org,
                workspace=workspace,
                tool_name=tool_name,
                defaults={"enabled": enabled},
            )
        else:
            raise PermissionDenied("Invalid MCP policy scope.")
        messages.success(request, f"{tool_name} policy updated.")
        redirect_url = reverse("settings_manager:mcp_tools")
        workspace_value = request.POST.get("workspace_id")
        if workspace_value:
            redirect_url = f"{redirect_url}?workspace={workspace_value}"
        return redirect(redirect_url)

    selected_workspace = _selected_workspace(request, request.GET.get("workspace"))
    org_policies = {
        policy.tool_name: policy.enabled
        for policy in McpToolPolicy.objects.filter(organization=request.org, workspace__isnull=True)
    }
    workspace_policies = {}
    if selected_workspace is not None:
        workspace_policies = {
            policy.tool_name: policy.enabled for policy in McpToolPolicy.objects.filter(workspace=selected_workspace)
        }
    query = request.GET.get("q", "").strip()[:128]
    tools = all_tools()
    if query:
        folded = query.casefold()
        tools = [tool for tool in tools if folded in tool.name.casefold() or folded in tool.description.casefold()]
    rows = [
        {
            "tool": tool,
            "organization_enabled": org_policies.get(tool.name, True),
            "workspace_enabled": workspace_policies.get(tool.name, True),
            "effective_enabled": (
                tool.enabled and org_policies.get(tool.name, True) and workspace_policies.get(tool.name, True)
            ),
        }
        for tool in tools
    ]
    context = _base_context(request)
    context.update(
        {
            "mcp_section": "tools",
            "rows": rows,
            "query": query,
            "selected_workspace": selected_workspace,
        }
    )
    return render(request, "settings_manager/mcp/tools.html", context)
