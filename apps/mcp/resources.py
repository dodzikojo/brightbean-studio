"""Authorized, JSON resource surface shared by legacy and SDK transports."""

from __future__ import annotations

import json
import re
from typing import Any

from apps.mcp.errors import DomainError

_RESOURCE_ROUTES: tuple[tuple[re.Pattern[str], str, dict[str, Any]], ...] = (
    (re.compile(r"^brightbean://workspaces/(?P<workspace_id>[0-9a-f-]+)/context$"), "get_workspace_context", {}),
    (re.compile(r"^brightbean://workspaces/(?P<workspace_id>[0-9a-f-]+)/accounts$"), "list_accounts", {}),
    (
        re.compile(
            r"^brightbean://workspaces/(?P<workspace_id>[0-9a-f-]+)/calendar/"
            r"(?P<start_date>\d{4}-\d{2}-\d{2})/(?P<end_date>\d{4}-\d{2}-\d{2})$"
        ),
        "get_calendar",
        {},
    ),
    (
        re.compile(r"^brightbean://workspaces/(?P<workspace_id>[0-9a-f-]+)/posts/(?P<post_id>[0-9a-f-]+)$"),
        "get_post",
        {},
    ),
    (
        re.compile(r"^brightbean://workspaces/(?P<workspace_id>[0-9a-f-]+)/analytics/(?P<days>\d+)$"),
        "get_workspace_analytics",
        {"days": int},
    ),
    (
        re.compile(r"^brightbean://workspaces/(?P<workspace_id>[0-9a-f-]+)/inbox/(?P<message_id>[0-9a-f-]+)$"),
        "get_inbox_message",
        {},
    ),
)

RESOURCE_TEMPLATES = (
    ("workspace_context", "brightbean://workspaces/{workspace_id}/context", "Workspace brand and editorial context"),
    ("workspace_accounts", "brightbean://workspaces/{workspace_id}/accounts", "Authorized social accounts"),
    (
        "workspace_calendar",
        "brightbean://workspaces/{workspace_id}/calendar/{start_date}/{end_date}",
        "Calendar posts and events in an inclusive local-date range",
    ),
    ("workspace_post", "brightbean://workspaces/{workspace_id}/posts/{post_id}", "One authorized post"),
    ("workspace_analytics", "brightbean://workspaces/{workspace_id}/analytics/{days}", "Workspace analytics"),
    ("workspace_inbox_message", "brightbean://workspaces/{workspace_id}/inbox/{message_id}", "One inbox message"),
)


def _invoke_read_tool(principal, request, tool_name: str, workspace_id: str | None, arguments: dict[str, Any]):
    from apps.mcp.policy import evaluate_tool_policy, policy_error
    from apps.mcp.registry import require_tool
    from apps.mcp.workspace import build_tool_context

    tool = require_tool(tool_name)
    if tool.workspace_scoped:
        context = build_tool_context(principal, workspace_id, request, is_write=False)
    else:
        context = {"principal": principal, "request": request}
    decision = evaluate_tool_policy(principal, tool, workspace=context.get("workspace"))
    if not decision.allowed:
        raise policy_error(decision, tool_name)
    result = tool.handler(arguments, context)
    if result.get("isError"):
        error = (result.get("structuredContent") or {}).get("error") or {}
        raise DomainError(error.get("code", "invalid_request"), error.get("message", "Resource read failed."))
    return result.get("structuredContent", {})


def list_resources(principal, request) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    try:
        _invoke_read_tool(principal, request, "list_workspaces", None, {})
    except DomainError:
        pass
    else:
        resources.append(
            {
                "name": "authorized_workspaces",
                "title": "Authorized workspaces",
                "uri": "brightbean://workspaces",
                "description": "Safe identifiers for every workspace authorized to this credential.",
                "mimeType": "application/json",
            }
        )
    for item in principal.authorized_workspaces:
        for suffix, tool_name, title in (
            ("context", "get_workspace_context", "context"),
            ("accounts", "list_accounts", "accounts"),
        ):
            try:
                _invoke_read_tool(principal, request, tool_name, str(item.workspace.id), {})
            except DomainError:
                continue
            resources.append(
                {
                    "name": f"workspace_{suffix}_{item.workspace.id}",
                    "title": f"{item.workspace.name} {title}",
                    "uri": f"brightbean://workspaces/{item.workspace.id}/{suffix}",
                    "mimeType": "application/json",
                }
            )
    return resources


def list_resource_templates() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "uriTemplate": uri_template,
            "description": description,
            "mimeType": "application/json",
        }
        for name, uri_template, description in RESOURCE_TEMPLATES
    ]


def read_resource(principal, request, uri: str) -> dict[str, Any]:
    if uri == "brightbean://workspaces":
        payload = _invoke_read_tool(principal, request, "list_workspaces", None, {})
    else:
        payload = None
        for pattern, tool_name, converters in _RESOURCE_ROUTES:
            match = pattern.fullmatch(uri)
            if match is None:
                continue
            values: dict[str, Any] = match.groupdict()
            workspace_id = values.pop("workspace_id")
            for key, converter in converters.items():
                values[key] = converter(values[key])
            payload = _invoke_read_tool(principal, request, tool_name, workspace_id, values)
            break
        if payload is None:
            raise DomainError("resource_not_found", "Unknown BrightBean resource URI.")
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(payload, sort_keys=True),
            }
        ]
    }
